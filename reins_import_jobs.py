# -*- coding: utf-8 -*-
"""
REINS background import job store + worker.

設計：
- 唔用 Redis/Celery。用 thread + JSON 檔案持久化 job 狀態（重啟後可以 resume）。
- concurrency=1：一個 worker thread 逐件處理，唔 parallel。
- 每件狀態：pending / processing / success / partial / failed。
- REINS session expired → 停止後續，未完成保持 pending，唔 retry login。

Job 狀態存 data/import_jobs/<job_id>.json。
"""
import os
import json
import threading
import time
import uuid
from datetime import datetime, timezone

_JOBS_DIR = os.path.join(os.path.dirname(__file__), 'data', 'import_jobs')
_LOCK = threading.Lock()
# in-memory cache：job_id -> dict（同檔案同步）
_JOBS = {}
# 每個 job 一個 worker thread handle
_WORKERS = {}

# item 狀態
ST_PENDING = 'pending'
ST_PROCESSING = 'processing'
ST_SUCCESS = 'success'
ST_PARTIAL = 'partial'
ST_FAILED = 'failed'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _job_path(job_id):
    return os.path.join(_JOBS_DIR, f'{job_id}.json')


def _persist(job):
    os.makedirs(_JOBS_DIR, exist_ok=True)
    tmp = _job_path(job['job_id']) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _job_path(job['job_id']))


def _load(job_id):
    p = _job_path(job_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def create_job(items):
    """items = [{reins_id, drawing_available}, ...]。返回 job dict。"""
    job_id = 'imp_' + uuid.uuid4().hex[:12]
    job = {
        'job_id': job_id,
        'created_at': _now(),
        'status': 'running',          # running / done / session_expired
        'total': len(items),
        'done_count': 0,
        'current_index': -1,
        'items': [
            {
                'reins_id': (it.get('reins_id') or '').strip(),
                'drawing_available': bool(it.get('drawing_available')),
                'status': ST_PENDING,
                'listing_id': None,
                'action': None,          # inserted / updated
                'has_drawing': None,     # True/False/None
                'error': None,
            }
            for it in items
        ],
        'error': None,
    }
    with _LOCK:
        _JOBS[job_id] = job
        _persist(job)
    return job


def get_job(job_id):
    with _LOCK:
        if job_id in _JOBS:
            return _JOBS[job_id]
    job = _load(job_id)
    if job:
        with _LOCK:
            _JOBS[job_id] = job
    return job


def _update_item(job, idx, **kw):
    with _LOCK:
        job['items'][idx].update(kw)
        job['done_count'] = sum(
            1 for it in job['items']
            if it['status'] in (ST_SUCCESS, ST_PARTIAL, ST_FAILED)
        )
        _persist(job)


def _set_job(job, **kw):
    with _LOCK:
        job.update(kw)
        _persist(job)


def _is_session_error(err):
    """判斷係咪 REINS session expired / login 問題。唔 retry。"""
    if not err:
        return False
    s = str(err).lower()
    return any(k in s for k in (
        'no reins session', 'login', 'session', 'auth', 'captcha',
        'manualintervention', '2fa', 'ログイン', '認証',
    ))


def _run_job(job_id):
    """Worker：逐件處理（concurrency=1）。喺 background thread 行。"""
    job = get_job(job_id)
    if not job:
        return
    from reins_client import import_reins_listing

    for idx, item in enumerate(job['items']):
        if item['status'] in (ST_SUCCESS, ST_PARTIAL):
            continue  # 已完成（resume 時 skip）
        _set_job(job, current_index=idx)
        _update_item(job, idx, status=ST_PROCESSING)

        reins_id = item['reins_id']
        try:
            r = import_reins_listing(
                reins_id,
                drawing_available=item['drawing_available'],
                headless=True,
            )
            if r.get('code') == 1:
                has_drawing = bool(r.get('drawing_pdf'))
                # partial：overview+DB 成功但 drawing 想要而冇
                want_drawing = item['drawing_available']
                if want_drawing and not has_drawing:
                    _update_item(job, idx, status=ST_PARTIAL,
                                 listing_id=r.get('id'), action=r.get('action'),
                                 has_drawing=False,
                                 error='図面取得失敗（overview+DB 已匯入）')
                else:
                    _update_item(job, idx, status=ST_SUCCESS,
                                 listing_id=r.get('id'), action=r.get('action'),
                                 has_drawing=has_drawing)
            else:
                err = r.get('error', 'unknown')
                if _is_session_error(err):
                    # session expired：停止後續，呢件同之後嘅保持 pending
                    _update_item(job, idx, status=ST_PENDING, error='session expired')
                    _set_job(job, status='session_expired',
                             error='REINS session expired — 請重新登入後用 resume 繼續')
                    return
                _update_item(job, idx, status=ST_FAILED, error=err)
        except Exception as e:
            err = str(e)
            if _is_session_error(err):
                _update_item(job, idx, status=ST_PENDING, error='session expired')
                _set_job(job, status='session_expired',
                         error='REINS session expired — 請重新登入後用 resume 繼續')
                return
            _update_item(job, idx, status=ST_FAILED, error=err)

    _set_job(job, status='done', current_index=-1)


def start_job(job_id):
    """起 background thread 行 job。已經行緊/完成就唔會再起。"""
    job = get_job(job_id)
    if not job:
        return False
    if job['status'] == 'done':
        return False
    with _LOCK:
        t = _WORKERS.get(job_id)
        if t and t.is_alive():
            return True  # 已經行緊
        t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
        _WORKERS[job_id] = t
        t.start()
    return True


def resume_job(job_id):
    """Resume：重置 session_expired → running，重起 worker。pending item 會繼續。"""
    job = get_job(job_id)
    if not job:
        return None
    if job['status'] == 'done':
        return job
    _set_job(job, status='running', error=None)
    start_job(job_id)
    return job
