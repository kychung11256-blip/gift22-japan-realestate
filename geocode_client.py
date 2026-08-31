"""
GSI (Geospatial Information Authority of Japan) geocoding client.
Simple address → lat/lon conversion using GSI API.
"""

import urllib.request
import urllib.parse
import json
import time

_last_call = 0.0
MIN_INTERVAL = 0.5

def _rate_limit():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_call = time.time()

def geocode(address):
    """
    Convert Japanese address to lat/lon using GSI geocoding API.
    Returns (lat, lon) tuple, or (None, None) if not found.
    """
    if not address:
        return None, None

    _rate_limit()

    # GSI geocoding API endpoint
    base_url = "https://msearch.gsi.go.jp/address-search/AddressSearch"

    # URL encode the address
    params = urllib.parse.urlencode({'q': address})
    url = f"{base_url}?{params}"

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Johnny-AI-Platform/1.0')

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))

            if data and len(data) > 0:
                # GSI returns list of results, take first one
                first = data[0]
                geometry = first.get('geometry', {})
                coords = geometry.get('coordinates', [])

                if len(coords) >= 2:
                    lon = coords[0]
                    lat = coords[1]
                    return lat, lon

    except Exception as e:
        print(f"[GSI geocode error] {address}: {e}", flush=True)

    return None, None

def geocode_with_fallback(address):
    """
    Try GSI geocoding, with fallback to simple parsing if GSI fails.
    Returns (lat, lon) tuple.
    """
    lat, lon = geocode(address)

    if lat and lon:
        return lat, lon

    # Fallback: try to extract city/ward from address and use approximate coordinates
    # This is a simple fallback for common Tokyo areas
    tokyo_coords = {
        '千代田区': (35.6940, 139.7536),
        '中央区': (35.6717, 139.7719),
        '港区': (35.6580, 139.7516),
        '新宿区': (35.6938, 139.7034),
        '文京区': (35.7081, 139.7522),
        '台東区': (35.7127, 139.7800),
        '墨田区': (35.7107, 139.8015),
        '江東区': (35.6731, 139.8171),
        '品川区': (35.6289, 139.7389),
        '目黒区': (35.6414, 139.6982),
        '大田区': (35.5614, 139.7160),
        '世田谷区': (35.6464, 139.6532),
        '渋谷区': (35.6580, 139.7016),
        '中野区': (35.7074, 139.6638),
        '杉並区': (35.6995, 139.6364),
        '豊島区': (35.7295, 139.7165),
        '北区': (35.7528, 139.7335),
        '荒川区': (35.7361, 139.7833),
        '板橋区': (35.7512, 139.7090),
        '練馬区': (35.7356, 139.6517),
        '足立区': (35.7744, 139.8044),
        '葛飾区': (35.7433, 139.8470),
        '江戸川区': (35.7067, 139.8683),
    }

    for area, (lat, lon) in tokyo_coords.items():
        if area in address:
            return lat, lon

    # Default to Tokyo Station if no match
    return 35.6812, 139.7671
