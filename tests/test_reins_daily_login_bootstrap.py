from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import reins_daily_login_bootstrap as boot

JST = ZoneInfo("Asia/Tokyo")


def write_env(path: Path) -> None:
    path.write_text("REINS_MEMBER_ID=dummy\nREINS_PASSWORD=dummy\nREINS_LOGIN_URL=https://example.invalid/\n")


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(7, 59, False), (8, 0, True), (22, 59, True), (23, 0, False)],
)
def test_reins_open_window_boundaries(hour, minute, expected):
    now = datetime(2026, 9, 2, hour, minute, tzinfo=JST)
    assert boot.is_reins_open(now) is expected


def test_closed_window_does_not_call_login(tmp_path):
    env = tmp_path / ".env"
    write_env(env)
    log = tmp_path / "login.log"
    called = False

    def login_fn(**_kwargs):
        nonlocal called
        called = True
        return {}

    code, payload = boot.run_bootstrap(
        now=datetime(2026, 9, 2, 7, 59, tzinfo=JST),
        env_path=env,
        log_path=log,
        lock_path=tmp_path / "lock",
        login_fn=login_fn,
    )
    assert code == 0
    assert payload["status"] == "closed"
    assert called is False
    assert read_events(log)[0]["status"] == "closed"


def test_successful_fresh_login(tmp_path):
    env = tmp_path / ".env"
    write_env(env)
    code, payload = boot.run_bootstrap(
        now=datetime(2026, 9, 2, 8, 0, tzinfo=JST),
        env_path=env,
        log_path=tmp_path / "login.log",
        lock_path=tmp_path / "lock",
        login_fn=lambda **_kwargs: {"login": True, "has_sales_search": True, "reused_session": False, "storage_state_saved": True},
    )
    assert code == 0
    assert payload["status"] == "success"
    assert payload["login"] is True
    assert payload["has_sales_search"] is True
    assert payload["reused_session"] is False


def test_successful_reused_session(tmp_path):
    env = tmp_path / ".env"
    write_env(env)
    code, payload = boot.run_bootstrap(
        now=datetime(2026, 9, 2, 8, 5, tzinfo=JST),
        env_path=env,
        log_path=tmp_path / "login.log",
        lock_path=tmp_path / "lock",
        login_fn=lambda **_kwargs: {"login": True, "has_sales_search": True, "reused_session": True, "storage_state_saved": True},
    )
    assert code == 0
    assert payload["status"] == "success"
    assert payload["reused_session"] is True


@pytest.mark.parametrize("manual", ["captcha", "2fa"])
def test_captcha_or_2fa_stops_as_failure(tmp_path, manual):
    env = tmp_path / ".env"
    write_env(env)
    code, payload = boot.run_bootstrap(
        now=datetime(2026, 9, 2, 9, 0, tzinfo=JST),
        env_path=env,
        log_path=tmp_path / "login.log",
        lock_path=tmp_path / "lock",
        login_fn=lambda **_kwargs: {"login": False, "has_sales_search": False, "reused_session": False, "needs_manual": manual},
    )
    assert code == 5
    assert payload["status"] == "failed"
    assert payload["error_type"] == manual


def test_lock_prevents_duplicate_run(tmp_path):
    env = tmp_path / ".env"
    write_env(env)
    lock = tmp_path / "lock"
    with boot.nonblocking_lock(lock):
        code, payload = boot.run_bootstrap(
            now=datetime(2026, 9, 2, 9, 0, tzinfo=JST),
            env_path=env,
            log_path=tmp_path / "login.log",
            lock_path=lock,
            login_fn=lambda **_kwargs: {"login": True, "has_sales_search": True},
        )
    assert code == 3
    assert payload["status"] == "failed"
    assert payload["error_type"] == "already_running"
