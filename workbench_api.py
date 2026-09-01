"""Pure data adapter helpers for the private property workbench.

This module deliberately has no Flask or database dependency so field
normalisation can be tested without touching the production SQLite file.
"""

import json
import re
import unicodedata

from property_search import infer_direction, listing_role


PRICE_RAW_YEN_THRESHOLD = 10_000_000
ALLOWED_STATUSES = {"draft", "published", "archived", "lead"}
ALLOWED_SORTS = {
    "updated_desc",
    "updated_asc",
    "price_desc",
    "price_asc",
    "area_desc",
    "area_asc",
    "completeness_desc",
    "completeness_asc",
}


def _text(value):
    return str(value or "").strip()


def _nfkc(value):
    return unicodedata.normalize("NFKC", _text(value))


def _json_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return []


def _money_to_int(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    digits = re.sub(r"[^0-9]", "", _nfkc(value))
    return int(digits) if digits else None


def _price_yen(data):
    raw = _money_to_int(data.get("price_raw"))
    if raw:
        return raw
    price = _money_to_int(data.get("price")) or 0
    return price if price > PRICE_RAW_YEN_THRESHOLD else price * 10_000


def _parse_address(address):
    address = _nfkc(address)
    pref_match = re.match(r"^(東京都|北海道|大阪府|京都府|.{2,3}県)", address)
    prefecture = pref_match.group(1) if pref_match else ""
    rest = address[len(prefecture):] if prefecture else address

    city = ""
    ward = ""
    city_match = re.match(r"^(.+?市)", rest)
    if city_match:
        city = city_match.group(1)
        ward_match = re.match(re.escape(city) + r"(.+?区)", rest)
        ward = ward_match.group(1) if ward_match else ""
    else:
        ward_match = re.match(r"^(.+?区)", rest)
        if ward_match:
            ward = ward_match.group(1)
            city = ward
        else:
            area_match = re.match(r"^(.+?(?:町|村|郡))", rest)
            city = area_match.group(1) if area_match else ""

    return prefecture, city, ward


def _image_url(value):
    if isinstance(value, dict):
        return _text(value.get("url"))
    return _text(value)


def _property_images(data):
    images = []
    seen = set()

    def add(value, image_type):
        url = _image_url(value)
        if not url or url in seen:
            return
        seen.add(url)
        images.append({"url": url, "type": image_type, "order": len(images) + 1})

    for value in _json_list(data.get("photos")):
        add(value, "exterior")
    for value in _json_list(data.get("interior_photos")):
        add(value, "interior")
    for value in _json_list(data.get("floorplan_images")):
        add(value, "floorplan")
    add(data.get("floorplan_url"), "floorplan")

    if float(data.get("latitude") or 0) and float(data.get("longitude") or 0):
        add(f"/map?listing={_text(data.get('id'))}", "map")
    return images


def _features(data):
    candidates = []
    candidates.extend(_json_list(data.get("ai_keywords")))
    candidates.extend([
        data.get("type"),
        data.get("land_rights"),
        data.get("current_status"),
        data.get("handover_timing"),
        data.get("use_district"),
    ])
    result = []
    seen = set()
    for item in candidates:
        value = _nfkc(item)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result[:8]


def _iso_datetime(value):
    value = _text(value)
    if not value:
        return ""
    if "T" not in value and " " in value:
        value = value.replace(" ", "T", 1)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", value):
        value += "Z"
    return value


def standardize_property(row):
    """Convert one DB/API row to the stable workbench property contract."""
    data = dict(row)
    address = _nfkc(data.get("address"))
    prefecture, city, ward = _parse_address(address)
    layout = _nfkc(data.get("room_layout"))
    structure = _nfkc(data.get("structure"))
    built_at = _nfkc(data.get("built_date_full"))
    if not built_at and int(data.get("built_year") or 0):
        built_at = f"{int(data['built_year'])}年"
    title = _nfkc(data.get("building_name")) or " ".join(v for v in (address, layout) if v)
    images = _property_images(data)

    status = _text(data.get("status")) or "draft"
    if status not in ALLOWED_STATUSES:
        status = "draft"

    direction, direction_source, direction_confidence = infer_direction(data)
    role = listing_role(data)
    details = {
        "buildingName": _nfkc(data.get("building_name")),
        "propertyType": _nfkc(data.get("type")),
        "landRights": _nfkc(data.get("land_rights")),
        "ownershipType": _nfkc(data.get("ownership_type")),
        "currentStatus": _nfkc(data.get("current_status")),
        "handoverTiming": _nfkc(data.get("handover_timing")),
        "transactionType": _nfkc(data.get("transaction_type")),
        "commissionType": _nfkc(data.get("commission_type")),
        "repairReserveYen": _money_to_int(data.get("repair_reserve")),
        "balconySqm": float(data.get("balcony_sqm") or 0),
        "totalUnits": int(data.get("total_units") or 0),
        "managementCompany": _nfkc(data.get("management_company")),
        "managementType": _nfkc(data.get("management_type")),
        "roofType": _nfkc(data.get("roof_type")),
        "undergroundFloors": int(data.get("underground_floors") or 0),
        "listingAgentName": _nfkc(data.get("listing_agent_name")),
        "licenseNumber": _nfkc(data.get("license_number")),
        "brokerageType": _nfkc(data.get("brokerage_type")),
        "landAreaSqm": float(data.get("land_area_sqm") or 0),
        "landAreaTsubo": float(data.get("land_area_tsubo") or 0),
        "landCategory": _nfkc(data.get("land_category")),
        "buildingCoverageRatio": int(data.get("building_coverage_ratio") or 0),
        "floorAreaRatio": int(data.get("floor_area_ratio") or 0),
        "cityPlanningZone": _nfkc(data.get("city_planning_zone")),
        "useDistrict": _nfkc(data.get("use_district")),
        "totalFloorAreaSqm": float(data.get("total_floor_area_sqm") or 0),
        "totalFloorAreaTsubo": float(data.get("total_floor_area_tsubo") or 0),
        "notes": _nfkc(data.get("notes_freetext")),
        "latitude": float(data.get("latitude") or 0),
        "longitude": float(data.get("longitude") or 0),
        "disasterFlood": _nfkc(data.get("disaster_flood")),
        "disasterEarthquake": _nfkc(data.get("disaster_earthquake")),
        "disasterLiquefaction": _nfkc(data.get("disaster_liquefaction")),
        "disasterTsunami": _nfkc(data.get("disaster_tsunami")),
        "reinsOverviewPdf": _text(data.get("reins_overview_pdf")),
        "reinsDrawingPdf": _text(data.get("reins_drawing_pdf")),
        "reinsRegisteredAt": _text(data.get("reins_registered_at")),
        "reinsUpdatedAt": _text(data.get("reins_updated_at")),
    }

    result = {
        "id": _text(data.get("id")),
        "source": _text(data.get("source")) or "upload",
        "title": title,
        "prefecture": prefecture,
        "city": city,
        "ward": ward,
        "address": address,
        "station": _nfkc(data.get("station")),
        "walkMinutes": max(0, int(data.get("walk_min") or 0)),
        "priceYen": _price_yen(data),
        "managementFeeYen": _money_to_int(data.get("mgmt_fee")),
        "areaSqm": float(data.get("size_sqm") or 0),
        "layout": layout,
        "floor": int(data.get("floor") or 0) or None,
        "totalFloors": int(data.get("total_floors") or data.get("floors_above") or 0) or None,
        "builtAt": built_at,
        "direction": direction,
        "directionSource": direction_source,
        "directionConfidence": direction_confidence,
        "listingRole": role["code"],
        "listingRoleLabel": role["label"],
        "listingRoleEvidence": role["evidence"],
        "structure": structure,
        "status": status,
        "features": _features(data),
        "images": images,
        "details": details,
        "updatedAt": _iso_datetime(data.get("updated_at")),
    }

    required = [
        "title", "address", "station", "priceYen", "areaSqm", "layout",
        "floor", "totalFloors", "builtAt", "direction", "structure", "images",
    ]
    missing = [name for name in required if not result.get(name)]
    result["missingFields"] = missing
    result["completeness"] = round((len(required) - len(missing)) / len(required), 2)
    return result


def filter_properties(items, *, q="", area="", min_price=0, max_price=0,
                      layout="", status="all", min_completeness=0,
                      source="all"):
    q = _nfkc(q).lower()
    area = _nfkc(area).lower()
    layout = _nfkc(layout).lower()
    result = []
    for item in items:
        haystack = " ".join(_nfkc(item.get(key)).lower() for key in (
            "title", "prefecture", "city", "ward", "address", "station",
            "layout", "structure", "source",
        ))
        haystack += " " + " ".join(_nfkc(v).lower() for v in item.get("features", []))
        if q and q not in haystack:
            continue
        if area and area not in haystack:
            continue
        if layout and layout not in _nfkc(item.get("layout")).lower():
            continue
        if status != "all" and item.get("status") != status:
            continue
        if source != "all" and item.get("source") != source:
            continue
        if min_price and item.get("priceYen", 0) < min_price:
            continue
        if max_price and item.get("priceYen", 0) > max_price:
            continue
        if item.get("completeness", 0) < min_completeness:
            continue
        result.append(item)
    return result


def sort_properties(items, sort="updated_desc"):
    sort = sort if sort in ALLOWED_SORTS else "updated_desc"
    key_name, reverse = {
        "updated_desc": ("updatedAt", True),
        "updated_asc": ("updatedAt", False),
        "price_desc": ("priceYen", True),
        "price_asc": ("priceYen", False),
        "area_desc": ("areaSqm", True),
        "area_asc": ("areaSqm", False),
        "completeness_desc": ("completeness", True),
        "completeness_asc": ("completeness", False),
    }[sort]
    return sorted(items, key=lambda item: item.get(key_name) or 0, reverse=reverse)


def property_stats(items):
    by_status = {name: 0 for name in ("draft", "published", "archived", "lead")}
    by_source = {}
    for item in items:
        status = item.get("status", "draft")
        by_status[status] = by_status.get(status, 0) + 1
        source = item.get("source", "unknown")
        by_source[source] = by_source.get(source, 0) + 1
    total = len(items)
    avg = round(sum(item.get("completeness", 0) for item in items) / total, 2) if total else 0
    return {
        "total": total,
        "byStatus": by_status,
        "bySource": by_source,
        "averageCompleteness": avg,
        "needsReview": sum(1 for item in items if item.get("status") == "draft" or item.get("completeness", 0) < 0.9),
    }
