"""Tests for the import impact dry-run (``GET /api/addresses/preview/{token}/impact``)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as app_db  # noqa: E402
from app.main import app, get_conn  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _preview(client, csv_bytes: bytes, name: str = "p.csv") -> dict:
    r = client.post("/api/addresses/preview",
                    files={"file": (name, csv_bytes, "text/csv")})
    assert r.status_code == 200
    return r.json()


def _impact(client, token: str):
    return client.get(f"/api/addresses/preview/{token}/impact")


def _item_for(data: dict, query: str) -> dict:
    matches = [it for it in data["items"] if it["query"] == query]
    assert len(matches) == 1, [it["query"] for it in data["items"]]
    return matches[0]


def _add_address(client, **fields) -> None:
    r = client.post("/api/addresses", json={"addresses": [fields]})
    assert r.status_code == 200
    assert r.json()["inserted"] == 1


def test_impact_improved_read_only_and_voucher_intact(client):
    # A query the current table cannot cover -> no_match log.
    query = "1 Impfx Alpha Way, Impfxville"
    r = client.post("/api/match", json={"query": query})
    assert r.json()["status"] == "no_match"

    csv_bytes = b'external_id,address\nIMP-001,"1 Impfx Alpha Way, Impfxville"\n'
    token = _preview(client, csv_bytes)["preview_token"]

    addr_before = client.get("/api/health").json()["address_count"]
    logs_before = client.get("/api/matches?limit=1").json()["total"]

    r = _impact(client, token)
    assert r.status_code == 200
    data = r.json()
    assert data["filename"] == "p.csv"
    assert data["valid_rows"] == 1
    # New addresses are identified by their CSV line number.
    assert data["new_addresses"] == [{
        "csv_line": 2,
        "full_text": "1 Impfx Alpha Way, Impfxville",
        "house": "1",
        "external_id": "IMP-001",
    }]

    item = _item_for(data, query)
    assert item["category"] == "improved"
    assert item["category_label"] == "改善"
    assert item["before"]["status"] == "no_match"
    assert item["before"]["target"] is None
    assert item["after"]["status"] == "auto_matched"
    assert item["after"]["target"]["kind"] == "new"
    assert item["after"]["target"]["csv_line"] == 2
    assert item["after"]["target"]["full_text"] == "1 Impfx Alpha Way, Impfxville"

    # Summary counts are consistent and cover every rehearsed query.
    s = data["summary"]
    assert s["total"] == data["query_count"] == len(data["items"])
    assert s["total"] == (
        s["improved"] + s["regressed"] + s["retargeted"] + s["unchanged"]
    )
    assert s["improved"] >= 1

    # The dry-run is read-only: no addresses and no match logs were written.
    assert client.get("/api/health").json()["address_count"] == addr_before
    assert client.get("/api/matches?limit=1").json()["total"] == logs_before

    # The voucher is not consumed by the dry-run: confirm still writes the
    # one valid row through the regular file-check + transaction path.
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    assert client.get("/api/health").json()["address_count"] == addr_before + 1


def test_impact_retargeted(client):
    _add_address(client, address="500 Impfx Beta Road", city="Impfxton Heights",
                 state="IL", postal_code="62704", external_id="IMP-BASE-1")
    query = "500 Impfx Beta Road, Impfxton"
    m = client.post("/api/match", json={"query": query}).json()
    assert m["status"] == "auto_matched"
    base_id = m["best_id"]

    # The new row matches the query exactly and outranks the current best.
    csv_bytes = b'external_id,address\nIMP-002,"500 Impfx Beta Road, Impfxton"\n'
    token = _preview(client, csv_bytes)["preview_token"]
    data = _impact(client, token).json()

    item = _item_for(data, query)
    assert item["category"] == "retargeted"
    assert item["category_label"] == "目标改派"
    assert item["before"]["status"] == "auto_matched"
    assert item["before"]["target"] == {
        "kind": "existing", "id": base_id, "full_text": "500 Impfx Beta Road",
    }
    assert item["after"]["status"] == "auto_matched"
    assert item["after"]["target"]["kind"] == "new"
    assert item["after"]["target"]["csv_line"] == 2


def test_impact_regressed(client):
    _add_address(client, address="700 Impfx Gamma Street", city="Impfxburg",
                 state="IL", postal_code="62704", external_id="IMP-BASE-2")
    query = "700 Impfx Gamma Street, Impfxburg"
    m = client.post("/api/match", json={"query": query}).json()
    assert m["status"] == "auto_matched"

    # A near-duplicate of the base address: close enough to make the top two
    # ambiguous (margin < threshold), so the after verdict drops to review.
    csv_bytes = (
        b'external_id,address\nIMP-003,"700 Impfx Gamma Street, Impfxburg, IL"\n'
    )
    token = _preview(client, csv_bytes)["preview_token"]
    data = _impact(client, token).json()

    item = _item_for(data, query)
    assert item["category"] == "regressed"
    assert item["category_label"] == "退化"
    assert item["before"]["status"] == "auto_matched"
    assert item["after"]["status"] == "needs_review"


def test_impact_applies_review_consensus(client):
    _add_address(client, address="900 Impfx Delta Ave", city="Impfx City",
                 external_id="IMP-BASE-3")

    # Two reviewed records with the same normalised key, both confirming the
    # same address, form a review consensus for later queries.
    for q in ("901 Impfx Delta Ave, Impfx City",
              "901 Impfx Delta Avenue, Impfx City"):
        m = client.post("/api/match", json={"query": q}).json()
        assert m["status"] == "needs_review"
        claim = client.post(f"/api/matches/{m['match_id']}/claim",
                            json={"reviewer": "pytest"})
        assert claim.status_code == 200
        r = client.post(f"/api/matches/{m['match_id']}/review",
                        json={"action": "confirmed",
                              "token": claim.json()["token"]})
        assert r.status_code == 200

    # Same normalised key, different surface form: consensus redirects it.
    query = "901 Impfx Delta Av, Impfx City"
    m = client.post("/api/match", json={"query": query}).json()
    assert m["status"] == "auto_matched"
    assert m["consensus"]["applied"] is True

    # An unrelated import must leave the consensus-redirected verdict intact;
    # both dry-run passes apply the review-consensus memory like /api/match.
    csv_bytes = b"external_id,address\nIMP-004,1 Impfx Omega Court\n"
    token = _preview(client, csv_bytes)["preview_token"]
    data = _impact(client, token).json()

    item = _item_for(data, query)
    assert item["category"] == "unchanged"
    assert item["before"]["status"] == "auto_matched"
    assert item["before"]["consensus_applied"] is True
    assert item["after"]["status"] == "auto_matched"
    assert item["after"]["consensus_applied"] is True
    assert item["before"]["target"]["kind"] == "existing"
    assert item["before"]["target"]["id"] == item["after"]["target"]["id"]


def test_impact_skips_invalid_and_duplicate_rows(client):
    csv_bytes = (
        b"external_id,address\n"
        b"IMP-007,7 Impfx Phi Terrace\n"          # line 2: valid
        b",\n"                                     # line 3: invalid (blank)
        b"IMP-BASE-3,900 Impfx Delta Ave\n"       # line 4: duplicate of table
        b"IMP-007,7 Impfx Phi Terrace\n"          # line 5: in-file duplicate
    )
    preview = _preview(client, csv_bytes)
    assert preview["valid"] == 1
    assert preview["invalid"] == 1
    assert preview["duplicates"] == 2

    data = _impact(client, preview["preview_token"]).json()
    assert data["valid_rows"] == 1
    # Only the valid row becomes a temporary address, keyed by its CSV line.
    assert [a["csv_line"] for a in data["new_addresses"]] == [2]
    assert data["new_addresses"][0]["full_text"] == "7 Impfx Phi Terrace"


def test_impact_unknown_token(client):
    r = _impact(client, "does-not-exist")
    assert r.status_code == 404


def test_impact_table_changed(client):
    csv_bytes = b"external_id,address\nIMP-005,5 Impfx Sigma Lane\n"
    token = _preview(client, csv_bytes)["preview_token"]

    # Another writer mutates the address table after the preview.
    _add_address(client, address="8 Impfx Interloper Road",
                 external_id="IMP-INT-1")

    r = _impact(client, token)
    assert r.status_code == 409


def test_impact_expired_token(client):
    csv_bytes = b"external_id,address\nIMP-006,6 Impfx Tau Place\n"
    token = _preview(client, csv_bytes)["preview_token"]

    # Age the voucher out directly in the database.
    conn = get_conn()
    conn.execute(
        "UPDATE upload_previews SET expires_at = ? WHERE token = ?",
        ("2000-01-01T00:00:00+00:00", token),
    )
    conn.commit()

    r = _impact(client, token)
    assert r.status_code == 404


def test_impact_recent_distinct_queries_capped_at_100(client):
    # 105 fresh distinct queries plus a duplicate of the newest one.
    queries = [f"{i} Impfx Cap Street {i}" for i in range(105)]
    r = client.post("/api/match/batch",
                    json={"queries": queries, "save": True})
    assert r.status_code == 200
    r = client.post("/api/match", json={"query": queries[-1]})
    assert r.status_code == 200

    csv_bytes = b"external_id,address\nIMP-008,9 Impfx Chi Boulevard\n"
    token = _preview(client, csv_bytes)["preview_token"]
    data = _impact(client, token).json()

    assert data["query_limit"] == 100
    assert data["query_count"] == 100
    item_queries = [it["query"] for it in data["items"]]
    assert len(set(item_queries)) == 100
    # The 5 oldest of the 105 fell out of the most-recent-100 window.
    assert queries[0] not in item_queries
    assert queries[4] not in item_queries
    assert queries[5] in item_queries
    assert queries[-1] in item_queries
    # The duplicated query still appears exactly once, keyed by its latest log.
    dup = [it for it in data["items"] if it["query"] == queries[-1]]
    assert len(dup) == 1
    latest_id = client.get("/api/matches?limit=1").json()["items"][0]["id"]
    assert dup[0]["latest_match_id"] == latest_id


def test_impact_consistent_snapshot_under_concurrent_write(client, monkeypatch):
    """A confirm landing *between* the voucher check and the address read must
    not leak into the report: the whole dry-run sees one read snapshot."""
    _add_address(client, address="600 Impfx Race Road", city="Raceville",
                 external_id="IMP-RACE-1")
    query = "600 Impfx Race Road, Raceville"
    m = client.post("/api/match", json={"query": query}).json()
    assert m["status"] == "auto_matched"
    base_id = m["best_id"]

    csv_bytes = b"external_id,address\nIMP-RACE-2,1 Impfx Race Court\n"
    token = _preview(client, csv_bytes)["preview_token"]

    # Simulate a concurrent confirm: right after the voucher's table digest
    # was verified, another connection deletes the base address and commits.
    real_all_addresses = app_db.all_addresses
    deleted = {"done": False}

    def concurrent_delete(conn):
        if not deleted["done"]:
            deleted["done"] = True
            other = app_db.connect()
            try:
                other.execute("DELETE FROM addresses WHERE id = ?", (base_id,))
                other.commit()
            finally:
                other.close()
        return real_all_addresses(conn)

    monkeypatch.setattr(app_db, "all_addresses", concurrent_delete)

    r = _impact(client, token)
    assert r.status_code == 200
    item = _item_for(r.json(), query)
    # The rehearsal is pinned to the snapshot the voucher was validated
    # against: the before-run still auto-hits the base address even though
    # the live table had already lost it mid-request.  Without the snapshot
    # the before-verdict would be computed against the newer (deleted) table
    # while the voucher check saw the older one -- a mixed-version report.
    assert item["before"]["status"] == "auto_matched"
    assert item["before"]["target"] == {
        "kind": "existing", "id": base_id, "full_text": "600 Impfx Race Road",
    }
    assert item["category"] == "unchanged"
    # The endpoint's transaction boundary does not leak onto the shared conn.
    assert get_conn().in_transaction is False


def test_read_snapshot_pins_version_and_always_releases():
    """``db.read_snapshot``: consistent reads, released on success and error."""
    conn = app_db.connect()
    other = app_db.connect()
    try:
        with app_db.read_snapshot(conn):
            pinned = app_db.count_addresses(conn)
            app_db.insert_address(
                other,
                full_text="1 Impfxsnap Anchor Lane",
                tokens=["impfxsnap", "anchor", "ln"],
                house="1",
                fields={},
                raw={"full_text": "1 Impfxsnap Anchor Lane"},
            )
            other.commit()
            # The open snapshot still sees the version pinned at its first read.
            assert app_db.count_addresses(conn) == pinned
        # Released on success: the next read observes the committed write.
        assert conn.in_transaction is False
        assert app_db.count_addresses(conn) == pinned + 1

        # Released on error too: an exception rolls the read transaction back.
        with pytest.raises(RuntimeError):
            with app_db.read_snapshot(conn):
                raise RuntimeError("boom")
        assert conn.in_transaction is False
    finally:
        conn.close()
        other.close()
