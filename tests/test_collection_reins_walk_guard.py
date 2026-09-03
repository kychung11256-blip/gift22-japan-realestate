import pytest

import server
from reins_client import _extract_reins_validation_errors


@pytest.fixture()
def client(monkeypatch):
    server.app.config.update(TESTING=True)
    return server.app.test_client()


def test_reins_walk_requires_line_and_station_returns_422(client, monkeypatch):
    called = False

    def fail_if_called(_filters):
        nonlocal called
        called = True
        raise AssertionError("REINS search must not run when walkMin lacks line/station")

    import reins_client

    monkeypatch.setattr(reins_client, "search_properties", fail_if_called)

    resp = client.post(
        "/api/collection/search",
        json={
            "source": "reins",
            "pref": "東京都",
            "city": "港区",
            "priceMin": 3000,
            "priceMax": 10000,
            "walkMin": 10,
            "page": 1,
        },
    )

    assert resp.status_code == 422
    assert called is False
    assert resp.get_json() == {
        "code": 0,
        "error": "REINS_WALK_REQUIRES_STATION",
        "message": "REINS 步行條件必須同時指定沿線及車站",
    }


def test_reins_without_walk_searches_and_preserves_price_man_units(client, monkeypatch):
    captured = {}

    def fake_search(filters):
        captured.update(filters)
        return {
            "code": 1,
            "results": [{"reins_id": "100140299379", "price": 5980, "walk_min": 6}],
            "total_count": 1,
            "page": 1,
            "page_size": 50,
            "total_pages": 1,
            "hit_limit": False,
        }

    import reins_client

    monkeypatch.setattr(reins_client, "search_properties", fake_search)

    resp = client.post(
        "/api/collection/search",
        json={
            "source": "reins",
            "pref": "東京都",
            "city": "港区",
            "priceMin": 3000,
            "priceMax": 10000,
            "walkMin": 0,
            "page": 1,
        },
    )

    assert resp.status_code == 200
    assert captured["price_min"] == 3000
    assert captured["price_max"] == 10000
    assert captured["walk_min"] is None
    assert resp.get_json()["found"] == 1


def test_reins_validation_error_returns_422_not_fake_zero(client, monkeypatch):
    def fake_search(_filters):
        return {
            "code": 0,
            "error": "REINS_VALIDATION_ERROR",
            "message": "REINS 検索条件に誤りがあります",
            "detail": ["入力に誤りがあります。", "駅名は駅から徒歩が設定されている場合、必須です。"],
            "validation": True,
        }

    import reins_client

    monkeypatch.setattr(reins_client, "search_properties", fake_search)

    resp = client.post(
        "/api/collection/search",
        json={
            "source": "reins",
            "pref": "東京都",
            "city": "港区",
            "priceMin": 3000,
            "priceMax": 10000,
            "walkMin": 10,
            "line": "山手線",
            "station": "品川",
            "page": 1,
        },
    )

    assert resp.status_code == 422
    data = resp.get_json()
    assert data["code"] == 0
    assert data["error"] == "REINS_VALIDATION_ERROR"
    assert data["detail"] == ["入力に誤りがあります。", "駅名は駅から徒歩が設定されている場合、必須です。"]
    assert "found" not in data


def test_suumo_walk_min_still_passes_through(client, monkeypatch):
    captured = {}

    def fake_suumo_search(**kwargs):
        captured.update(kwargs)
        return {"listings": [{"id": "SUUMO1", "walk_min": "10"}]}

    monkeypatch.setattr(server, "suumo_search", fake_suumo_search)

    resp = client.post(
        "/api/collection/search",
        json={
            "source": "suumo",
            "pref": "東京都",
            "city": "港区",
            "priceMin": 3000,
            "priceMax": 10000,
            "walkMin": 10,
            "maxProperties": 20,
            "page": 1,
        },
    )

    assert resp.status_code == 200
    assert captured["walk"] == 10
    assert captured["pmin"] == 3000
    assert captured["pmax"] == 10000
    assert resp.get_json()["found"] == 1


def test_reins_validation_error_extractor_sanitizes_actual_page_messages():
    body = """
    入力に誤りがあります。
    沿線名
    駅名は駅から徒歩が設定されている場合、必須です。
    token=secret-like-noise
    """

    assert _extract_reins_validation_errors(body) == [
        "入力に誤りがあります。",
        "駅名は駅から徒歩が設定されている場合、必須です。",
    ]
