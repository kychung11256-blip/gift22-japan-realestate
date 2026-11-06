import base64
import secrets
from pathlib import Path

import db
import server


_TEST_USER = "fixture-" + secrets.token_hex(8)
_TEST_PASSWORD = secrets.token_urlsafe(24)


def auth_header():
    token = base64.b64encode(f"{_TEST_USER}:{_TEST_PASSWORD}".encode()).decode("ascii")
    return {"Authorization": "Basic " + token}


def setup_app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "security.db"))
    monkeypatch.setenv("WORKBENCH_USER", _TEST_USER)
    monkeypatch.setenv("WORKBENCH_PASSWORD", _TEST_PASSWORD)
    server.app.config.update(TESTING=True)
    db.init_db()
    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO listings (
            id, address, price, status, source, reins_id, listing_agent_name,
            license_number, brokerage_type, notes_freetext, photos,
            interior_photos, floorplan_images, staged_photos, reins_drawing_pdf
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "REINS-PUBLIC-1", "東京都港区", 8000, "published", "reins",
            "100000000001", "上游中介", "免許123", "専任", "電話 03-0000-0000",
            '["/uploads/reins/raw.jpg","/uploads/staged/safe.jpg"]',
            '["/uploads/reins-extracted/REINS-PUBLIC-1/raw.jpg"]',
            '["/uploads/reins/REINS-PUBLIC-1/drawing.jpg"]',
            '["/uploads/staged/safe-stage.jpg"]',
            "/uploads/reins/100000000001/drawing.pdf",
        ),
    )
    conn.execute(
        "INSERT INTO listings (id,address,price,status,source) VALUES (?,?,?,?,?)",
        ("DRAFT-PRIVATE-1", "東京都新宿区", 6000, "draft", "reins"),
    )
    conn.commit()
    conn.close()
    return server.app.test_client()


def test_internal_pages_and_mutations_fail_closed(tmp_path, monkeypatch):
    client = setup_app(tmp_path, monkeypatch)
    for path in ("/collection", "/upload", "/review", "/mysok", "/collection.html"):
        response = client.get(path)
        assert response.status_code in (401, 404), path

    calls = [
        ("post", "/api/nl-search", {"query": "港区 8000万円以下"}),
        ("post", "/api/collection/search", {"source": "reins", "city": "港区"}),
        ("post", "/api/collection/import", {"source": "reins", "items": []}),
        ("post", "/api/update/REINS-PUBLIC-1", {"price": 1}),
        ("delete", "/api/listing/REINS-PUBLIC-1", None),
        ("post", "/api/confirm/REINS-PUBLIC-1", {}),
        ("post", "/api/ai-generate", {}),
    ]
    for method, path, payload in calls:
        response = getattr(client, method)(path, json=payload)
        assert response.status_code == 401, path

    conn = db.get_db()
    assert conn.execute("SELECT price FROM listings WHERE id='REINS-PUBLIC-1'").fetchone()[0] == 8000
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
    conn.close()


def test_public_ui_defaults_to_no_internal_controls():
    root = Path(__file__).resolve().parents[1]
    listings = (root / "listings.html").read_text(encoding="utf-8")
    detail = (root / "listing.html").read_text(encoding="utf-8")
    assert 'id="internalCollectionNav"' in listings and 'class="hidden ' in listings
    assert 'id="internalSourceFilters" class="hidden ' in listings
    assert "${canManage ? `<span" in listings
    assert "安全圖片準備中" in listings
    assert "const canManage = !!p.canManage" in detail
    assert "${canManage ? `<!-- AI 效果圖" in detail
    assert "${canManage ? `<button onclick=\"deleteListing" in detail

def test_public_listing_projection_hides_upstream_broker_and_raw_media(tmp_path, monkeypatch):
    client = setup_app(tmp_path, monkeypatch)

    listing = client.get("/api/listing/REINS-PUBLIC-1")
    assert listing.status_code == 200
    public = listing.get_json()
    for key in (
        "source", "reins_id", "listing_agent_name", "license_number",
        "brokerage_type", "notes_freetext", "reins_drawing_pdf",
    ):
        assert key not in public
    assert public["photos"] == ["/uploads/staged/safe.jpg"]
    assert public["interior_photos"] == []
    assert public["floorplan_images"] == []
    assert public["staged_photos"] == ["/uploads/staged/safe-stage.jpg"]
    assert public["canManage"] is False
    assert client.get("/api/listing/DRAFT-PRIVATE-1").status_code == 404

    collection = client.get("/api/listings").get_json()
    assert collection["canManage"] is False
    assert collection["count"] == 1
    assert collection["listings"][0]["photos"] == ["/uploads/staged/safe.jpg"]


def test_admin_keeps_full_listing_and_internal_routes(tmp_path, monkeypatch):
    client = setup_app(tmp_path, monkeypatch)
    headers = auth_header()

    assert client.get("/collection", headers=headers).status_code == 200
    detail = client.get("/api/listing/REINS-PUBLIC-1", headers=headers).get_json()
    assert detail["canManage"] is True
    assert detail["listing_agent_name"] == "上游中介"
    assert detail["reins_id"] == "100000000001"
    assert client.get("/api/listing/DRAFT-PRIVATE-1", headers=headers).status_code == 200
    assert client.get("/api/listings", headers=headers).get_json()["count"] == 2


def test_raw_reins_and_mysok_media_require_auth(tmp_path, monkeypatch):
    client = setup_app(tmp_path, monkeypatch)
    assert client.get("/uploads/reins/anything.pdf").status_code == 401
    assert client.get("/uploads/reins-extracted/REINS-PUBLIC-1/raw.jpg").status_code == 401
    assert client.get("/uploads/mysok/anything.jpg").status_code == 401

