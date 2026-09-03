# -*- coding: utf-8 -*-
"""Local REINS drawing PDF interior photo extraction helpers.

MVP goals:
- Prefer original embedded image/XObject extraction.
- Fall back to local high-resolution page rendering only when no usable
  embedded photo candidates are found.
- Never call external image/PDF services.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pymupdf
from PIL import Image, ImageOps, ImageStat

BASE_DIR = Path(__file__).resolve().parent
CONTROLLED_UPLOAD_ROOT = BASE_DIR / "uploads" / "reins-extracted"
CONTROLLED_TEMP_ROOT = CONTROLLED_UPLOAD_ROOT / "_preview"

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 3
MAX_CANDIDATES = 24
MAX_OUTPUT_EDGE = 1400
MIN_EMBED_WIDTH = 180
MIN_EMBED_HEIGHT = 120
MIN_FALLBACK_SIDE = 160
MIN_MANUAL_CROP_SIDE = 32
MAX_MANUAL_CROPS_PER_PAGE = 20

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ROOM_LABELS = {"客廳", "睡房", "廚房", "餐廳", "書房", "LDK", "其他"}
PHOTO_CATEGORIES = {"室內照片", "外觀", "戶型圖", "景觀", "設備", "其他"}


@dataclass
class Candidate:
    id: str
    url: str
    temp_path: str
    page: int
    bbox: list[float]
    width: int
    height: int
    quality: float
    classification: str
    excluded: bool
    reason: str
    method: str
    source_xref: int | None = None
    source_image_id: str | None = None
    normalized_crop: dict | None = None

    def to_dict(self):
        d = asdict(self)
        d.pop("temp_path", None)
        return d


@dataclass
class SourcePage:
    id: str
    page: int
    url: str
    temp_path: str
    width: int
    height: int
    pdf_width: float
    pdf_height: float

    def to_dict(self):
        d = asdict(self)
        d.pop("temp_path", None)
        return d


class ExtractionError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def safe_listing_id(listing_id: str) -> str:
    listing_id = str(listing_id or "").strip()
    if not SAFE_ID_RE.match(listing_id):
        raise ExtractionError("invalid listing id", 400)
    return listing_id


def web_to_abs_upload_path(web_path: str) -> Path:
    web_path = str(web_path or "")
    if not web_path.startswith("/uploads/reins/") or ".." in web_path.split("/"):
        raise ExtractionError("invalid REINS drawing path", 400)
    abs_path = (BASE_DIR / web_path.lstrip("/")).resolve()
    uploads_reins = (BASE_DIR / "uploads" / "reins").resolve()
    if uploads_reins not in abs_path.parents:
        raise ExtractionError("invalid REINS drawing path", 400)
    return abs_path


def _reset_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _web_path(path: Path) -> str:
    rel = path.resolve().relative_to(BASE_DIR)
    return "/" + rel.as_posix()


def _save_resized_jpeg(img: Image.Image, dest: Path) -> tuple[int, int]:
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    scale = min(1.0, MAX_OUTPUT_EDGE / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=90, optimize=True)
    return img.size


def _image_quality(img: Image.Image) -> tuple[float, str, bool, str]:
    w, h = img.size
    area = w * h
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    mean = float(stat.mean[0])
    std = float(stat.stddev[0])
    aspect = w / h if h else 0
    # Photo-like images have moderate/high tonal variation; line/text pages are
    # typically very white with low-to-moderate variation, while floorplans are
    # often high-white and very wide/full-page.
    quality = 0.0
    quality += min(45.0, area / 9000.0)
    quality += min(35.0, std * 0.55)
    quality += 10.0 if 0.7 <= aspect <= 2.2 else 0.0
    quality -= 20.0 if mean > 230 and std < 55 else 0.0
    quality = max(0.0, min(100.0, quality))

    if area < MIN_EMBED_WIDTH * MIN_EMBED_HEIGHT:
        return quality, "too_small", True, "解像度太低"
    if mean > 232 and std < 40:
        return quality, "text_or_floorplan", True, "白底線條/文字候選，預設排除"
    if aspect > 2.8 or aspect < 0.35:
        return quality, "text_or_banner", True, "比例似文字/橫幅，預設排除"
    return quality, "interior_photo", False, "室內相片候選"


def _manifest_path(listing_id: str) -> Path:
    return CONTROLLED_TEMP_ROOT / safe_listing_id(listing_id) / "manifest.json"


def _write_manifest(listing_id: str, drawing_pdf: str, candidates: list[Candidate]):
    source_pages = _render_source_pages(drawing_pdf, CONTROLLED_TEMP_ROOT / safe_listing_id(listing_id))
    manifest = {
        "listing_id": listing_id,
        "drawing_pdf": drawing_pdf,
        "session_id": uuid.uuid4().hex,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_pages": [asdict(p) for p in source_pages],
        "candidates": [asdict(c) for c in candidates],
    }
    path = _manifest_path(listing_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_manifest(listing_id: str) -> dict:
    path = _manifest_path(listing_id)
    if not path.exists():
        raise ExtractionError("preview candidates not found; run preview first", 400)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise ExtractionError("invalid preview manifest", 400)


def _write_raw_manifest(listing_id: str, manifest: dict):
    path = _manifest_path(listing_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_source_pages(drawing_pdf: str, out_dir: Path) -> list[SourcePage]:
    pdf_path = web_to_abs_upload_path(drawing_pdf)
    pages: list[SourcePage] = []
    doc = pymupdf.open(str(pdf_path))
    try:
        for page_no, page in enumerate(doc, 1):
            if page_no > MAX_PAGES:
                break
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), alpha=False)
            dest = out_dir / f"source_page_{page_no}.jpg"
            dest.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(dest), jpg_quality=92)
            pages.append(SourcePage(
                id=f"page_{page_no}",
                page=page_no,
                url=_web_path(dest),
                temp_path=str(dest.resolve()),
                width=int(pix.width),
                height=int(pix.height),
                pdf_width=round(float(page.rect.width), 2),
                pdf_height=round(float(page.rect.height), 2),
            ))
    finally:
        doc.close()
    return pages


def _normalize_bbox(bbox: list[float], page) -> dict | None:
    try:
        x0, y0, x1, y1 = [float(v) for v in bbox]
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)
        if page_w <= 0 or page_h <= 0 or x1 <= x0 or y1 <= y0:
            return None
        return {
            "x": max(0.0, min(1.0, x0 / page_w)),
            "y": max(0.0, min(1.0, y0 / page_h)),
            "width": max(0.0, min(1.0, (x1 - x0) / page_w)),
            "height": max(0.0, min(1.0, (y1 - y0) / page_h)),
        }
    except Exception:
        return None


def _validate_manual_crop(crop: dict, source: dict) -> tuple[int, int, int, int]:
    if not isinstance(crop, dict):
        raise ExtractionError("crop must be an object", 422)
    values = {}
    for key in ("x", "y", "width", "height"):
        try:
            value = float(crop.get(key))
        except Exception:
            raise ExtractionError(f"invalid crop {key}", 422)
        if not math.isfinite(value):
            raise ExtractionError(f"invalid crop {key}", 422)
        if value < 0 or value > 1:
            raise ExtractionError(f"crop {key} out of range", 422)
        values[key] = value
    if values["width"] <= 0 or values["height"] <= 0:
        raise ExtractionError("crop area must be greater than zero", 422)
    if values["x"] + values["width"] > 1 or values["y"] + values["height"] > 1:
        raise ExtractionError("crop exceeds source image bounds", 422)

    src_w = int(source.get("width") or 0)
    src_h = int(source.get("height") or 0)
    if src_w <= 0 or src_h <= 0:
        raise ExtractionError("invalid source image metadata", 400)
    left = int(round(values["x"] * src_w))
    top = int(round(values["y"] * src_h))
    right = int(round((values["x"] + values["width"]) * src_w))
    bottom = int(round((values["y"] + values["height"]) * src_h))
    left = max(0, min(src_w, left))
    top = max(0, min(src_h, top))
    right = max(0, min(src_w, right))
    bottom = max(0, min(src_h, bottom))
    if right - left < MIN_MANUAL_CROP_SIDE or bottom - top < MIN_MANUAL_CROP_SIDE:
        raise ExtractionError("crop is too small", 422)
    return left, top, right, bottom


def _preview_asset_path_from_manifest(listing_id: str, asset_id: str, manifest: dict) -> Path:
    listing_id = safe_listing_id(listing_id)
    asset_id = str(asset_id or "").strip()
    if not SAFE_ID_RE.match(asset_id):
        raise ExtractionError("invalid preview asset id", 404)
    assets = list(manifest.get("source_pages") or []) + list(manifest.get("candidates") or [])
    asset = next((a for a in assets if a.get("id") == asset_id), None)
    if not asset:
        raise ExtractionError("preview asset not found", 404)
    path = Path(asset.get("temp_path") or "").resolve()
    root = (CONTROLLED_TEMP_ROOT / listing_id).resolve()
    if root not in path.parents or not path.exists():
        raise ExtractionError("preview asset path is not allowed", 403)
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ExtractionError("preview asset type is not allowed", 403)
    return path


def preview_asset_path(listing_id: str, asset_id: str) -> tuple[Path, dict]:
    listing_id = safe_listing_id(listing_id)
    manifest = load_manifest(listing_id)
    path = _preview_asset_path_from_manifest(listing_id, asset_id, manifest)
    return path, manifest


def _embedded_candidates(doc, page, page_no: int, out_dir: Path) -> list[Candidate]:
    results: list[Candidate] = []
    seen_xrefs = set()
    for img in page.get_images(full=True):
        xref = int(img[0])
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            info = doc.extract_image(xref)
            raw = info.get("image") or b""
            im = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            continue
        rects = page.get_image_rects(xref)
        bbox = [round(float(v), 2) for v in (rects[0] if rects else page.rect)]
        q, cls, excluded, reason = _image_quality(im)
        # Full-page embedded raster is the whole mysol/listing sheet, not a room photo.
        page_area = float(page.rect.width * page.rect.height) or 1.0
        rect_area = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        if rect_area / page_area > 0.55:
            cls, excluded, reason = "composite_page", True, "整張組合圖/圖面，預設排除"
            q = min(q, 20.0)
        cid = hashlib.sha256(f"embedded:{page_no}:{xref}:{len(raw)}".encode()).hexdigest()[:16]
        dest = out_dir / f"cand_{page_no}_{cid}.jpg"
        out_w, out_h = _save_resized_jpeg(im, dest)
        results.append(Candidate(
            cid, _web_path(dest), str(dest.resolve()), page_no, bbox, out_w, out_h,
            round(q, 1), cls, excluded, reason, "embedded_xobject", xref,
            f"page_{page_no}", _normalize_bbox(bbox, page),
        ))
    return results


def _fallback_candidates(page, page_no: int, out_dir: Path) -> list[Candidate]:
    # Simple deterministic local fallback: render high-res, then crop likely photo
    # cells from the left/top photo block. This is only used when no embedded
    # interior photos survived classification.
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    candidates = []
    # Common REINS layout: four photos in left-top grid. Relative boxes are broad
    # and are reclassified, so text/floorplan crops stay excluded.
    boxes = [
        (0.005, 0.30, 0.21, 0.47),
        (0.005, 0.47, 0.21, 0.63),
        (0.16, 0.30, 0.36, 0.47),
        (0.16, 0.47, 0.36, 0.63),
    ]
    for i, (x1, y1, x2, y2) in enumerate(boxes, 1):
        crop = img.crop((int(x1 * img.width), int(y1 * img.height), int(x2 * img.width), int(y2 * img.height)))
        if crop.width < MIN_FALLBACK_SIDE or crop.height < MIN_FALLBACK_SIDE:
            continue
        q, cls, excluded, reason = _image_quality(crop)
        cid = hashlib.sha256(f"fallback:{page_no}:{i}:{crop.size}".encode()).hexdigest()[:16]
        dest = out_dir / f"fallback_{page_no}_{cid}.jpg"
        out_w, out_h = _save_resized_jpeg(crop, dest)
        bbox = [round(x1 * float(page.rect.width), 2), round(y1 * float(page.rect.height), 2), round(x2 * float(page.rect.width), 2), round(y2 * float(page.rect.height), 2)]
        candidates.append(Candidate(
            cid, _web_path(dest), str(dest.resolve()), page_no, bbox, out_w, out_h,
            round(q, 1), cls, excluded, reason, "render_fallback", None,
            f"page_{page_no}", {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1},
        ))
    return candidates


def preview_candidates(listing_id: str, drawing_pdf_web: str) -> dict:
    listing_id = safe_listing_id(listing_id)
    pdf_path = web_to_abs_upload_path(drawing_pdf_web)
    if not pdf_path.exists():
        raise ExtractionError("drawing PDF not found", 404)
    if pdf_path.stat().st_size > MAX_PDF_BYTES:
        raise ExtractionError("drawing PDF too large", 413)

    out_dir = CONTROLLED_TEMP_ROOT / listing_id
    _reset_dir(out_dir)
    candidates: list[Candidate] = []
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:
        raise ExtractionError(f"cannot open drawing PDF: {e}", 400)
    try:
        if doc.page_count > MAX_PAGES:
            raise ExtractionError(f"PDF page limit exceeded ({doc.page_count}>{MAX_PAGES})", 413)
        for page_no, page in enumerate(doc, 1):
            candidates.extend(_embedded_candidates(doc, page, page_no, out_dir))
        if not any(c.classification == "interior_photo" and not c.excluded for c in candidates):
            for page_no, page in enumerate(doc, 1):
                candidates.extend(_fallback_candidates(page, page_no, out_dir))
    finally:
        doc.close()

    candidates = candidates[:MAX_CANDIDATES]
    _write_manifest(listing_id, drawing_pdf_web, candidates)
    manifest = load_manifest(listing_id)
    source_pages = []
    for page in manifest.get("source_pages", []):
        public = dict(page)
        public.pop("temp_path", None)
        source_pages.append(public)
    return {
        "code": 1,
        "listing_id": listing_id,
        "source_pdf": drawing_pdf_web,
        "candidate_count": len(candidates),
        "included_count": sum(1 for c in candidates if not c.excluded and c.classification == "interior_photo"),
        "limits": {"max_pages": MAX_PAGES, "max_candidates": MAX_CANDIDATES, "max_output_edge": MAX_OUTPUT_EDGE},
        "source_pages": source_pages,
        "candidates": [c.to_dict() for c in candidates],
    }


def create_manual_crop(listing_id: str, drawing_pdf_web: str, payload: dict) -> dict:
    listing_id = safe_listing_id(listing_id)
    if not isinstance(payload, dict):
        raise ExtractionError("request body must be an object", 400)
    allowed = {"source_image_id", "crop", "category", "temp_id"}
    if set(payload) - allowed:
        raise ExtractionError("unsupported manual crop field", 400)

    manifest = load_manifest(listing_id)
    if manifest.get("listing_id") != listing_id or manifest.get("drawing_pdf") != drawing_pdf_web:
        raise ExtractionError("preview manifest does not match listing/PDF", 403)
    source_id = str(payload.get("source_image_id") or "").strip()
    temp_id = str(payload.get("temp_id") or "").strip()
    if not SAFE_ID_RE.match(temp_id):
        raise ExtractionError("invalid temporary crop id", 400)
    category = str(payload.get("category") or "其他").strip()
    if category not in PHOTO_CATEGORIES:
        raise ExtractionError("invalid category", 422)

    source_pages = manifest.get("source_pages") or []
    by_source = {p.get("id"): p for p in source_pages}
    source = by_source.get(source_id)
    if not source:
        raise ExtractionError("unknown source image", 400)

    manual_on_page = [
        c for c in manifest.get("candidates", [])
        if c.get("method") == "manual_crop" and c.get("source_image_id") == source_id and c.get("id") != temp_id
    ]
    if len(manual_on_page) >= MAX_MANUAL_CROPS_PER_PAGE:
        raise ExtractionError("too many manual crops on this page", 422)
    crop_payload = payload.get("crop")
    if not isinstance(crop_payload, dict):
        raise ExtractionError("crop must be an object", 422)

    src = Path(source.get("temp_path") or "").resolve()
    tmp_root = (CONTROLLED_TEMP_ROOT / listing_id).resolve()
    if tmp_root not in src.parents or not src.exists():
        raise ExtractionError("source image path is not allowed", 403)
    box = _validate_manual_crop(crop_payload, source)
    with Image.open(src) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        cropped = image.crop(box)
        out_dir = tmp_root / "manual"
        dest = (out_dir / f"{temp_id}.jpg").resolve()
        if out_dir.resolve() not in dest.parents:
            raise ExtractionError("invalid manual crop path", 403)
        out_w, out_h = _save_resized_jpeg(cropped, dest)

    x0, y0, x1, y1 = box
    candidate = {
        "id": temp_id,
        "url": _web_path(dest),
        "temp_path": str(dest),
        "page": source.get("page"),
        "bbox": [x0, y0, x1, y1],
        "width": out_w,
        "height": out_h,
        "quality": None,
        "classification": category,
        "excluded": False,
        "reason": "手動裁切",
        "method": "manual_crop",
        "source_xref": None,
        "source_image_id": source_id,
        "normalized_crop": {
            "x": float(crop_payload["x"]),
            "y": float(crop_payload["y"]),
            "width": float(crop_payload["width"]),
            "height": float(crop_payload["height"]),
        },
        "crop_pixels": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
    }
    candidates = [c for c in manifest.get("candidates", []) if c.get("id") != temp_id]
    candidates.append(candidate)
    manifest["candidates"] = candidates
    _write_raw_manifest(listing_id, manifest)
    public = dict(candidate)
    public.pop("temp_path", None)
    return {"code": 1, "candidate": public}


def confirm_candidates(listing_id: str, drawing_pdf_web: str, selections: Iterable[dict]) -> tuple[list[dict], int]:
    listing_id = safe_listing_id(listing_id)
    manifest = load_manifest(listing_id)
    if manifest.get("listing_id") != listing_id or manifest.get("drawing_pdf") != drawing_pdf_web:
        raise ExtractionError("preview manifest does not match listing/PDF", 403)
    by_id = {c.get("id"): c for c in manifest.get("candidates", [])}
    selected = list(selections or [])
    if not selected:
        raise ExtractionError("no candidates selected", 400)
    if len(selected) > MAX_CANDIDATES:
        raise ExtractionError("too many selected candidates", 400)

    out_dir = CONTROLLED_UPLOAD_ROOT / listing_id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for sel in selected:
        cid = str(sel.get("id") or "").strip()
        cand = by_id.get(cid)
        if not cand:
            raise ExtractionError("unknown candidate", 400)
        if cand.get("excluded") or (cand.get("method") != "manual_crop" and cand.get("classification") != "interior_photo"):
            raise ExtractionError("excluded/non-interior candidate cannot be confirmed", 400)
        src = Path(cand.get("temp_path") or "").resolve()
        tmp_root = (CONTROLLED_TEMP_ROOT / listing_id).resolve()
        if tmp_root not in src.parents or not src.exists():
            raise ExtractionError("candidate path is not allowed", 403)
        room_label = str(sel.get("room_label") or sel.get("category") or "其他").strip()
        if cand.get("method") == "manual_crop" and room_label not in PHOTO_CATEGORIES:
            room_label = cand.get("classification") if cand.get("classification") in PHOTO_CATEGORIES else "其他"
        elif room_label not in ROOM_LABELS:
            room_label = "其他"
        fname = f"{cid}.jpg"
        dest = (out_dir / fname).resolve()
        if out_dir.resolve() not in dest.parents:
            raise ExtractionError("invalid output path", 403)
        shutil.copy2(src, dest)
        meta = {
            "url": _web_path(dest),
            "photo_category": room_label,
            "room_label": room_label,
            "source": "reins_drawing",
            "extraction_method": cand.get("method") or "auto",
            "source_image_id": cand.get("source_image_id"),
            "normalized_crop": cand.get("normalized_crop"),
            "crop_pixels": cand.get("crop_pixels"),
            "page": cand.get("page"),
            "bbox": cand.get("bbox"),
            "width": cand.get("width"),
            "height": cand.get("height"),
            "quality": cand.get("quality"),
            "classification": cand.get("classification"),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        saved.append(meta)
    return saved, len(selected)
