# -*- coding: utf-8 -*-
"""
REINS drawing PDF → image renderer.

本地腳本 render，唔用 AI / OCR。
用 PyMuPDF get_pixmap，150~200 DPI，保留原比例。
output：<output_dir>/drawing_page_<n>.jpg

如果 PDF 更新，重新 render 並清理舊 page image，唔留 duplicate。
"""
import os
import glob

import pymupdf

# 目標 DPI（web display 足夠）
DEFAULT_DPI = 180


def render_drawing_pdf(pdf_path, output_dir, dpi=DEFAULT_DPI):
    """
    將 drawing.pdf 每頁 render 成 JPEG。
    - 保留原比例
    - 輸出 <output_dir>/drawing_page_<n>.jpg
    - 重新 render 前清理舊 drawing_page_*.jpg（唔留 page_1(1).jpg 呢類 duplicate）

    返回 list[str]：相對 web path 唔係絕對 path（由 caller 決定點轉）。
    呢度返回絕對 file path list。
    """
    if not os.path.exists(pdf_path):
        return []

    os.makedirs(output_dir, exist_ok=True)

    # 清理舊 render（唔留 duplicate）
    for old in glob.glob(os.path.join(output_dir, 'drawing_page_*.jpg')):
        try:
            os.remove(old)
        except OSError:
            pass

    zoom = dpi / 72.0  # PDF 預設 72 DPI
    mat = pymupdf.Matrix(zoom, zoom)

    out_paths = []
    doc = pymupdf.open(pdf_path)
    try:
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out = os.path.join(output_dir, f'drawing_page_{i}.jpg')
            # 用 JPEG（quality 85）——圖面係線條圖，JPEG 細啲
            pix.save(out, jpg_quality=85)
            out_paths.append(out)
    finally:
        doc.close()

    return out_paths


def render_drawing_pdf_web_paths(reins_id, base_dir, dpi=DEFAULT_DPI):
    """
    方便函數：畀 reins_id 同 platform base_dir，
    render uploads/reins/<reins_id>/drawing.pdf → drawing_page_N.jpg，
    返回 web path list（/uploads/reins/<reins_id>/drawing_page_N.jpg）。
    """
    pdf_path = os.path.join(base_dir, 'uploads', 'reins', reins_id, 'drawing.pdf')
    out_dir = os.path.join(base_dir, 'uploads', 'reins', reins_id)
    abs_paths = render_drawing_pdf(pdf_path, out_dir, dpi=dpi)
    return [
        '/uploads/reins/{}/{}'.format(reins_id, os.path.basename(p))
        for p in abs_paths
    ]
