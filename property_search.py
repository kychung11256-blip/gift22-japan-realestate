"""Deterministic property search and listing-role helpers.

Search constraints are evaluated against normalized database values.  Missing
facts never count as a match: for example, an unknown orientation is excluded
from an explicit south-facing search instead of being guessed.
"""

import json
import re
import unicodedata


PRICE_RAW_YEN_THRESHOLD = 10_000_000
_DIRECTIONS = ("南東", "南西", "北東", "北西", "南", "北", "東", "西")


def normalize_text(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def price_man(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return amount / 10000 if amount >= PRICE_RAW_YEN_THRESHOLD else amount


def _searchable_json(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def canonical_direction(value, *, require_marker=False):
    """Return one of the eight directions or an empty string.

    Structured values may be a bare direction.  Free text requires an explicit
    marker such as 朝南, 南向き or バルコニー方向：南 so 東京都 cannot become 東.
    """
    text = re.sub(r"\s+", "", normalize_text(value))
    if not text:
        return ""
    bare = text.replace("向き", "").replace("向", "")
    if not require_marker and bare in _DIRECTIONS:
        return bare

    marker_patterns = (
        r"(?:バルコニー方向|主要採光面|開口部方向|方角|朝向)[:：]?(?:朝|向)?(南東|南西|北東|北西|南|北|東|西)",
        r"(?:朝|向)(南東|南西|北東|北西|南|北|東|西)",
        r"(南東|南西|北東|北西|南|北|東|西)(?:向き|向|面バルコニー)",
    )
    for pattern in marker_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def infer_direction(item):
    structured = canonical_direction(item.get("orientation"))
    if structured:
        source = normalize_text(item.get("orientation_source")) or "structured"
        try:
            confidence = float(item.get("orientation_confidence") or 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        return structured, source, confidence

    evidence_fields = (
        ("notes_freetext", "notes"),
        ("ai_generated_copy", "copy"),
        ("ai_keywords", "keywords"),
        ("orientation_evidence", "evidence"),
    )
    for field, source in evidence_fields:
        direction = canonical_direction(_searchable_json(item.get(field)), require_marker=True)
        if direction:
            return direction, source, 0.85
    return "", "missing", 0.0


def listing_role(item):
    """Classify direct-owner vs agent/representative without inventing facts."""
    transaction = normalize_text(item.get("transaction_type"))
    commission = normalize_text(item.get("commission_type"))
    brokerage = normalize_text(item.get("brokerage_type"))
    agent_name = normalize_text(item.get("listing_agent_name"))
    evidence = " / ".join(x for x in (transaction, commission, brokerage) if x)
    joined = " ".join((transaction, commission, brokerage))

    if any(term in joined for term in ("売主", "所有者", "個人直售", "業主直售")):
        return {"code": "direct", "label": "個人／業主房", "evidence": evidence or "取引態様"}
    if any(term in joined for term in ("代理", "媒介", "仲介", "専任", "専属")) or agent_name:
        return {"code": "agent", "label": "仲介／代理房", "evidence": evidence or agent_name}
    return {"code": "unknown", "label": "身份待確認", "evidence": evidence}


def parse_query(query):
    text = normalize_text(query).lower()
    residual = text
    constraints = {"price_min": 0, "price_max": 0, "direction": "", "flags": []}

    price_patterns = (
        ("price_max", r"(\d+(?:\.\d+)?)\s*億(?:円)?\s*(?:以下|以内|まで)", 10000),
        ("price_min", r"(\d+(?:\.\d+)?)\s*億(?:円)?\s*(?:以上|から)", 10000),
        ("price_max", r"(\d+(?:\.\d+)?)\s*万(?:円)?\s*(?:以下|以内|まで)", 1),
        ("price_min", r"(\d+(?:\.\d+)?)\s*万(?:円)?\s*(?:以上|から)", 1),
    )
    for key, pattern, multiplier in price_patterns:
        match = re.search(pattern, residual)
        if match:
            constraints[key] = float(match.group(1)) * multiplier
            residual = residual[:match.start()] + " " + residual[match.end():]

    direction = canonical_direction(text, require_marker=True)
    if direction:
        constraints["direction"] = direction
        direction_patterns = (
            r"(?:バルコニー方向|主要採光面|開口部方向|方角|朝向)[:：]?(?:朝|向)?(?:南東|南西|北東|北西|南|北|東|西)",
            r"(?:朝|向)(?:南東|南西|北東|北西|南|北|東|西)",
            r"(?:南東|南西|北東|北西|南|北|東|西)(?:向き|向|面バルコニー)",
        )
        for pattern in direction_patterns:
            residual = re.sub(pattern, " ", residual)

    flag_terms = {
        "investment": ("投資用", "investment"),
        "near_station": ("駅近", "站近", "station"),
        "newer": ("新築", "new"),
    }
    for flag, terms in flag_terms.items():
        if any(term in residual for term in terms):
            constraints["flags"].append(flag)
            for term in terms:
                residual = residual.replace(term, " ")

    residual = re.sub(r"[\s,，、;；/]+", " ", residual).strip()
    constraints["terms"] = [term for term in residual.split(" ") if term]
    return constraints


def filter_local_listings(items, query):
    constraints = parse_query(query)
    output = []
    for item in items:
        amount = price_man(item.get("price"))
        if constraints["price_min"] and amount < constraints["price_min"]:
            continue
        if constraints["price_max"] and amount > constraints["price_max"]:
            continue

        direction, direction_source, direction_confidence = infer_direction(item)
        if constraints["direction"] and direction != constraints["direction"]:
            continue

        flags = constraints["flags"]
        if "investment" in flags and float(item.get("yield_surface") or 0) < 4.5:
            continue
        if "near_station" in flags and int(item.get("walk_min") or 0) > 5:
            continue
        if "newer" in flags and int(item.get("age") or 0) > 5:
            continue

        role = listing_role(item)
        haystack = " ".join(normalize_text(_searchable_json(item.get(key))).lower() for key in (
            "address", "station", "room_layout", "structure", "type",
            "building_name", "notes_freetext", "ai_generated_copy", "ai_keywords",
            "transaction_type", "commission_type", "listing_agent_name", "brokerage_type",
        ))
        haystack += f" {direction.lower()} {role['label'].lower()}"
        if any(term not in haystack for term in constraints["terms"]):
            continue

        enriched = dict(item)
        enriched["effective_orientation"] = direction
        enriched["orientation_source"] = direction_source
        enriched["orientation_confidence"] = direction_confidence
        enriched["listing_role"] = role["code"]
        enriched["listing_role_label"] = role["label"]
        enriched["listing_role_evidence"] = role["evidence"]
        output.append(enriched)

    return sorted(output, key=lambda item: price_man(item.get("price")))
