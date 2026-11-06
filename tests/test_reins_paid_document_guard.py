import base64
import io
import os
import secrets
import sys
import types

import pytest

import db
import reins_client
import reins_document_cache as cache
import server


_TEST_USER = "fixture-" + secrets.token_hex(8)
_TEST_PASSWORD = secrets.token_urlsafe(24)


def auth_header():
    token = base64.b64encode(f"{_TEST_USER}:{_TEST_PASSWORD}".encode()).decode("ascii")
    return {"Authorization": "Basic " + token}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "listings.db"))
    monkeypatch.setattr(cache, "__file__", str(tmp_path / "reins_document_cache.py"))
    monkeypatch.setenv("WORKBENCH_USER", _TEST_USER)
    monkeypatch.setenv("WORKBENCH_PASSWORD", _TEST_PASSWORD)
    monkeypatch.delenv("REINS_PAID_DOWNLOAD_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("REINS_PAID_DOWNLOAD_MONTHLY_LIMIT", raising=False)
    db.init_db()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO listings (id,address,price,status,source,reins_id,reins_updated_at) VALUES (?,?,?,?,?,?,?)",
        ("L1", "東京都港区", 5000, "draft", "reins", "100000000001", "2026-11-06"),
    )
    conn.execute(
        "INSERT INTO listings (id,address,price,status,source,reins_id,reins_updated_at) VALUES (?,?,?,?,?,?,?)",
        ("L2", "東京都新宿区", 4500, "draft", "reins", "100000000002", "v1"),
    )
    conn.commit()
    conn.close()
    return server.app.test_client(), tmp_path


def test_request_is_idempotent_and_never_downloads(isolated, monkeypatch):
    client, _ = isolated
    called = []
    monkeypatch.setattr(reins_client, "download_drawing_pdf", lambda *a, **k: called.append(1))
    body = {"upstreamVersion": "2026-11-06", "expectedCostYen": 500}
    first = client.post(
        "/api/reins-documents/100000000001/drawing/request",
        headers={**auth_header(), "Idempotency-Key": "same-request"}, json=body,
    )
    second = client.post(
        "/api/reins-documents/100000000001/drawing/request",
        headers={**auth_header(), "Idempotency-Key": "same-request"}, json=body,
    )
    assert first.status_code == second.status_code == 200
    assert first.get_json()["automaticDownload"] is False
    assert second.get_json()["document"]["id"] == first.get_json()["document"]["id"]
    assert called == []
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM reins_document_cache").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM reins_document_ledger").fetchone()[0] == 1
    conn.close()


def test_uploaded_pdf_is_cached_and_reused(isolated):
    client, _ = isolated
    payload = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    data = {"upstreamVersion": "2026-11-06", "costYen": "500", "file": (io.BytesIO(payload), "drawing.pdf")}
    first = client.post(
        "/api/reins-documents/100000000001/drawing/upload",
        headers=auth_header(), data=data, content_type="multipart/form-data",
    )
    assert first.status_code == 200
    assert first.get_json()["document"]["reused"] is False
    second = client.post(
        "/api/reins-documents/100000000001/drawing/upload",
        headers=auth_header(),
        data={"upstreamVersion": "2026-11-06", "costYen": "500", "file": (io.BytesIO(payload), "drawing.pdf")},
        content_type="multipart/form-data",
    )
    assert second.status_code == 200
    assert second.get_json()["document"]["reused"] is True
    status = client.get(
        "/api/reins-documents/100000000001/drawing/status?version=2026-11-06",
        headers=auth_header(),
    ).get_json()["document"]
    assert status["status"] == "ready"
    assert status["file_valid"] is True
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM reins_document_ledger WHERE event_type='manual_purchase_uploaded'").fetchone()[0] == 1
    conn.close()


def test_new_upstream_version_requires_explicit_confirmation(isolated):
    client, _ = isolated
    payload = b"%PDF-1.4\n%%EOF\n"
    ready = client.post(
        "/api/reins-documents/100000000001/drawing/upload", headers=auth_header(),
        data={"upstreamVersion": "v1", "file": (io.BytesIO(payload), "drawing.pdf")},
        content_type="multipart/form-data",
    )
    assert ready.status_code == 200
    blocked = client.post(
        "/api/reins-documents/100000000001/drawing/request", headers=auth_header(),
        json={"upstreamVersion": "v2"},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "REINS_VERSION_CONFIRMATION_REQUIRED"
    allowed = client.post(
        "/api/reins-documents/100000000001/drawing/request", headers=auth_header(),
        json={"upstreamVersion": "v2", "confirmLatest": True},
    )
    assert allowed.status_code == 200
    assert allowed.get_json()["document"]["status"] == "requested"


def test_budget_cap_blocks_new_request(isolated, monkeypatch):
    client, _ = isolated
    payload = b"%PDF-1.4\n%%EOF\n"
    assert client.post(
        "/api/reins-documents/100000000001/drawing/upload", headers=auth_header(),
        data={"upstreamVersion": "v1", "file": (io.BytesIO(payload), "drawing.pdf")},
        content_type="multipart/form-data",
    ).status_code == 200
    monkeypatch.setenv("REINS_PAID_DOWNLOAD_DAILY_LIMIT", "1")
    blocked = client.post(
        "/api/reins-documents/100000000002/drawing/request", headers=auth_header(),
        json={"upstreamVersion": "v1"},
    )
    assert blocked.status_code == 429
    assert blocked.get_json()["error"] == "REINS_DAILY_BUDGET_REACHED"


def test_import_never_calls_paid_drawing_download(monkeypatch):
    monkeypatch.setattr(reins_client, "download_overview_pdf", lambda *a, **k: ("/uploads/reins/X/overview.pdf", None))
    monkeypatch.setattr(reins_client.os.path, "exists", lambda p: True)
    monkeypatch.setattr(reins_client, "parse_overview_pdf", lambda p: {
        "reins_id": "X", "price": 5000, "address": "東京都港区", "latest_update_date": "v1"
    })
    monkeypatch.setattr(reins_client, "_upsert_reins_listing", lambda *a, **k: ("LX", "inserted"))
    monkeypatch.setattr(cache, "get_cached_drawing", lambda *a, **k: "")
    called = []
    monkeypatch.setattr(reins_client, "download_drawing_pdf", lambda *a, **k: called.append(1))
    fake_geo = types.SimpleNamespace(geocode=lambda address: (35.0, 139.0))
    monkeypatch.setitem(sys.modules, "geocode_client", fake_geo)
    result = reins_client.import_reins_listing("X", drawing_available=True)
    assert result["code"] == 1
    assert result["drawing_deferred"] is True
    assert result["drawing_pdf"] == ""
    assert called == []


def test_routes_are_staff_only_and_ui_explains_manual_flow(isolated):
    client, _ = isolated
    assert client.get("/reins-documents").status_code == 401
    assert client.get("/reins-documents", headers=auth_header()).status_code == 200
    assert client.get("/api/reins-documents/100000000001/drawing/status").status_code == 401
    html = open(os.path.join(os.path.dirname(server.__file__), "reins_documents.html"), encoding="utf-8").read()
    assert "平台唔會自動向 REINS 下載付費圖紙" in html
    assert "建立手動購買請求" in html
