"""
MLIT 不動産情報ライブラリ API client using pyreinfolib library.
Wrapper for pyreinfolib to maintain backward compatibility with existing code.
"""

import os
import json
import math
import time

# Import pyreinfolib
try:
    from pyreinfolib import Client
    from pyreinfolib.exceptions import (
        APIError, AuthenticationError, InvalidResponseError,
        NoResultsError, RateLimitError, ReinfolibError, TransportError
    )
    PYREINFOLIB_AVAILABLE = True
except ImportError:
    PYREINFOLIB_AVAILABLE = False
    print("[mlit_client] Warning: pyreinfolib not available, falling back to manual implementation")

BASE_URL = "https://www.reinfolib.mlit.go.jp/ex-api/external"

# Rate limit: minimum interval between API calls in seconds
MIN_INTERVAL = 0.6
_last_call = 0.0

def _api_key():
    key = os.environ.get("REINFOLIB_API_KEY", "")
    if not key:
        raise RuntimeError("REINFOLIB_API_KEY environment variable not set")
    return key

def _rate_limit():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_call = time.time()

def latlon_to_tile(lat, lon, zoom):
    """Convert lat/lon to Web Mercator tile coordinates (z/x/y)."""
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return zoom, x, y

# ── pyreinfolib-based functions ──

def get_transactions(year, quarter, area=None, city=None, station=None,
                     price_classification=None, language=None):
    """
    Fetch real estate transaction data using pyreinfolib.
    Falls back to manual implementation if pyreinfolib not available.
    """
    if not PYREINFOLIB_AVAILABLE:
        return _get_transactions_manual(year, quarter, area, city, station,
                                       price_classification, language)

    try:
        client = Client(api_key=_api_key())
        _rate_limit()

        # Convert price_classification string to enum if needed
        pc = None
        if price_classification:
            from pyreinfolib.enums import PriceClassification
            if price_classification == "01":
                pc = PriceClassification.TRANSACTION
            elif price_classification == "02":
                pc = PriceClassification.CONTRACT

        result = client.get_real_estate_prices(
            year=int(year),
            quarter=int(quarter) if quarter else None,
            area=area,
            city=city,
            station=station,
            price_classification=pc,
            language=language
        )

        # Convert pyreinfolib response to our format
        return {
            "data": result.get("data", []),
            "error": False
        }
    except Exception as e:
        return {"error": True, "message": str(e)}

def get_land_use_zone(lat, lon, zoom=14):
    """
    Fetch land use zone classification using pyreinfolib.
    """
    if not PYREINFOLIB_AVAILABLE:
        return _get_land_use_zone_manual(lat, lon, zoom)

    try:
        client = Client(api_key=_api_key())
        _rate_limit()
        z, x, y = latlon_to_tile(lat, lon, zoom)
        result = client.get_use_districts(z, x, y)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}

def get_flood_risk(lat, lon):
    """XKT026: 洪水浸水想定区域"""
    if not PYREINFOLIB_AVAILABLE:
        return _disaster_api_manual("XKT026", lat, lon)

    try:
        client = Client(api_key=_api_key())
        _rate_limit()
        z, x, y = latlon_to_tile(lat, lon, 14)  # XKT026 requires zoom 14-15
        result = client.get_expected_flood_inundation_areas_at_maximum_scale(z, x, y)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}

def get_storm_surge_risk(lat, lon):
    """XKT027: 高潮浸水想定区域"""
    if not PYREINFOLIB_AVAILABLE:
        return _disaster_api_manual("XKT027", lat, lon)

    try:
        client = Client(api_key=_api_key())
        _rate_limit()
        z, x, y = latlon_to_tile(lat, lon, 14)  # XKT027 requires zoom 13-15
        result = client.get_expected_storm_surge_inundation_areas(z, x, y)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}

