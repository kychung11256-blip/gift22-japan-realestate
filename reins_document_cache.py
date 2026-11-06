"""REINS paid-document registry and local cache.

This module deliberately never talks to REINS.  A staff member buys/downloads
the document in the official REINS UI and uploads it here once.  All later
parsing, rendering and photo extraction reuse the verified local file.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from db import get_db


ALLOWED_DOCUMENT_TYPES = {"drawing"}
MAX_PDF_BYTES = int(os.environ.get("REINS_DOCUMENT_MAX_BYTES", 25 * 1024 * 1024))
_SAFE_VERSION = re.compile(r"[^0-9A-Za-z._-]+")


class DocumentCacheError(Exception):
    def __init__(self, message, status=400, code="DOCUMENT_CACHE_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate(reins_id, document_type, upstream_version=""):
    reins_id = (reins_id or "").strip()
    document_type = (document_type or "").strip().lower()
    upstream_version = (upstream_version or "unknown").strip()[:120]
    if not reins_id or not re.fullmatch(r"[0-9A-Za-z_-]{4,80}", reins_id):
        raise DocumentCacheError("Invalid REINS listing id")
    if document_type not in ALLOWED_DOCUMENT_TYPES:
        raise DocumentCacheError("Only REINS drawing documents are supported")
    return reins_id, document_type, upstream_version


def ensure_tables(conn=None):
    own = conn is None
    conn = conn or get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reins_document_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reins_id TEXT NOT NULL,
            document_type TEXT NOT NULL CHECK(document_type IN ('drawing')),
            upstream_version TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL DEFAULT 'requested'
                CHECK(status IN ('requested','ready','failed','superseded')),
            local_path TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            expected_cost_yen INTEGER NOT NULL DEFAULT 0,
            requested_by TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(reins_id, document_type, upstream_version)
        );
        CREATE INDEX IF NOT EXISTS idx_reins_doc_status
            ON reins_document_cache(status, requested_at);
        CREATE TABLE IF NOT EXISTS reins_document_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            amount_yen INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(cache_id) REFERENCES reins_document_cache(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_reins_doc_ledger_created
            ON reins_document_ledger(created_at);
    """)
    conn.commit()
    if own:
        conn.close()


def _row_dict(row):
    return dict(row) if row else None


def _file_matches(row):
    if not row or row["status"] != "ready" or not row["local_path"] or not row["sha256"]:
        return False
    path = Path(__file__).resolve().parent / row["local_path"].lstrip("/")
    if not path.is_file() or path.stat().st_size != int(row["size_bytes"] or 0):
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest == row["sha256"]


