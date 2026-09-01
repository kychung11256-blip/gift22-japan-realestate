import base64
import io
import json
from pathlib import Path

from PIL import Image

import db
import server


def auth_header():
    token = base64.b64encode(b"johnny:test-password").decode("ascii")
    return {"Authorization": "Basic " + token}


def _make_jpeg(path, size, color):
    img = Image.new("RGB", size, color)
    # Add deterministic texture so the MVP classifier treats it as a photo-like
    # embedded image rather than a flat colour block.
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            if (x // 13 + y // 9) % 3 == 0:
                px[x, y] = tuple(min(255, c + ((x * y) % 80)) for c in color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _make_pdf(path: Path):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    # Full-page light line-art raster should be excluded as composite page.
    full = Image.new("RGB", (1200, 800), "white")
    px = full.load()
    for x in range(0, 1200, 80):
        for y in range(800):
            px[x, y] = (30, 30, 30)
    for y in range(0, 800, 80):
        for x in range(1200):
            px[x, y] = (30, 30, 30)
    full_buf = io.BytesIO()
    full.save(full_buf, format="JPEG", quality=85)
    page.insert_image(pymupdf.Rect(0, 0, 600, 400), stream=full_buf.getvalue())
    # Two photo-like embedded images.
    page.insert_image(pymupdf.Rect(20, 120, 220, 240), stream=_make_jpeg(path, (350, 210), (110, 90, 70)))
    page.insert_image(pymupdf.Rect(240, 120, 440, 240), stream=_make_jpeg(path, (350, 210), (80, 120, 150)))
    doc.save(str(path))
    doc.close()


def _setup(tmp_path, monkeypatch):
    database = tmp_path / "listings.db"
    monkeypatch.setattr(db, "DB_PATH", str(database))
    monkeypatch.setenv("WORKBENCH_USER", "johnny")
    monkeypatch.setenv("WORKBENCH_PASSWORD", "test-password")
    monkeypatch.setattr(server, "WORKBENCH_USER", "johnny", raising=False)
    db.init_db()
    upload_dir = Path(server.__file__).resolve().parent / "uploads" / "reins" / "TESTREINS"
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf = upload_dir / "drawing.pdf"
    _make_pdf(pdf)
    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO listings (id, address, price, status, source, reins_drawing_pdf, interior_photos, staged_photos, photos)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        ("REINS-TEST-1", "東京都新宿区", 30000, "draft", "reins", "/uploads/reins/TESTREINS/drawing.pdf", "[]", "[]", "[]"),
    )
    conn.commit()
    conn.close()
    return server.app.test_client()


def test_reins_photo_preview_auth_and_no_db_write(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    denied = client.post("/api/reins-photo-preview/REINS-TEST-1")
    assert denied.status_code == 401

    ok = client.post("/api/reins-photo-preview/REINS-TEST-1", headers=auth_header())
    assert ok.status_code == 200
    data = ok.get_json()
    assert data["code"] == 1
    assert data["candidate_count"] >= 2
    assert data["included_count"] >= 1
    assert any(c["classification"] == "interior_photo" and not c["excluded"] for c in data["candidates"])
    assert any(c["classification"] == "composite_page" and c["excluded"] for c in data["candidates"])

    conn = db.get_db()
    row = conn.execute("SELECT interior_photos FROM listings WHERE id='REINS-TEST-1'").fetchone()
    conn.close()
    assert json.loads(row[0]) == []


def test_reins_photo_confirm_idempotent_and_rejects_bad_candidate(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    preview = client.post("/api/reins-photo-preview/REINS-TEST-1", headers=auth_header()).get_json()
    cand = next(c for c in preview["candidates"] if c["classification"] == "interior_photo" and not c["excluded"])

    bad = client.post(
        "/api/reins-photo-confirm/REINS-TEST-1",
        headers=auth_header(),
        json={"candidates": [{"id": "../../etc/passwd", "room_label": "客廳"}]},
    )
    assert bad.status_code == 400

    ok = client.post(
        "/api/reins-photo-confirm/REINS-TEST-1",
        headers=auth_header(),
        json={"candidates": [{"id": cand["id"], "room_label": "客廳"}]},
    )
    assert ok.status_code == 200
    assert ok.get_json()["confirmed"] == 1

    again = client.post(
        "/api/reins-photo-confirm/REINS-TEST-1",
        headers=auth_header(),
        json={"candidates": [{"id": cand["id"], "room_label": "客廳"}]},
    )
    assert again.status_code == 200
    assert again.get_json()["confirmed"] == 0

    conn = db.get_db()
    row = conn.execute("SELECT interior_photos, photos, floorplan_images, staged_photos FROM listings WHERE id='REINS-TEST-1'").fetchone()
    conn.close()
    interior = json.loads(row[0])
    assert len(interior) == 1
    assert interior[0]["source"] == "reins_drawing"
    assert interior[0]["room_label"] == "客廳"
    assert row[1] == "[]"
    assert row[2] == "[]"
    assert row[3] == "[]"


def test_reins_photo_preview_wrong_listing_404(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    missing = client.post("/api/reins-photo-preview/NOPE", headers=auth_header())
    assert missing.status_code == 404
