"""Gift22 viewing route planner helpers.

MVP route policy:
- Driving/taxi: Google Directions API with provider waypoint optimization when a
  configured API key is available. We never fabricate durations on provider
  failure.
- Public transit: Google Routes API v2 Compute Route Matrix for time-aware leg
  durations, then a deterministic nearest-neighbour + 2-opt heuristic over the
  provider matrix. Routes API v2 Compute Routes is called separately for each
  optimized transit leg because transit routes do not support intermediate
  waypoints. If any matrix element or leg is unsupported/unavailable, return an
  explicit error.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import secrets
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
JAPAN_TZ = ZoneInfo("Asia/Tokyo")
_geometry_cache: dict[str, dict[str, Any]] = {}
_transit_route_cache: dict[str, dict[str, Any]] = {}

ALLOWED_DURATIONS = {30, 45, 60}
ALLOWED_MODES = {"driving", "transit"}


class ViewingPlanError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "VALIDATION_ERROR"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def parse_iso(value: str, field: str) -> datetime:
    if not value or not isinstance(value, str):
        raise ViewingPlanError(f"{field} 必填", 400)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        raise ViewingPlanError(f"{field} 格式不正確，請使用 ISO timestamp", 400)
    if dt.tzinfo is None:
        # Agent-entered naive timestamps are Japan viewing time, not UTC.
        dt = dt.replace(tzinfo=JAPAN_TZ)
    return dt.astimezone(timezone.utc)


def parse_date_time(viewing_date: str, departure_time: str) -> datetime:
    if not viewing_date or not departure_time:
        raise ViewingPlanError("睇樓日期同出發時間必填", 400)
    try:
        raw = f"{viewing_date}T{departure_time}"
        dt = datetime.fromisoformat(raw)
    except Exception:
        raise ViewingPlanError("睇樓日期／出發時間格式不正確", 400)
    # UI date/time is Japan viewing time. Convert to true UTC for Google/storage.
    return dt.replace(tzinfo=JAPAN_TZ).astimezone(timezone.utc)


def coord(value: Any, field: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ViewingPlanError(f"{field} 座標必須係數字", 400)
    if field.endswith("lat") and not -90 <= v <= 90:
        raise ViewingPlanError(f"{field} 座標超出範圍", 400)
    if field.endswith("lon") and not -180 <= v <= 180:
        raise ViewingPlanError(f"{field} 座標超出範圍", 400)
    return v


def validate_stop_count(listing_ids: list[str]) -> None:
    if not isinstance(listing_ids, list):
        raise ViewingPlanError("listingIds 必須係列表", 400)
    if len(listing_ids) < 2 or len(listing_ids) > 8:
        raise ViewingPlanError("請選擇 2 至 8 個睇樓房源", 400)
    if len(set(listing_ids)) != len(listing_ids):
        raise ViewingPlanError("睇樓房源不可重複", 400)


def make_share_token() -> str:
    return secrets.token_urlsafe(32)


def public_property(row: dict[str, Any]) -> dict[str, Any]:
    """Client-safe fields only; no notes, agent/license/internal workflow fields."""
    price = row.get("price") or 0
    if price and price < 10_000_000:
        price_yen = int(price) * 10000
    else:
        price_yen = int(price or 0)
    return {
        "id": row.get("id"),
        "title": row.get("building_name") or row.get("address") or row.get("id"),
        "address": row.get("address") or "",
        "station": row.get("station") or "",
        "walkMinutes": int(row.get("walk_min") or 0),
        "priceYen": price_yen,
        "areaSqm": float(row.get("size_sqm") or 0),
        "layout": row.get("room_layout") or "",
        "floor": int(row.get("floor") or 0) or None,
        "totalFloors": int(row.get("total_floors") or row.get("floors_above") or 0) or None,
        "builtAt": row.get("built_date_full") or (f"{int(row.get('built_year') or 0)}年" if row.get("built_year") else ""),
        "latitude": float(row.get("latitude") or 0),
        "longitude": float(row.get("longitude") or 0),
        "url": f"/listing/{urllib.parse.quote(str(row.get('id') or ''))}",
    }


def nav_url(origin: dict[str, Any], dest: dict[str, Any], mode: str) -> str:
    travelmode = "driving" if mode == "driving" else "transit"
    origin_q = f"{origin['lat']},{origin['lon']}"
    dest_q = f"{dest['lat']},{dest['lon']}"
    return "https://www.google.com/maps/dir/?api=1&" + urllib.parse.urlencode({
        "origin": origin_q,
        "destination": dest_q,
        "travelmode": travelmode,
    })


def decode_polyline(encoded: str) -> list[list[float]]:
    """Decode Google's encoded polyline into GeoJSON [lng, lat] coordinates."""
    if not encoded:
        return []
    points: list[list[float]] = []
    index = lat = lng = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result = shift = 0
            while True:
                if index >= len(encoded):
                    raise ViewingPlanError("Google polyline 格式不完整；不會建立假 geometry。", 502, "PROVIDER_ERROR")
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if (result & 1) else (result >> 1))
        lat += deltas[0]
        lng += deltas[1]
        points.append([lng / 1e5, lat / 1e5])
    return points


