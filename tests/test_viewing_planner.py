import base64
import json

import pytest

import db
import server
import viewing_planner
from viewing_planner import ViewingPlanError, decode_polyline, geometry_bounds, google_transit_matrix, google_transit_route, nearest_neighbor_order, optimize_driving, optimize_transit, parse_date_time, parse_iso


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


def test_google_routes_matrix_parsing_transit_geometry_and_element_failure(monkeypatch):
    monkeypatch.setattr('viewing_planner.GOOGLE_MAPS_API_KEY', 'configured-for-test')
    origin = {"lat": 35.0, "lon": 139.0, "label": "start"}
    stops = [
        {"id": "A", "listingId": "A", "lat": 35.1, "lon": 139.1},
        {"id": "B", "listingId": "B", "lat": 35.2, "lon": 139.2},
    ]
    calls = []
    def fake_routes_post(path, payload, field_mask, timeout=25):
        calls.append({"path": path, "field_mask": field_mask, "payload": payload})
        assert 'key=' not in path
        assert 'maps.googleapis.com/maps/api/distancematrix' not in path.lower()
        assert 'directions/json' not in path.lower()
        if 'computeRouteMatrix' in path:
            return [
                {"originIndex": 0, "destinationIndex": 0},
                {"originIndex": 0, "destinationIndex": 1, "duration": "600s", "condition": "ROUTE_EXISTS"},
                {"originIndex": 0, "destinationIndex": 2, "duration": "900s", "condition": "ROUTE_EXISTS"},
                {"originIndex": 1, "destinationIndex": 0, "duration": "600s", "condition": "ROUTE_EXISTS"},
                {"originIndex": 1, "destinationIndex": 1},
                {"originIndex": 1, "destinationIndex": 2, "duration": "500s", "condition": "ROUTE_EXISTS"},
                {"originIndex": 2, "destinationIndex": 0, "duration": "900s", "condition": "ROUTE_EXISTS"},
                {"originIndex": 2, "destinationIndex": 1, "duration": "500s", "condition": "ROUTE_EXISTS"},
                {"originIndex": 2, "destinationIndex": 2},
            ]
        return {"routes": [{"duration": "600s", "polyline": {"encodedPolyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"}}]}
    monkeypatch.setattr('viewing_planner._google_routes_post', fake_routes_post)
    matrix = google_transit_matrix(origin, stops, server.datetime.now(server.timezone.utc))
    assert matrix[0][1] == 600
    assert matrix[1][2] == 500
    res = optimize_transit(origin, stops, server.datetime.now(server.timezone.utc), 45, matrix_provider=None, manual_order=True)
    assert res["provider"] == "google_routes_api"
    assert res["routeGeometry"]["type"] == "MultiLineString"
    assert len(res["routeGeometry"]["coordinates"]) == 2
    assert len([c for c in calls if 'computeRoutes' in c['path']]) == 2
    assert any('originIndex,destinationIndex,status,condition,duration,distanceMeters' == c['field_mask'] for c in calls)

    assert not any('maps.googleapis.com/maps/api/distancematrix' in str(c).lower() or 'directions/json' in str(c).lower() for c in calls)

    def bad_matrix(path, payload, field_mask, timeout=25):
        if 'computeRouteMatrix' in path:
            return [
                {"originIndex": 0, "destinationIndex": 0},
                {"originIndex": 0, "destinationIndex": 1, "status": {"code": 5, "message": "no transit"}},
                {"originIndex": 1, "destinationIndex": 0, "duration": "600s"},
                {"originIndex": 1, "destinationIndex": 1},
            ]
        return {}
    monkeypatch.setattr('viewing_planner._google_routes_post', bad_matrix)
    with pytest.raises(ViewingPlanError) as exc:
        google_transit_matrix(origin, stops[:1], server.datetime.now(server.timezone.utc))
    assert exc.value.code in {"PROVIDER_UNSUPPORTED_ROUTE", "PROVIDER_ERROR"}
    assert "Routes API Compute Route Matrix" in exc.value.message


def test_google_routes_compute_routes_polyline_parsing(monkeypatch):
    monkeypatch.setattr('viewing_planner.GOOGLE_MAPS_API_KEY', 'configured-for-test')
    def fake_routes_post(path, payload, field_mask, timeout=25):
        assert path.endswith('computeRoutes')
        assert field_mask == 'routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline'
        assert payload['travelMode'] == 'TRANSIT'
        assert payload['departureTime'].endswith('Z')
        return {"routes": [{"duration": "123s", "polyline": {"encodedPolyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"}}]}
    monkeypatch.setattr('viewing_planner._google_routes_post', fake_routes_post)
    viewing_planner._transit_route_cache.clear()
    geom = google_transit_route({"lat": 38.5, "lon": -120.2}, {"lat": 40.7, "lon": -120.95}, server.datetime.now(server.timezone.utc))
    assert geom["type"] == "LineString"
    assert geom["coordinates"][0] == [-120.2, 38.5]
    assert geom["durationSeconds"] == 123


