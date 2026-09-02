#!/usr/bin/env python3
"""Daily REINS session bootstrap.

Runs the existing reins_client.login_and_verify() only during REINS service
hours (JST 08:00 inclusive to 23:00 exclusive).  It never prints secrets,
cookies, or storage state content.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from reins_client import login_and_verify

JST = ZoneInfo("Asia/Tokyo")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"
DEFAULT_LOG_PATH = BASE_DIR / "logs" / "reins_login_bootstrap.log"
DEFAULT_LOCK_PATH = BASE_DIR / "data" / "reins_login_bootstrap.lock"

SAFE_RESULT_KEYS = ("login", "has_sales_search", "reused_session", "needs_manual", "storage_state_saved")
REQUIRED_ENV_KEYS = ("REINS_MEMBER_ID", "REINS_PASSWORD")
OPTIONAL_ENV_KEYS = ("REINS_LOGIN_URL",)


class BootstrapError(RuntimeError):
    pass


class AlreadyRunning(BootstrapError):
    pass


def load_env_file(path: Path = DEFAULT_ENV_PATH) -> dict[str, bool]:
    """Load project .env into os.environ without returning secret values."""
    presence = {key: bool(os.environ.get(key, "")) for key in REQUIRED_ENV_KEYS + OPTIONAL_ENV_KEYS}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in REQUIRED_ENV_KEYS + OPTIONAL_ENV_KEYS and not os.environ.get(key):
                os.environ[key] = value.strip()
    return {key: bool(os.environ.get(key, "")) for key in REQUIRED_ENV_KEYS + OPTIONAL_ENV_KEYS}


def is_reins_open(now: datetime | None = None) -> bool:
    now_jst = (now or datetime.now(JST)).astimezone(JST)
    return 8 <= now_jst.hour < 23


def event(status: str, result: dict | None = None, error_type: str = "", now: datetime | None = None) -> dict:
    result = result or {}
    now_jst = (now or datetime.now(JST)).astimezone(JST)
    payload = {
        "ts_jst": now_jst.isoformat(),
        "status": status,
        "reused_session": bool(result.get("reused_session", False)),
        "has_sales_search": bool(result.get("has_sales_search", False)),
        "error_type": error_type,
    }
    if "login" in result:
        payload["login"] = bool(result.get("login"))
    return payload


def append_log(payload: dict, log_path: Path = DEFAULT_LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def nonblocking_lock(lock_path: Path = DEFAULT_LOCK_PATH):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("another REINS bootstrap is already running")
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def sanitize_result(result: dict) -> dict:
    safe = {key: result.get(key) for key in SAFE_RESULT_KEYS if key in result}
    if result.get("needs_manual"):
        safe["needs_manual"] = result.get("needs_manual")
    if result.get("error"):
        safe["error_type"] = type(result.get("error")).__name__ if not isinstance(result.get("error"), str) else "login_error"
    return safe


def run_bootstrap(
    *,
    now: datetime | None = None,
    env_path: Path = DEFAULT_ENV_PATH,
    log_path: Path = DEFAULT_LOG_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    login_fn: Callable[..., dict] = login_and_verify,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> tuple[int, dict]:
    env_presence = load_env_file(env_path)
    now_jst = (now or datetime.now(JST)).astimezone(JST)
    if not is_reins_open(now_jst):
        payload = event("closed", now=now_jst)
        append_log(payload, log_path)
        return 0, payload
    missing = [key for key in REQUIRED_ENV_KEYS if not env_presence.get(key)]
    if missing:
        payload = event("failed", error_type="missing_env", now=now_jst)
        append_log(payload, log_path)
        return 2, payload
    try:
        with nonblocking_lock(lock_path):
            result = login_fn(headless=headless, timeout_ms=timeout_ms)
    except AlreadyRunning:
        payload = event("failed", error_type="already_running", now=now_jst)
        append_log(payload, log_path)
        return 3, payload
    except Exception as exc:
        payload = event("failed", error_type=type(exc).__name__, now=now_jst)
        append_log(payload, log_path)
        return 4, payload

    safe = sanitize_result(result)
    if result.get("needs_manual"):
        payload = event("failed", result, error_type=str(result.get("needs_manual")), now=now_jst)
        append_log(payload, log_path)
        return 5, payload
    if result.get("login") is True and result.get("has_sales_search") is True:
        payload = event("success", result, now=now_jst)
        append_log(payload, log_path)
        return 0, payload
    payload = event("failed", result, error_type=safe.get("error_type") or "verification_failed", now=now_jst)
    append_log(payload, log_path)
    return 6, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap daily REINS session")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="Path to project .env")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH), help="Safe JSONL log path")
    parser.add_argument("--lock", default=str(DEFAULT_LOCK_PATH), help="Lock file path")
    parser.add_argument("--headed", action="store_true", help="Run browser headed")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    args = parser.parse_args()
    code, payload = run_bootstrap(
        env_path=Path(args.env),
        log_path=Path(args.log),
        lock_path=Path(args.lock),
        headless=not args.headed,
        timeout_ms=args.timeout_ms,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