def get_tsunami_risk(lat, lon):
    """XKT028: 津波浸水想定"""
    if not PYREINFOLIB_AVAILABLE:
        return _disaster_api_manual("XKT028", lat, lon)

    try:
        client = Client(api_key=_api_key())
        _rate_limit()
        z, x, y = latlon_to_tile(lat, lon, 14)  # XKT028 requires zoom 14-15
        result = client.get_expected_tsunami_inundation(z, x, y)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}

def get_landslide_risk(lat, lon):
    """XKT029: 土砂災害警戒区域"""
    if not PYREINFOLIB_AVAILABLE:
        return _disaster_api_manual("XKT029", lat, lon)

    try:
        client = Client(api_key=_api_key())
        _rate_limit()
        z, x, y = latlon_to_tile(lat, lon, 14)  # XKT029 requires zoom 11-15
        result = client.get_sediment_disaster_alert_areas(z, x, y)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}

# ── Batch query helper ──

def check_disaster_risks(lat, lon):
    """Query all 4 disaster APIs and return a dict summary."""
    results = {}
    for name, fn in [
        ("flood", get_flood_risk),
        ("high_tide", get_storm_surge_risk),
        ("tsunami", get_tsunami_risk),
        ("landslide", get_landslide_risk),
    ]:
        try:
            data = fn(lat, lon)
            if isinstance(data, dict) and not data.get("error"):
                features = data.get("features", [])
                results[name] = "true" if features else "false"
            else:
                results[name] = "unknown"
        except Exception:
            results[name] = "unknown"
    return results

