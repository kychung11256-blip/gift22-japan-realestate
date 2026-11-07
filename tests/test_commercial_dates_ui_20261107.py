"""Commercial frontend/date regressions; no network or database mutations."""
import importlib.util
from pathlib import Path
import pytest
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def dates():
    path = ROOT / 'jp_date.py'
    assert path.exists(), 'commercial Japanese date parser is missing'
    spec = importlib.util.spec_from_file_location('commercial_jp_date', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.mark.parametrize('raw,expected', [
    ('平成22年2月', '2010-02'), ('令和元年5月1日', '2019-05-01'),
    ('昭和64年1月7日', '1989-01-07'), ('２０１０年２月', '2010-02'),
    ('2024/2/29', '2024-02-29'), ('2010-02', '2010-02'),
    ('2010年', '2010'), ('2023-02-29', ''), ('平成22年13月', ''),
    ('令和0年5月', ''), ('平成31年5月1日', ''), ('garbage 2010-02', ''),
])
def test_dates_preserve_precision_and_validate(raw, expected):
    assert dates().parse_japanese_date(raw) == expected

def test_js_dates_and_safe_media_runtime():
    code = """
const fs=require('fs'),vm=require('vm');
const location={origin:'http://localhost:8901',href:'http://localhost:8901/listings',pathname:'/listings'};
const ctx={window:{fetch:async()=>{}},document:{addEventListener:()=>{}},location,URL,Request,Headers,Intl,Date,Object,console};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const c=ctx.window.Commercial;
console.log(JSON.stringify({dates:['平成22年2月','令和元年5月1日','昭和64年1月7日','２０２４年２月２９日','2023-02-29','平成31年5月1日','2010-02'].map(c.date),ts:c.timestamp('2026-11-07T01:30:00Z'),safe:[c.safeMedia('/api/customer-safe-image/L/A'),c.safeMedia('https://upstream.invalid/raw.jpg'),c.safeMedia('/uploads/reins/raw.jpg')]}));
"""
    result = subprocess.run(['node', '-e', code, str(ROOT/'config/commercial.js')], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    assert data['dates'] == ['2010-02', '2019-05-01', '1989-01-07', '2024-02-29', '待確認', '待確認', '2010-02']
    assert data['ts'] == '2026-11-07 10:30 JST'
    assert data['safe'] == ['/api/customer-safe-image/L/A', '', '']


def test_timestamp_and_date_precision():
    mod = dates()
    assert mod.iso_z('2010-02') == '2010-02'
    assert mod.iso_z('2026-11-07') == '2026-11-07'
    assert mod.iso_z('2026-11-07T00:10:00+09:00') == '2026-11-06T15:10:00Z'


def test_commercial_shell_and_role_pages():
    for name in ['index.html', 'listings.html', 'listing.html', 'map.html',
                 'client_portal.html', 'privacy.html', 'terms.html']:
        text = (ROOT / name).read_text()
        assert 'lang="zh-HK"' in text
        assert '/config/commercial.js' in text
        assert 'Johnny AI' not in text
    for name in ['staff_login.html', 'client_login.html', 'admin_users.html', 'media_review.html']:
        assert '/config/commercial.js' in (ROOT / name).read_text()
    js = (ROOT / 'config/commercial.js').read_text()
    assert 'X-CSRF-Token' in js
    assert '/api/auth/session' in js
    assert '/api/public-contact-qr/whatsapp' in js
    assert 'timeZone' in js and 'Asia/Tokyo' in js
    assert 'safeMedia' in js


def test_workbench_comparison_uses_iso():
    text = (ROOT / 'workbench.html').read_text()
    assert "['築年月',p=>Commercial.date(p.builtAt)]" in text


def test_screenshot_state_and_safe_default():
    text = (ROOT / 'screenshot_tool.py').read_text()
    assert '/platform/verify_screenshots' not in text
    assert '--storage-state' in text
    assert 'SCREENSHOT_STORAGE_STATE' in text
    mod = dates()
    assert mod.display_jst('2026-11-07T01:30:00Z') == '2026-11-07 10:30 JST'
    assert mod.display_jst('平成22年2月') == '2010-02'
    assert mod.display_jst('2026-11-07') == '2026-11-07'