def get_status(reins_id, document_type="drawing", upstream_version=""):
    reins_id, document_type, upstream_version = _validate(reins_id, document_type, upstream_version)
    conn = get_db()
    ensure_tables(conn)
    if upstream_version == "unknown":
        row = conn.execute(
            """SELECT * FROM reins_document_cache
               WHERE reins_id=? AND document_type=?
               ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END, id DESC LIMIT 1""",
            (reins_id, document_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM reins_document_cache WHERE reins_id=? AND document_type=? AND upstream_version=?",
            (reins_id, document_type, upstream_version),
        ).fetchone()
    result = _row_dict(row)
    if result:
        result["file_valid"] = _file_matches(row)
        result.pop("local_path", None)
    conn.close()
    return result


def request_purchase(reins_id, document_type="drawing", upstream_version="", *,
                     actor="", expected_cost_yen=0, idempotency_key="", confirm_latest=False):
    """Create an idempotent manual-purchase request; never contacts REINS."""
    reins_id, document_type, upstream_version = _validate(reins_id, document_type, upstream_version)
    expected_cost_yen = max(0, int(expected_cost_yen or 0))
    conn = get_db()
    ensure_tables(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        same = conn.execute(
            "SELECT * FROM reins_document_cache WHERE reins_id=? AND document_type=? AND upstream_version=?",
            (reins_id, document_type, upstream_version),
        ).fetchone()
        if same:
            valid = _file_matches(same)
            conn.commit()
            out = _row_dict(same)
            out["reused"] = bool(valid)
            out["manual_purchase_required"] = not valid
            out.pop("local_path", None)
            return out

        latest_ready = conn.execute(
            """SELECT * FROM reins_document_cache
               WHERE reins_id=? AND document_type=? AND status='ready'
               ORDER BY id DESC LIMIT 1""",
            (reins_id, document_type),
        ).fetchone()
        if latest_ready and latest_ready["upstream_version"] != upstream_version and not confirm_latest:
            conn.rollback()
            raise DocumentCacheError(
                "A cached drawing exists for an older version; confirm latest-version purchase first",
                409, "REINS_VERSION_CONFIRMATION_REQUIRED",
            )

        daily_limit = max(0, int(os.environ.get("REINS_PAID_DOWNLOAD_DAILY_LIMIT", "0") or 0))
        monthly_limit = max(0, int(os.environ.get("REINS_PAID_DOWNLOAD_MONTHLY_LIMIT", "0") or 0))
        daily_count = conn.execute(
            """SELECT COUNT(*) FROM reins_document_ledger
               WHERE event_type='manual_purchase_uploaded' AND date(created_at)=date('now')"""
        ).fetchone()[0]
        monthly_count = conn.execute(
            """SELECT COUNT(*) FROM reins_document_ledger
               WHERE event_type='manual_purchase_uploaded'
               AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')"""
        ).fetchone()[0]
        if daily_limit and daily_count >= daily_limit:
            conn.rollback()
            raise DocumentCacheError("Daily REINS document budget reached", 429, "REINS_DAILY_BUDGET_REACHED")
        if monthly_limit and monthly_count >= monthly_limit:
            conn.rollback()
            raise DocumentCacheError("Monthly REINS document budget reached", 429, "REINS_MONTHLY_BUDGET_REACHED")

        now = _now()
        cur = conn.execute(
            """INSERT INTO reins_document_cache
               (reins_id,document_type,upstream_version,status,expected_cost_yen,
                requested_by,idempotency_key,requested_at)
               VALUES (?,?,?,'requested',?,?,?,?)""",
            (reins_id, document_type, upstream_version, expected_cost_yen,
             actor[:120], (idempotency_key or "")[:160], now),
        )
        cache_id = cur.lastrowid
        conn.execute(
            "INSERT INTO reins_document_ledger(cache_id,event_type,actor,amount_yen,note,created_at) VALUES (?,?,?,?,?,?)",
            (cache_id, "manual_purchase_requested", actor[:120], expected_cost_yen,
             "No REINS network request was made by the platform", now),
        )
        conn.commit()
        return {
            "id": cache_id, "reins_id": reins_id, "document_type": document_type,
            "upstream_version": upstream_version, "status": "requested", "reused": False,
            "manual_purchase_required": True,
            "budget": {
                "daily": {"used": daily_count, "limit": daily_limit or None},
                "monthly": {"used": monthly_count, "limit": monthly_limit or None},
            },
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute(
            "SELECT * FROM reins_document_cache WHERE reins_id=? AND document_type=? AND upstream_version=?",
            (reins_id, document_type, upstream_version),
        ).fetchone()
        if not row:
            raise
        out = _row_dict(row)
        out["reused"] = bool(_file_matches(row))
        out["manual_purchase_required"] = not out["reused"]
        out.pop("local_path", None)
        return out
    finally:
        conn.close()


def store_uploaded_pdf(reins_id, document_type, upstream_version, stream, *, actor="", cost_yen=0):
    """Store a staff-downloaded PDF atomically and mark the cache ready."""
    reins_id, document_type, upstream_version = _validate(reins_id, document_type, upstream_version)
    root = Path(__file__).resolve().parent
    version_dir = _SAFE_VERSION.sub("_", upstream_version).strip("._") or "unknown"
    dest_dir = root / "uploads" / "reins-cache" / reins_id / document_type / version_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="upload-", suffix=".pdf", dir=str(dest_dir))
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise DocumentCacheError("PDF is too large", 413, "DOCUMENT_TOO_LARGE")
                digest.update(chunk)
                out.write(chunk)
        tmp = Path(tmp_name)
        with tmp.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise DocumentCacheError("Only a valid PDF can be uploaded", 400, "INVALID_PDF")
        sha = digest.hexdigest()
        final = dest_dir / f"{sha}.pdf"
        if final.exists():
            tmp.unlink()
        else:
            os.replace(tmp, final)
        web_path = "/" + str(final.relative_to(root)).replace(os.sep, "/")

        conn = get_db()
        ensure_tables(conn)
        now = _now()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM reins_document_cache WHERE reins_id=? AND document_type=? AND upstream_version=?",
            (reins_id, document_type, upstream_version),
        ).fetchone()
        if row and row["status"] == "ready" and _file_matches(row):
            if row["sha256"] != sha:
                conn.rollback()
                conn.close()
                if final.exists():
                    final.unlink()
                raise DocumentCacheError(
                    "This version is already cached with different content; use a new upstream version",
                    409, "REINS_DOCUMENT_VERSION_CONFLICT",
                )
            conn.commit()
            conn.close()
            return {"status": "ready", "reused": True, "sha256": row["sha256"],
                    "web_path": row["local_path"]}
        if row:
            cache_id = row["id"]
            conn.execute(
                """UPDATE reins_document_cache SET status='ready',local_path=?,sha256=?,size_bytes=?,
                   expected_cost_yen=?,requested_by=?,error='',completed_at=? WHERE id=?""",
                (web_path, sha, size, max(0, int(cost_yen or 0)), actor[:120], now, cache_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO reins_document_cache
                   (reins_id,document_type,upstream_version,status,local_path,sha256,size_bytes,
                    expected_cost_yen,requested_by,requested_at,completed_at)
                   VALUES (?,?,?,'ready',?,?,?,?,?,?,?)""",
                (reins_id, document_type, upstream_version, web_path, sha, size,
                 max(0, int(cost_yen or 0)), actor[:120], now, now),
            )
            cache_id = cur.lastrowid
        conn.execute(
            "INSERT INTO reins_document_ledger(cache_id,event_type,actor,amount_yen,note,created_at) VALUES (?,?,?,?,?,?)",
            (cache_id, "manual_purchase_uploaded", actor[:120], max(0, int(cost_yen or 0)), sha, now),
        )
        conn.execute(
            "UPDATE listings SET reins_drawing_pdf=?, updated_at=datetime('now') WHERE reins_id=?",
            (web_path, reins_id),
        )
        conn.commit()
        conn.close()
        return {"status": "ready", "reused": False, "sha256": sha, "size_bytes": size, "web_path": web_path}
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def get_cached_drawing(reins_id, upstream_version=""):
    reins_id, document_type, upstream_version = _validate(reins_id, "drawing", upstream_version)
    conn = get_db()
    ensure_tables(conn)
    if upstream_version == "unknown":
        row = conn.execute(
            """SELECT * FROM reins_document_cache WHERE reins_id=? AND document_type='drawing'
               AND status='ready' ORDER BY id DESC LIMIT 1""", (reins_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM reins_document_cache WHERE reins_id=? AND document_type='drawing'
               AND upstream_version=? AND status='ready' ORDER BY id DESC LIMIT 1""",
            (reins_id, upstream_version),
        ).fetchone()
    valid = _file_matches(row)
    path = row["local_path"] if valid else ""
    conn.close()
    return path