def _point_in_polygon(point, polygon_coords):
    """
    Check if a point is inside a polygon using ray casting algorithm.
    point: (lon, lat) tuple
    polygon_coords: list of [lon, lat] pairs
    """
    x, y = point
    n = len(polygon_coords)
    inside = False

    p1x, p1y = polygon_coords[0]
    for i in range(1, n + 1):
        p2x, p2y = polygon_coords[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y

    return inside

def _point_in_geometry(point, geometry):
    """
    Check if a point is inside a GeoJSON geometry (Polygon or MultiPolygon).
    point: (lon, lat) tuple
    geometry: GeoJSON geometry dict
    """
    geom_type = geometry.get("type", "")

    if geom_type == "Polygon":
        # Polygon coordinates: [[[lon, lat], ...]] (first ring is exterior)
        coords = geometry.get("coordinates", [])
        if coords and len(coords) > 0:
            return _point_in_polygon(point, coords[0])
    elif geom_type == "MultiPolygon":
        # MultiPolygon coordinates: [[[[lon, lat], ...]]] (multiple polygons)
        polygons = geometry.get("coordinates", [])
        for polygon in polygons:
            if polygon and len(polygon) > 0:
                if _point_in_polygon(point, polygon[0]):
                    return True

    return False

def extract_use_district_from_geojson(geojson_data, lat=None, lon=None):
    """
    Extract use district name from XKT002 GeoJSON response.
    If lat/lon provided, returns only the use district that contains the point.
    Otherwise, returns all use districts (for backward compatibility).

    Returns:
        If lat/lon provided: (use_district_name, coverage_ratio, floor_area_ratio) tuple
        Otherwise: use_district_string (backward compatible)
    """
    if not isinstance(geojson_data, dict) or geojson_data.get("error"):
        if lat is not None and lon is not None:
            return "", None, None
        return ""
    features = geojson_data.get("features", [])
    if not features:
        if lat is not None and lon is not None:
            return "", None, None
        return ""

    # If coordinates provided, do point-in-polygon check
    if lat is not None and lon is not None:
        point = (lon, lat)  # GeoJSON uses (lon, lat) order
        for f in features:
            props = f.get("properties", {})
            geom = f.get("geometry", {})
            name = (props.get("use_area_ja") or
                    props.get("土地利用区分名") or
                    props.get("用途地域") or
                    props.get("name") or "")

            if name and _point_in_geometry(point, geom):
                # Extract coverage ratio and floor area ratio from this specific feature
                cr_str = props.get("u_building_coverage_ratio_ja", "")
                fr_str = props.get("u_floor_area_ratio_ja", "")

                coverage_ratio = None
                floor_area_ratio = None

                if cr_str:
                    try:
                        coverage_ratio = int(float(cr_str.replace("%", "").replace(".0", "")))
                    except:
                        pass
                if fr_str:
                    try:
                        floor_area_ratio = int(float(fr_str.replace("%", "").replace(".0", "")))
                    except:
                        pass

                return name, coverage_ratio, floor_area_ratio

        # Point not in any polygon (shouldn't happen, but return first as fallback)
        if features:
            props = features[0].get("properties", {})
            name = (props.get("use_area_ja") or
                    props.get("土地利用区分名") or
                    props.get("用途地域") or
                    props.get("name") or "")
            return name, None, None

        return "", None, None

    # Backward compatible: return all unique use districts
    names = []
    for f in features:
        props = f.get("properties", {})
        # Try multiple possible property names
        name = (props.get("use_area_ja") or
                props.get("土地利用区分名") or
                props.get("用途地域") or
                props.get("name") or "")
        if name and name not in names:
            names.append(name)
    return "・".join(names) if names else ""

def extract_planning_from_transactions(transactions_data):
    """
    Extract CityPlanning, CoverageRatio, FloorAreaRatio from XIT001 response.
    Returns dict with the most common values from nearby transactions.
    """
    if not isinstance(transactions_data, dict) or transactions_data.get("error"):
        return {}
    items = transactions_data.get("data", [])
    if not items:
        return {}

    # Collect all values
    city_plannings = []
    coverage_ratios = []
    floor_area_ratios = []

    for item in items:
        cp = item.get("CityPlanning", "")
        if cp:
            city_plannings.append(cp)
        cr = item.get("CoverageRatio", "")
        if cr:
            try:
                coverage_ratios.append(int(cr))
            except (ValueError, TypeError):
                pass
        far = item.get("FloorAreaRatio", "")
        if far:
            try:
                floor_area_ratios.append(int(far))
            except (ValueError, TypeError):
                pass

    # Most common
    def most_common(lst):
        if not lst:
            return None
        from collections import Counter
        return Counter(lst).most_common(1)[0][0]

    return {
        "city_planning": most_common(city_plannings) or "",
        "coverage_ratio": most_common(coverage_ratios),
        "floor_area_ratio": most_common(floor_area_ratios),
        "transaction_count": len(items),
        "transactions": items[:5]  # Keep first 5 for reference
    }

# ── Manual fallback implementations ──

def _make_request(url):
    """Make a GET request to MLIT API with rate limiting and gzip support."""
    _rate_limit()
    import urllib.request
    import gzip
    req = urllib.request.Request(url)
    req.add_header("Ocp-Apim-Subscription-Key", _api_key())
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                data = gzip.decompress(data)
            return json.loads(data)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": True, "status": e.code, "body": body[:200]}
    except Exception as e:
        return {"error": True, "message": str(e)}

def _get_transactions_manual(year, quarter, area=None, city=None, station=None,
                            price_classification=None, language=None):
    """Manual fallback for XIT001."""
    params = f"year={year}&quarter={quarter}"
    if area:
        params += f"&area={area}"
    if city:
        params += f"&city={city}"
    if station:
        params += f"&station={station}"
    if price_classification:
        params += f"&priceClassification={price_classification}"
    if language:
        params += f"&language={language}"

    url = f"{BASE_URL}/XIT001?{params}"
    return _make_request(url)

def _get_land_use_zone_manual(lat, lon, zoom=14):
    """Manual fallback for XKT002."""
    z, x, y = latlon_to_tile(lat, lon, zoom)
    url = f"{BASE_URL}/XKT002?response_format=geojson&z={z}&x={x}&y={y}"
    return _make_request(url)

def _disaster_api_manual(endpoint, lat, lon, zoom=14):
    """Manual fallback for disaster APIs."""
    z, x, y = latlon_to_tile(lat, lon, zoom)
    url = f"{BASE_URL}/{endpoint}?response_format=geojson&z={z}&x={x}&y={y}"
    return _make_request(url)
