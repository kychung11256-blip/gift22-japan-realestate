import base64
import json

import pytest

import db
import server
from viewing_planner import ViewingPlanError, decode_polyline, geometry_bounds, nearest_neighbor_order, optimize_driving, optimize_transit


def auth_header():
    token = base64.b64encode(b"johnny:test-password").decode("ascii")
    return {"Authorization": "Basic " + token}


def seed_viewing_data():
    conn = db.get_db()
    conn.execute("INSERT INTO clients (id, name, requirement_text) VALUES (?,?,?)", ("C1", "客人 A", "港區 2LDK"))
    rows = [
        ("L1", "東京都港区赤坂1丁目", "赤坂", 50000, 35.6720, 139.7360, "draft", "internal note 1"),
        ("L2", "東京都港区六本木1丁目", "六本木一丁目", 62000, 35.6650, 139.7390, "published", "internal note 2"),
        ("L3", "東京都中央区銀座1丁目", "銀座", 45000, 35.6719, 139.7648, "draft", "internal note 3"),
        ("L4", "東京都新宿区西新宿2丁目", "新宿", 30000, 0, 0, "draft", "missing coords"),
    ]
    for row in rows:
        conn.execute(
            """
            INSERT INTO listings (id, address, station, price, latitude, longitude, status, notes_freetext, room_layout, size_sqm, built_year)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*row, "2LDK", 70, 2015),
        )
    for lid in ("L1", "L2", "L4"):
        conn.execute("INSERT INTO client_shortlists (client_id, listing_id, search_query) VALUES (?,?,?)", ("C1", lid, "港區 2LDK"))
    conn.commit()
    conn.close()


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    database = tmp_path / "viewing.db"
    monkeypatch.setattr(db, "DB_PATH", str(database))
    monkeypatch.setenv("WORKBENCH_USER", "johnny")
    monkeypatch.setenv("WORKBENCH_PASSWORD", "test-password")
    db.init_db()
    seed_viewing_data()
    return server.app.test_client()


def mocked_route(origin, stops, departure, duration, google_get=None, optimize_waypoints=True, end_location=None):
    ordered = list(reversed(stops)) if optimize_waypoints else list(stops)
    minutes = [10 + i * 5 for i in range(len(ordered) + (1 if end_location else 0))]
    from viewing_planner import build_schedule
    return {
        "provider": "mock_google_directions",
        "heuristic": "provider_waypoint_optimization" if optimize_waypoints else "manual_order_recalculation",
        "routeGeometry": {"type": "LineString", "coordinates": [[origin["lon"], origin["lat"]]] + [[s["lon"], s["lat"]] for s in ordered]},
        "routeBounds": [min([origin["lon"]]+[s["lon"] for s in ordered]), min([origin["lat"]]+[s["lat"] for s in ordered]), max([origin["lon"]]+[s["lon"] for s in ordered]), max([origin["lat"]]+[s["lat"] for s in ordered])],
        **build_schedule([origin] + ordered, minutes, departure, duration, "driving", end_location=end_location),
    }


def google_directions_response(polyline="_p~iF~ps|U_ulLnnqC_mqNvxq`@"):
    return {
        "status": "OK",
        "routes": [{
            "waypoint_order": [],
            "overview_polyline": {"points": polyline},
            "legs": [{"duration": {"value": 600}, "duration_in_traffic": {"value": 720}}],
        }],
    }


def payload(listing_ids=None):
    return {
        "clientId": "C1",
        "listingIds": listing_ids or ["L1", "L2"],
        "viewingDate": "2026-09-02",
        "departureTime": "10:00",
        "start": {"label": "東京駅", "lat": 35.681236, "lon": 139.767125},
        "viewingDurationMin": 45,
        "travelMode": "driving",
    }


def test_auth_required_on_all_workbench_planner_apis(app_client):
    assert app_client.get("/api/v1/shortlists/C1").status_code == 401
    assert app_client.get("/api/v1/viewing-plans").status_code == 401
    assert app_client.post("/api/v1/viewing-plans/optimize", json=payload()).status_code == 401
    assert app_client.post("/api/v1/viewing-plans", json=payload()).status_code == 401
    assert app_client.get("/api/v1/viewing-plans/VP-NOPE").status_code == 401
    assert app_client.post("/api/v1/viewing-plans/VP-NOPE/share").status_code == 401
    assert app_client.delete("/api/v1/viewing-plans/VP-NOPE/share").status_code == 401


def test_shortlist_ownership_stop_count_and_missing_coordinate_validation(app_client, monkeypatch):
    monkeypatch.setattr(server, "optimize_driving", mocked_route)
    headers = auth_header()

    owned = app_client.get("/api/v1/shortlists/C1", headers=headers)
    assert owned.status_code == 200
    assert {x["id"] for x in owned.get_json()["items"]} == {"L1", "L2", "L4"}
    assert next(x for x in owned.get_json()["items"] if x["id"] == "L4")["routeEligible"] is False

    one_stop = app_client.post("/api/v1/viewing-plans/optimize", headers=headers, json=payload(["L1"]))
    assert one_stop.status_code == 400
    assert "2 至 8" in one_stop.get_json()["error"]

    wrong_client = app_client.post("/api/v1/viewing-plans/optimize", headers=headers, json=payload(["L1", "L3"]))
    assert wrong_client.status_code == 403
    assert wrong_client.get_json()["errorCode"] == "SHORTLIST_OWNERSHIP"

    missing_coords = app_client.post("/api/v1/viewing-plans/optimize", headers=headers, json=payload(["L1", "L4"]))
    assert missing_coords.status_code == 400
    assert missing_coords.get_json()["errorCode"] == "MISSING_COORDINATES"


def test_deterministic_transit_heuristic_and_provider_failure_no_fabrication():
    order = nearest_neighbor_order([
        [0, 90, 30, 60],
        [90, 0, 20, 70],
        [30, 20, 0, 25],
        [60, 70, 25, 0],
    ], 3)
    assert order == [1, 0, 2]

    origin = {"lat": 35.0, "lon": 139.0, "label": "start"}
    stops = [
        {"id": "A", "listingId": "A", "lat": 35.1, "lon": 139.1},
        {"id": "B", "listingId": "B", "lat": 35.2, "lon": 139.2},
    ]
    with pytest.raises(ViewingPlanError) as exc:
        optimize_transit(origin, stops, server.datetime.now(server.timezone.utc), 45, matrix_provider=lambda *_: [[0, 120, 0], [120, 0, 90], [0, 90, 0]])
    assert "唔會估算" in exc.value.message


def test_driving_polyline_decoding_and_geometry_serialization(monkeypatch):
    coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert coords == [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]
    monkeypatch.setattr('viewing_planner.GOOGLE_MAPS_API_KEY', 'configured-for-test')
    origin = {"lat": 38.5, "lon": -120.2, "label": "start"}
    stops = [{"id": "A", "listingId": "A", "lat": 40.7, "lon": -120.95}]
    result = optimize_driving(origin, stops, server.datetime.now(server.timezone.utc), 45, google_get=lambda url: google_directions_response(), optimize_waypoints=False)
    assert result["routeGeometry"]["type"] == "LineString"
    assert len(result["routeGeometry"]["coordinates"]) == 3
    assert result["routeBounds"] == geometry_bounds(result["routeGeometry"])
    assert result.get("warnings") == []


def test_transit_multileg_geometry_and_unavailable_warning(monkeypatch):
    monkeypatch.setattr('viewing_planner.GOOGLE_MAPS_API_KEY', 'configured-for-test')
    origin = {"lat": 35.0, "lon": 139.0, "label": "start"}
    stops = [
        {"id": "A", "listingId": "A", "lat": 35.1, "lon": 139.1},
        {"id": "B", "listingId": "B", "lat": 35.2, "lon": 139.2},
    ]
    calls = []
    def fake_get(url):
        calls.append(url)
        if 'distancematrix' in url:
            return {"status": "OK", "rows": [
                {"elements": [{"status":"OK","duration":{"value":0}}, {"status":"OK","duration":{"value":600}}, {"status":"OK","duration":{"value":900}}]},
                {"elements": [{"status":"OK","duration":{"value":600}}, {"status":"OK","duration":{"value":0}}, {"status":"OK","duration":{"value":500}}]},
                {"elements": [{"status":"OK","duration":{"value":900}}, {"status":"OK","duration":{"value":500}}, {"status":"OK","duration":{"value":0}}]},
            ]}
        return google_directions_response()
    monkeypatch.setattr('viewing_planner._google_get', fake_get)
    res = optimize_transit(origin, stops, server.datetime.now(server.timezone.utc), 45, matrix_provider=None, manual_order=True)
    assert res["routeGeometry"]["type"] == "MultiLineString"
    assert len(res["routeGeometry"]["coordinates"]) == 2
    assert len([u for u in calls if 'directions' in u]) == 2

    monkeypatch.setattr('viewing_planner._geometry_cache', {})
    def no_geom(url):
        if 'distancematrix' in url:
            return fake_get(url)
        return {"status": "ZERO_RESULTS", "routes": []}
    monkeypatch.setattr('viewing_planner._google_get', no_geom)
    res2 = optimize_transit(origin, stops, server.datetime.now(server.timezone.utc), 45, matrix_provider=None, manual_order=True)
    assert not res2.get("routeGeometry")
    assert "不會畫假路線" in res2["warnings"][0]["message"]


def test_save_reopen_share_revocation_regeneration_and_no_status_mutation(app_client, monkeypatch):
    monkeypatch.setattr(server, "optimize_driving", mocked_route)
    headers = auth_header()
    before_statuses = db.get_db().execute("SELECT id,status FROM listings ORDER BY id").fetchall()
    before_statuses = [(r["id"], r["status"]) for r in before_statuses]

    optimized = app_client.post("/api/v1/viewing-plans/optimize", headers=headers, json=payload()).get_json()
    assert optimized["stops"][0]["listingId"] == "L2"
    assert optimized["stops"][0]["travelMinutes"] == 10
    assert optimized["routeGeometry"]["type"] == "LineString"

    manual_body = payload()
    manual_body["manualOrder"] = ["L1", "L2"]
    manual_body["mockDurations"] = [10, 15]
    manual = app_client.post("/api/v1/viewing-plans/optimize", headers=headers, json=manual_body).get_json()
    assert manual["stops"][0]["listingId"] == "L1"
    assert manual["heuristic"] == "manual deterministic staging validation"
    assert manual.get("routeGeometry") is None
    assert "不會畫假路線" in json.dumps(manual["warnings"], ensure_ascii=False)

    manual_provider_body = payload()
    manual_provider_body["manualOrder"] = ["L1", "L2"]
    manual_provider = app_client.post("/api/v1/viewing-plans/optimize", headers=headers, json=manual_provider_body).get_json()
    assert manual_provider["stops"][0]["listingId"] == "L1"
    assert manual_provider["heuristic"] == "manual_order_recalculation"

    save_body = payload()
    save_body.update({"optimized": optimized, "departureAt": optimized["departureAt"], "title": "測試睇樓路線"})
    created = app_client.post("/api/v1/viewing-plans", headers=headers, json=save_body)
    assert created.status_code == 201
    plan = created.get_json()["plan"]
    assert plan["shareUrl"].startswith("/share/viewing/")
    assert len(plan["shareUrl"].split("/")[-1]) >= 32

    reopened = app_client.get(f"/api/v1/viewing-plans/{plan['id']}", headers=headers).get_json()["plan"]
    assert reopened["title"] == "測試睇樓路線"
    assert len(reopened["stops"]) == 2
    assert reopened["routeGeometry"]["type"] == "LineString"

    token = plan["shareUrl"].split("/")[-1]
    share_api = app_client.get(f"/api/share/viewing/{token}")
    assert share_api.status_code == 200
    assert share_api.headers["X-Robots-Tag"] == "noindex, nofollow"
    share_plan = share_api.get_json()["plan"]
    assert "clientId" not in share_plan
    assert "shareUrl" not in share_plan
    assert "notes_freetext" not in json.dumps(share_plan)
    assert all("property" in s and "notes" not in s["property"] for s in share_plan["stops"])

    share_page = app_client.get(f"/share/viewing/{token}")
    assert share_page.status_code == 200
    assert b"noindex,nofollow" in share_page.data
    assert share_page.headers["X-Robots-Tag"] == "noindex, nofollow"

    revoked = app_client.delete(f"/api/v1/viewing-plans/{plan['id']}/share", headers=headers).get_json()["plan"]
    assert revoked["shareUrl"] == ""
    assert app_client.get(f"/api/share/viewing/{token}").status_code == 404

    regenerated = app_client.post(f"/api/v1/viewing-plans/{plan['id']}/share", headers=headers).get_json()["plan"]
    assert regenerated["shareUrl"] and regenerated["shareUrl"] != plan["shareUrl"]

    after_statuses = db.get_db().execute("SELECT id,status FROM listings ORDER BY id").fetchall()
    after_statuses = [(r["id"], r["status"]) for r in after_statuses]
    assert after_statuses == before_statuses
