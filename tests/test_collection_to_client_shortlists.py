import base64
import hashlib
import json

import db
import server


def auth_header():
    token = base64.b64encode(b"johnny:test-password").decode("ascii")
    return {"Authorization": "Basic " + token}


def setup_db(tmp_path, monkeypatch):
    database = tmp_path / "flow.db"
    monkeypatch.setattr(db, "DB_PATH", str(database))
    monkeypatch.setenv("WORKBENCH_USER", "johnny")
    monkeypatch.setenv("WORKBENCH_PASSWORD", "test-password")
    db.init_db()
    return database, server.app.test_client()


def seed_client_and_listings():
    conn = db.get_db()
    conn.execute("INSERT INTO clients (id, name, requirement_text) VALUES (?,?,?)", ("C1", "客人 A", "港区"))
    conn.execute("INSERT INTO clients (id, name, requirement_text) VALUES (?,?,?)", ("C2", "客人 B", "新宿区"))
    conn.execute("INSERT INTO listings (id, address, price, status, source) VALUES (?,?,?,?,?)", ("L1", "東京都港区赤坂", 3000, "draft", "reins"))
    conn.execute("INSERT INTO listings (id, address, price, status, source) VALUES (?,?,?,?,?)", ("L2", "東京都新宿区", 4000, "draft", "suumo"))
    conn.commit()
    conn.close()


def test_collection_suumo_import_adds_only_listings_as_draft_and_dedupes(tmp_path, monkeypatch):
    database, client = setup_db(tmp_path, monkeypatch)

    def fake_scrape_detail(url):
        return {"price": "5000", "address": "東京都港区芝", "area": "50", "layout": "2LDK", "source_url": url}

    def fake_import_to_db(data):
        import hashlib as _hashlib
        import sqlite3
        from datetime import datetime, timezone
        url = data["source_url"]
        key = _hashlib.sha256(url.encode()).hexdigest()[:24]
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        existing = conn.execute("SELECT id FROM listings WHERE source='suumo' AND reins_id=?", (key,)).fetchone()
        if existing:
            conn.close()
            return existing["id"], "existing"
        lid = "SU-DEDUP-1"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO listings (id, address, price, status, source, reins_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (lid, data["address"], int(data["price"]), "draft", "suumo", key, now, now),
        )
        conn.commit(); conn.close()
        return lid, "inserted"

    monkeypatch.setattr(server, "scrape_detail", fake_scrape_detail)
    monkeypatch.setattr(server, "import_to_db", fake_import_to_db)

    body = {"urls": ["https://suumo.jp/ms/chuko/tokyo/sc_minato/nc_123456789/"]}
    first = client.post("/api/collection/import", json=body)
    assert first.status_code == 200
    assert first.get_json()["imported"] == 1
    assert first.get_json()["existing"] == 0

    second = client.post("/api/collection/import", json=body)
    assert second.status_code == 200
    assert second.get_json()["imported"] == 0
    assert second.get_json()["existing"] == 1

    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE source='suumo'").fetchone()[0] == 1
    row = conn.execute("SELECT status FROM listings WHERE id='SU-DEDUP-1'").fetchone()
    assert row["status"] == "draft"
    assert conn.execute("SELECT COUNT(*) FROM client_shortlists").fetchone()[0] == 0
    conn.close()


def test_reins_import_status_reports_inserted_existing_failed_without_client_links(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)

    class FakeJobs:
        @staticmethod
        def get_job(_job_id):
            return {
                "job_id": "JOB1",
                "status": "done",
                "total": 3,
                "done_count": 3,
                "current_index": 2,
                "error": None,
                "items": [
                    {"status": "success", "action": "inserted", "listing_id": "L-new"},
                    {"status": "success", "action": "updated", "listing_id": "L-existing"},
                    {"status": "failed", "error": "boom"},
                ],
            }

    import sys
    monkeypatch.setitem(sys.modules, "reins_import_jobs", FakeJobs)
    resp = server.app.test_client().get("/api/collection/import-status/JOB1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["inserted"] == 1
    assert body["existing"] == 1
    assert body["failed"] == 1

    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM client_shortlists").fetchone()[0] == 0
    conn.close()


def test_bulk_add_shortlists_is_auth_required_idempotent_and_status_safe(tmp_path, monkeypatch):
    database, client = setup_db(tmp_path, monkeypatch)
    seed_client_and_listings()
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    denied = client.post("/api/client-shortlists/bulk-add", json={"client_ids": ["C1"], "listing_ids": ["L1"]})
    assert denied.status_code == 401
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    resp = client.post(
        "/api/client-shortlists/bulk-add",
        headers=auth_header(),
        json={"client_ids": ["C1", "C2"], "listing_ids": ["L1", "L2"]},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"code": 1, "created": 4, "already_exists": 0, "failed": [], "client_count": 2, "listing_count": 2}

    repeat = client.post(
        "/api/client-shortlists/bulk-add",
        headers=auth_header(),
        json={"client_ids": ["C1", "C2"], "listing_ids": ["L1", "L2"]},
    )
    assert repeat.status_code == 200
    assert repeat.get_json()["created"] == 0
    assert repeat.get_json()["already_exists"] == 4

    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM client_shortlists").fetchone()[0] == 4
    assert conn.execute("SELECT status FROM listings WHERE id='L1'").fetchone()["status"] == "draft"
    assert conn.execute("SELECT status FROM listings WHERE id='L2'").fetchone()["status"] == "draft"
    conn.close()


def test_bulk_add_rejects_invalid_ids_and_limits_without_partial_write(tmp_path, monkeypatch):
    database, client = setup_db(tmp_path, monkeypatch)
    seed_client_and_listings()
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    invalid = client.post(
        "/api/client-shortlists/bulk-add",
        headers=auth_header(),
        json={"client_ids": ["C1", "NOPE"], "listing_ids": ["L1", "MISSING"]},
    )
    assert invalid.status_code == 404
    body = invalid.get_json()
    assert body["missing_clients"] == ["NOPE"]
    assert body["missing_listings"] == ["MISSING"]
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM client_shortlists").fetchone()[0] == 0
    conn.close()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    too_many_clients = client.post(
        "/api/client-shortlists/bulk-add",
        headers=auth_header(),
        json={"client_ids": [f"C{i}" for i in range(21)], "listing_ids": ["L1"]},
    )
    assert too_many_clients.status_code == 400

    too_many_listings = client.post(
        "/api/client-shortlists/bulk-add",
        headers=auth_header(),
        json={"client_ids": ["C1"], "listing_ids": [f"L{i}" for i in range(51)]},
    )
    assert too_many_listings.status_code == 400


def test_remove_shortlist_does_not_delete_listing(tmp_path, monkeypatch):
    _, client = setup_db(tmp_path, monkeypatch)
    seed_client_and_listings()
    added = client.post("/api/client-shortlists/bulk-add", headers=auth_header(), json={"client_ids": ["C1"], "listing_ids": ["L1"]})
    assert added.status_code == 200
    removed = client.delete("/api/v1/shortlists/C1/L1", headers=auth_header())
    assert removed.status_code == 200
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM client_shortlists").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE id='L1'").fetchone()[0] == 1
    conn.close()
