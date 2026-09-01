import base64

import db
import server


def auth_header():
    token = base64.b64encode(b"johnny:test-password").decode("ascii")
    return {"Authorization": "Basic " + token}


def test_client_shortlist_is_private_and_visible_in_workbench(tmp_path, monkeypatch):
    database = tmp_path / "shortlist.db"
    monkeypatch.setattr(db, "DB_PATH", str(database))
    monkeypatch.setenv("WORKBENCH_USER", "johnny")
    monkeypatch.setenv("WORKBENCH_PASSWORD", "test-password")
    db.init_db()

    conn = db.get_db()
    conn.execute(
        "INSERT INTO listings (id, address, price, status) VALUES (?,?,?,?)",
        ("P-CLIENT-1", "東京都新宿区", 30000, "draft"),
    )
    conn.commit()
    conn.close()

    client = server.app.test_client()
    denied = client.post("/api/v1/clients", json={"name": "客人 A"})
    assert denied.status_code == 401

    created = client.post(
        "/api/v1/clients",
        headers=auth_header(),
        json={"name": "客人 A", "requirementText": "新宿區 3億以下 朝南"},
    )
    assert created.status_code == 201
    client_id = created.get_json()["client"]["id"]

    added = client.post(
        "/api/v1/shortlists",
        headers=auth_header(),
        json={
            "clientId": client_id,
            "listingId": "P-CLIENT-1",
            "searchQuery": "新宿區 3億以下 朝南",
        },
    )
    assert added.status_code == 200

    properties = client.get(
        "/api/v1/properties?status=all&page_size=100",
        headers=auth_header(),
    ).get_json()["items"]
    item = next(p for p in properties if p["id"] == "P-CLIENT-1")
    assert item["clientAssignments"][0]["name"] == "客人 A"
    assert item["clientAssignments"][0]["searchQuery"] == "新宿區 3億以下 朝南"

    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM client_shortlists").fetchone()[0] == 1
    conn.close()