def _flatten_geometry(geometry: dict[str, Any]) -> list[list[float]]:
    if not geometry:
        return []
    if geometry.get("type") == "LineString":
        return geometry.get("coordinates") or []
    if geometry.get("type") == "MultiLineString":
        return [c for line in (geometry.get("coordinates") or []) for c in line]
    if geometry.get("type") == "Feature":
        return _flatten_geometry(geometry.get("geometry") or {})
    if geometry.get("type") == "FeatureCollection":
        return [c for f in (geometry.get("features") or []) for c in _flatten_geometry(f.get("geometry") or {})]
    return []


def geometry_bounds(geometry: dict[str, Any] | None) -> list[float]:
    coords = [c for c in _flatten_geometry(geometry or {}) if isinstance(c, list) and len(c) >= 2]
    if not coords:
        return []
    xs = [float(c[0]) for c in coords]
    ys = [float(c[1]) for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def _directions_geometry(origin: dict[str, Any], destination: dict[str, Any], mode: str, departure: datetime, google_get: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if not GOOGLE_MAPS_API_KEY:
        return None
    google_get = google_get or _google_get
    key = f"{mode}|{round(float(origin['lat']),5)},{round(float(origin['lon']),5)}|{round(float(destination['lat']),5)},{round(float(destination['lon']),5)}|{int(departure.timestamp())//21600}"
    if key in _geometry_cache:
        return _geometry_cache[key]
    params = {
        "origin": f"{origin['lat']},{origin['lon']}",
        "destination": f"{destination['lat']},{destination['lon']}",
        "mode": mode,
        "departure_time": str(int(departure.timestamp())),
        "language": "zh-TW",
        "key": GOOGLE_MAPS_API_KEY,
    }
    url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params, safe="|,:")
    g = google_get(url)
    if g.get("status") != "OK" or not g.get("routes"):
        return None
    overview = (g["routes"][0].get("overview_polyline") or {}).get("points") or ""
    coords = decode_polyline(overview) if overview else []
    geom = {"type": "LineString", "coordinates": coords} if coords else None
    if geom:
        _geometry_cache[key] = geom
    return geom


def build_schedule(order: list[dict[str, Any]], leg_minutes: list[int], departure: datetime, duration_min: int, mode: str, end_location: dict[str, Any] | None = None) -> dict[str, Any]:
    stops = []
    current = departure
    total_travel = 0
    prev = {"lat": order[0]["lat"], "lon": order[0]["lon"], "label": order[0].get("label") or order[0].get("address") or "出發點"}
    for i, stop in enumerate(order[1:], start=1):
        travel = int(math.ceil(float(leg_minutes[i - 1])))
        total_travel += travel
        depart_at = current
        arrive_at = depart_at + timedelta(minutes=travel)
        view_end = arrive_at + timedelta(minutes=duration_min)
        stop = dict(stop)
        stop.update({
            "seq": i,
            "travelMinutes": travel,
            "departAt": depart_at.isoformat(),
            "arriveAt": arrive_at.isoformat(),
            "viewingStartAt": arrive_at.isoformat(),
            "viewingEndAt": view_end.isoformat(),
            "navigationUrl": nav_url(prev, stop, mode),
        })
        stops.append(stop)
        current = view_end
        prev = stop
    end_leg = None
    if end_location and len(leg_minutes) > len(stops):
        travel = int(math.ceil(float(leg_minutes[len(stops)])))
        total_travel += travel
        end_arrive = current + timedelta(minutes=travel)
        end_leg = {
            "label": end_location.get("label") or end_location.get("address") or "終點",
            "lat": end_location.get("lat"),
            "lon": end_location.get("lon"),
            "travelMinutes": travel,
            "departAt": current.isoformat(),
            "arriveAt": end_arrive.isoformat(),
            "navigationUrl": nav_url(prev, end_location, mode),
        }
        current = end_arrive
    return {
        "stops": stops,
        "totalTravelMinutes": total_travel,
        "totalItineraryMinutes": int((current - departure).total_seconds() // 60),
        "departureAt": departure.isoformat(),
        "finishAt": current.isoformat(),
        "endLeg": end_leg,
    }


def _google_get(url: str, timeout: int = 25) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Gift22-viewing-planner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _google_routes_post(path: str, payload: dict[str, Any], field_mask: str, timeout: int = 25) -> Any:
    """Call Google Routes API v2 with key in header and explicit field mask.

    The URL never contains credentials. Callers must not log headers/payload.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://routes.googleapis.com/" + path.lstrip("/"),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Gift22-viewing-planner/1.0",
            "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": field_mask,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"error": {"status": f"HTTP_{e.code}", "message": e.reason or "HTTP error"}}
        err = data.get("error") if isinstance(data, dict) else {}
        status = (err or {}).get("status") or f"HTTP_{e.code}"
        detail = (err or {}).get("message") or e.reason or ""
        msg, code = _provider_error_message("Routes API", status, detail)
        raise ViewingPlanError(msg, 502 if e.code < 500 else 503, code)
    return json.loads(raw) if raw.strip() else {}


def _provider_error_message(service: str, status: str | None, detail: str = "") -> tuple[str, str]:
    status = status or "UNKNOWN"
    detail = (detail or "").strip()
    if status in {"REQUEST_DENIED", "PERMISSION_DENIED", "API_KEY_SERVICE_BLOCKED", "SERVICE_DISABLED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
        return (
            f"Google {service} 拒絕請求（{status}）：staging 使用緊嘅 Google API key 未獲授權使用 {service}。請在 Google Cloud Console 啟用 Routes API，並在 API key restrictions 加入 Routes API；毋須改房源資料。",
            "PROVIDER_CONFIG_ERROR",
        )
    if status in {"OVER_QUERY_LIMIT", "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"}:
        return (f"Google {service} 配額／限流（{status}）：請檢查 Google Cloud quota 或 billing；系統唔會估算時間。", "PROVIDER_QUOTA")
    if status in {"ZERO_RESULTS", "NOT_FOUND", "ROUTE_NOT_FOUND", "NO_ROUTE_FOUND", "FAILED_PRECONDITION"}:
        return (f"Google {service} 未支援其中一段路線（{status}）；系統唔會估算時間或畫假路線。", "PROVIDER_UNSUPPORTED_ROUTE")
    extra = f"：{detail}" if detail else ""
    return (f"Google {service} 回應錯誤（{status}）{extra}；系統唔會估算時間。", "PROVIDER_ERROR")


def optimize_driving(origin: dict[str, Any], stops: list[dict[str, Any]], departure: datetime, duration_min: int, google_get: Callable[[str], dict[str, Any]] | None = None, optimize_waypoints: bool = True, end_location: dict[str, Any] | None = None) -> dict[str, Any]:
    if not GOOGLE_MAPS_API_KEY:
        raise ViewingPlanError("GOOGLE_MAPS_API_KEY 未設定；未能使用 Google Directions waypoint optimization，唔會估算行車時間。", 503, "PROVIDER_NOT_CONFIGURED")
    google_get = google_get or _google_get
    origin_s = f"{origin['lat']},{origin['lon']}"
    dest = end_location or stops[-1]
    middle = stops if end_location else stops[:-1]
    params = {
        "origin": origin_s,
        "destination": f"{dest['lat']},{dest['lon']}",
        "mode": "driving",
        "departure_time": str(int(departure.timestamp())),
        "traffic_model": "best_guess",
        "language": "zh-TW",
        "key": GOOGLE_MAPS_API_KEY,
    }
    if middle:
        prefix = "optimize:true|" if optimize_waypoints else ""
        params["waypoints"] = prefix + "|".join(f"{s['lat']},{s['lon']}" for s in middle)
    url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params, safe="|,:" )
    try:
        g = google_get(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ViewingPlanError(f"Google Directions upstream 回應異常／非 JSON（{type(e).__name__}）；請檢查網絡、API 啟用狀態或配額，系統唔會估算行車時間。", 502, "PROVIDER_UPSTREAM_ERROR")
    except Exception as e:
        raise ViewingPlanError(f"Google Directions 呼叫失敗（{type(e).__name__}）；請檢查 provider 設定，系統唔會估算行車時間。", 502, "PROVIDER_ERROR")
    if g.get("status") != "OK" or not g.get("routes"):
        msg, code = _provider_error_message("Directions API", g.get("status"), g.get("error_message", ""))
        raise ViewingPlanError(msg, 502, code)
    route = g["routes"][0]
    wp_order = route.get("waypoint_order", list(range(len(middle))))
    if end_location:
        ordered = [middle[i] for i in wp_order]
    else:
        ordered = [middle[i] for i in wp_order] + [dest]
    leg_minutes = []
    for leg in route.get("legs", []):
        sec = (leg.get("duration_in_traffic") or leg.get("duration") or {}).get("value")
        if not sec:
            raise ViewingPlanError("Google Directions 未提供某段行車時間，已停止，唔會估算。", 502, "PROVIDER_ERROR")
        leg_minutes.append(math.ceil(sec / 60))
    overview = (route.get("overview_polyline") or {}).get("points") or ""
    coords = decode_polyline(overview) if overview else []
    route_geometry = {"type": "LineString", "coordinates": coords} if coords else None
    heuristic = "provider_waypoint_optimization" if optimize_waypoints else "manual_order_recalculation"
    result = {"provider": "google_directions", "heuristic": heuristic, **build_schedule([origin] + ordered, leg_minutes, departure, duration_min, "driving", end_location=end_location)}
    warnings = []
    if route_geometry:
        result["routeGeometry"] = route_geometry
        result["routeBounds"] = geometry_bounds(route_geometry)
    else:
        warnings.append({"message": "Google Directions 未提供路線 geometry；只顯示真實地圖及 markers，不會畫假路線。"})
    result["warnings"] = warnings
    return result


def nearest_neighbor_order(matrix: list[list[int]], count: int) -> list[int]:
    """Deterministic public-transit heuristic: nearest neighbour then 2-opt.

    Nodes: 0 is origin, 1..count are properties. Ties break by original index.
    """
    remaining = set(range(1, count + 1))
    order = []
    cur = 0
    while remaining:
        nxt = min(remaining, key=lambda j: (matrix[cur][j], j))
        order.append(nxt)
        remaining.remove(nxt)
        cur = nxt
    improved = True
    while improved:
        improved = False
        for i, j in itertools.combinations(range(len(order)), 2):
            cand = order[:i] + list(reversed(order[i:j + 1])) + order[j + 1:]
            if _path_cost(matrix, cand) < _path_cost(matrix, order):
                order = cand
                improved = True
    return [i - 1 for i in order]


def _path_cost(matrix: list[list[int]], order_nodes: list[int]) -> int:
    cur = 0
    total = 0
    for n in order_nodes:
        total += matrix[cur][n]
        cur = n
    return total


def optimize_transit(origin: dict[str, Any], stops: list[dict[str, Any]], departure: datetime, duration_min: int, matrix_provider: Callable[..., list[list[int]]] | None = None, manual_order: bool = False, routes_provider: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    if matrix_provider is None and not GOOGLE_MAPS_API_KEY:
        raise ViewingPlanError("GOOGLE_MAPS_API_KEY 未設定；未能使用 Google Routes API transit，唔會估算公共交通時間。", 503, "PROVIDER_NOT_CONFIGURED")
    matrix = matrix_provider(origin, stops, departure) if matrix_provider else google_transit_matrix(origin, stops, departure)
    n = len(stops)
    if len(matrix) != n + 1 or any(len(row) != n + 1 for row in matrix):
        raise ViewingPlanError("公共交通 Routes API matrix 回應格式不完整，已停止，唔會估算。", 502, "PROVIDER_ERROR")
    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if r != c and (value is None or int(value) <= 0):
                raise ViewingPlanError("公共交通 Routes API 未支援其中一段路線，已停止，唔會估算時間。", 502, "PROVIDER_UNSUPPORTED_ROUTE")
    order_idx = list(range(n)) if manual_order else nearest_neighbor_order(matrix, n)
    ordered = [stops[i] for i in order_idx]
    matrix_leg_minutes: list[int] = []
    cur = 0
    for idx in order_idx:
        node = idx + 1
        matrix_leg_minutes.append(math.ceil(int(matrix[cur][node]) / 60))
        cur = node

    warnings = []
    lines: list[list[list[float]]] = []
    leg_minutes = list(matrix_leg_minutes)
    if matrix_provider is None or routes_provider is not None:
        # Compute Routes is the source of truth for each scheduled transit leg:
        # it supplies both the time at the actual leg departure and its polyline.
        route_leg_minutes: list[int] = []
        prev = origin
        current_departure = departure
        for i, stop in enumerate(ordered):
            route_data = (routes_provider or google_transit_route)(prev, stop, current_departure)
            coords = (route_data or {}).get("coordinates") or []
            if not coords:
                raise ViewingPlanError("Google Routes API 未提供某段公共交通 geometry；已停止，唔會畫假路線。", 502, "PROVIDER_ERROR")
            seconds = _duration_seconds((route_data or {}).get("durationSeconds"))
            if seconds <= 0:
                if routes_provider is not None:
                    seconds = matrix_leg_minutes[i] * 60
                else:
                    raise ViewingPlanError("Google Routes API Compute Routes 未提供某段公共交通時間；系統唔會估算。", 502, "PROVIDER_ERROR")
            minutes = math.ceil(seconds / 60)
            route_leg_minutes.append(minutes)
            lines.append(coords)
            current_departure = current_departure + timedelta(minutes=minutes + duration_min)
            prev = stop
        leg_minutes = route_leg_minutes
    else:
        warnings.append({"message": "測試／自訂 transit matrix provider 未提供路線 geometry；不會畫假路線。"})

    heuristic = "manual_order_recalculation" if manual_order else "nearest-neighbour + deterministic 2-opt over time-aware Google Routes API transit matrix"
    result = {"provider": "google_routes_api", "heuristic": heuristic, **build_schedule([origin] + ordered, leg_minutes, departure, duration_min, "transit")}
    if lines:
        route_geometry = {"type": "MultiLineString", "coordinates": lines}
        result["routeGeometry"] = route_geometry
        result["routeBounds"] = geometry_bounds(route_geometry)
    result["warnings"] = warnings
    return result\n\ndef _routes_waypoint(place: dict[str, Any]) -> dict[str, Any]:
    return {"waypoint": {"location": {"latLng": {"latitude": float(place["lat"]), "longitude": float(place["lon"])}}}}


def _duration_seconds(value: Any) -> int:
    if isinstance(value, str) and value.endswith("s"):
        return int(float(value[:-1]))
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _status_name(status: Any, condition: Any = None) -> str:
    if isinstance(status, dict):
        explicit = status.get("status")
        if explicit:
            return str(explicit)
        code = status.get("code")
        grpc_map = {5: "NOT_FOUND", 7: "PERMISSION_DENIED", 8: "RESOURCE_EXHAUSTED", 9: "FAILED_PRECONDITION", 14: "UNAVAILABLE"}
        if code in grpc_map:
            return grpc_map[code]
        return str(code or status.get("message") or condition or "UNKNOWN")
    if status:
        return str(status)
    if condition and condition != "ROUTE_EXISTS":
        return str(condition)
    return "UNKNOWN"


def google_transit_route(origin: dict[str, Any], destination: dict[str, Any], departure: datetime) -> dict[str, Any]:
    departure_utc = departure.astimezone(timezone.utc)
    cache_key = (
        f"{round(float(origin['lat']), 5)},{round(float(origin['lon']), 5)}|"
        f"{round(float(destination['lat']), 5)},{round(float(destination['lon']), 5)}|"
        f"{departure_utc.replace(second=0, microsecond=0).isoformat()}"
    )
    cached = _transit_route_cache.get(cache_key)
    if cached:
        return cached
    payload = {
        "origin": {"location": {"latLng": {"latitude": float(origin["lat"]), "longitude": float(origin["lon"])}}},
        "destination": {"location": {"latLng": {"latitude": float(destination["lat"]), "longitude": float(destination["lon"])}}},
        "travelMode": "TRANSIT",
        "departureTime": departure_utc.isoformat().replace("+00:00", "Z"),
        "languageCode": "zh-TW",
        "units": "METRIC",
    }
    data = _google_routes_post("directions/v2:computeRoutes", payload, "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline")
    routes = data.get("routes") if isinstance(data, dict) else []
    if not routes:
        msg, code = _provider_error_message("Routes API Compute Routes", "ROUTE_NOT_FOUND", "")
        raise ViewingPlanError(msg, 502, code)
    duration_seconds = _duration_seconds(routes[0].get("duration"))
    if duration_seconds <= 0:
        raise ViewingPlanError("Google Routes API Compute Routes 未提供 transit duration；系統唔會估算。", 502, "PROVIDER_ERROR")
    poly = ((routes[0].get("polyline") or {}).get("encodedPolyline") or "")
    coords = decode_polyline(poly) if poly else []
    if not coords:
        raise ViewingPlanError("Google Routes API Compute Routes 未提供 transit polyline；系統唔會畫假路線。", 502, "PROVIDER_ERROR")
    result = {"type": "LineString", "coordinates": coords, "durationSeconds": duration_seconds}
    _transit_route_cache[cache_key] = result
    return result\n\ndef google_transit_matrix(origin: dict[str, Any], stops: list[dict[str, Any]], departure: datetime) -> list[list[int]]:
    nodes = [origin] + stops
    payload = {
        "origins": [_routes_waypoint(x) for x in nodes],
        "destinations": [_routes_waypoint(x) for x in nodes],
        "travelMode": "TRANSIT",
        "departureTime": departure.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "languageCode": "zh-TW",
        "units": "METRIC",
    }
    try:
        items = _google_routes_post("distanceMatrix/v2:computeRouteMatrix", payload, "originIndex,destinationIndex,status,condition,duration,distanceMeters")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ViewingPlanError(f"Google Routes API Compute Route Matrix upstream 回應異常／非 JSON（{type(e).__name__}）；請檢查網絡、API 啟用狀態或配額，系統唔會估算公共交通時間。", 502, "PROVIDER_UPSTREAM_ERROR")
    except ViewingPlanError:
        raise
    except Exception as e:
        raise ViewingPlanError(f"Google Routes API Compute Route Matrix 呼叫失敗（{type(e).__name__}）；請檢查 provider 設定，系統唔會估算公共交通時間。", 502, "PROVIDER_ERROR")
    if not isinstance(items, list):
        raise ViewingPlanError("Google Routes API Compute Route Matrix 回應格式不正確；系統唔會估算公共交通時間。", 502, "PROVIDER_ERROR")
    size = len(nodes)
    matrix: list[list[int]] = [[0 for _ in range(size)] for _ in range(size)]
    seen: set[tuple[int, int]] = set()
    for el in items:
        if not isinstance(el, dict):
            continue
        oi = el.get("originIndex")
        di = el.get("destinationIndex")
        if oi is None or di is None or not (0 <= int(oi) < size and 0 <= int(di) < size):
            continue
        oi, di = int(oi), int(di)
        seen.add((oi, di))
        if oi == di:
            matrix[oi][di] = 0
            continue
        status = el.get("status")
        condition = el.get("condition")
        status_name = _status_name(status, condition)
        sec = _duration_seconds(el.get("duration"))
        needs_pair_fallback = (
            status_name in {"NOT_FOUND", "ROUTE_NOT_FOUND", "NO_ROUTE_FOUND"}
            or (not status and (not condition or condition == "ROUTE_EXISTS") and sec <= 0)
        )
        if needs_pair_fallback:
            # Google Maps can expose valid Tokyo transit routes even when
            # Compute Route Matrix returns ROUTE_NOT_FOUND for the same pair.
            # Compute Routes is still the real Google Routes API; use its real
            # duration instead of rejecting the whole itinerary or estimating.
            route_data = google_transit_route(nodes[oi], nodes[di], departure)
            sec = _duration_seconds(route_data.get("durationSeconds"))
        elif status or (condition and condition != "ROUTE_EXISTS"):
            msg, code = _provider_error_message("Routes API Compute Route Matrix", status_name, (status or {}).get("message", "") if isinstance(status, dict) else "")
            raise ViewingPlanError(msg, 502, code)
        if sec <= 0:
            msg, code = _provider_error_message("Routes API Compute Route Matrix", "ROUTE_NOT_FOUND", "missing duration")
            raise ViewingPlanError(msg, 502, code)
        matrix[oi][di] = sec
    missing = [(r, c) for r in range(size) for c in range(size) if r != c and (r, c) not in seen]
    if missing:
        raise ViewingPlanError("Google Routes API Compute Route Matrix 回應缺少部分元素；系統唔會估算公共交通時間。", 502, "PROVIDER_ERROR")
    return matrix