def test_google_routes_matrix_route_not_found_uses_real_compute_routes(monkeypatch):
    monkeypatch.setattr("viewing_planner.GOOGLE_MAPS_API_KEY", "configured-for-test")
    viewing_planner._transit_route_cache.clear()
    calls = []
    def route_not_found_then_real_route(path, payload, field_mask, timeout=25):
        calls.append({"path": path, "payload": payload, "field_mask": field_mask})
        if "computeRouteMatrix" in path:
            return [
                {"originIndex": 0, "destinationIndex": 0},
                {"originIndex": 0, "destinationIndex": 1, "status": {"code": 5, "message": "ROUTE_NOT_FOUND"}, "condition": "ROUTE_NOT_FOUND"},
                {"originIndex": 1, "destinationIndex": 0, "status": {"code": 5, "message": "ROUTE_NOT_FOUND"}, "condition": "ROUTE_NOT_FOUND"},
                {"originIndex": 1, "destinationIndex": 1},
            ]
        assert path.endswith("computeRoutes")
        assert payload["travelMode"] == "TRANSIT"
        assert "routingPreference" not in payload
        assert "intermediates" not in payload
        assert "computeAlternativeRoutes" not in payload
        encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq" + chr(96) + "@"
        return {"routes": [{"duration": "420s", "distanceMeters": 8000, "polyline": {"encodedPolyline": encoded}}]}
    monkeypatch.setattr("viewing_planner._google_routes_post", route_not_found_then_real_route)
    departure = parse_iso("2026-09-03T01:00:00Z", "departureAt")
    origin = {"lat": 35.681236, "lon": 139.767125, "label": "東京駅"}
    stops = [{"id": "A", "listingId": "A", "lat": 35.692627, "lon": 139.688416}]
    matrix = google_transit_matrix(origin, stops, departure)
    assert matrix == [[0, 420], [420, 0]]
    assert len([c for c in calls if "computeRouteMatrix" in c["path"]]) == 1
    assert len([c for c in calls if "computeRoutes" in c["path"]]) == 2


def test_google_transit_request_denied_is_specific_config_error(monkeypatch):
    monkeypatch.setattr('viewing_planner.GOOGLE_MAPS_API_KEY', 'configured-for-test')
    def denied(path, payload, field_mask, timeout=25):
        raise ViewingPlanError('Google Routes API 拒絕請求（PERMISSION_DENIED）：API key 未獲授權使用 Routes API。', 502, 'PROVIDER_CONFIG_ERROR')
    monkeypatch.setattr('viewing_planner._google_routes_post', denied)
    origin = {"lat": 35.0, "lon": 139.0, "label": "start"}
    stops = [{"id": "A", "listingId": "A", "lat": 35.1, "lon": 139.1}]
    with pytest.raises(ViewingPlanError) as exc:
        google_transit_matrix(origin, stops, server.datetime.now(server.timezone.utc))
    assert exc.value.status == 502
    assert exc.value.code == "PROVIDER_CONFIG_ERROR"
    assert "Routes API" in exc.value.message
    assert "API key 未獲授權" in exc.value.message


def test_japan_timezone_conversion_regression():
    dt = parse_date_time("2026-09-02", "10:00")
    assert dt.tzinfo is not None
    assert dt.isoformat() == "2026-09-02T01:00:00+00:00"
    assert parse_iso("2026-09-02T10:00:00", "departureAt").isoformat() == "2026-09-02T01:00:00+00:00"
    assert parse_iso("2026-09-02T10:00:00+09:00", "departureAt").isoformat() == "2026-09-02T01:00:00+00:00"


def test_planner_unexpected_exception_returns_json_envelope(app_client, monkeypatch):
    headers = auth_header()
    def boom(_data):
        raise RuntimeError("simulated broken provider")
    monkeypatch.setattr(server, "_optimize_payload", boom)
    resp = app_client.post("/api/v1/viewing-plans/optimize", headers={**headers, "X-Request-ID": "TEST-REQ-500"}, json=payload())
    assert resp.status_code == 500
    assert resp.content_type.startswith("application/json")
    data = resp.get_json()
    assert data["code"] == 0
    assert data["errorCode"] == "INTERNAL_ERROR"
    assert data["requestId"] == "TEST-REQ-500"
    assert "TEST-REQ-500" in data["error"]


def test_planner_404_405_json_envelope(app_client):
    headers = auth_header()
    missing = app_client.get("/api/v1/viewing-plans/VP-NOPE", headers=headers)
    assert missing.status_code == 404
    assert missing.content_type.startswith("application/json")
    assert missing.get_json()["errorCode"] == "NOT_FOUND"
    method = app_client.put("/api/v1/viewing-plans/optimize", headers=headers, json=payload())
    assert method.status_code == 405
    assert method.content_type.startswith("application/json")
    assert method.get_json()["errorCode"] == "METHOD_NOT_ALLOWED"


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
