"""OCR 封装（DESIGN.md §3-⑥）。

选定引擎：RapidOCR（rapidocr-onnxruntime，PP-OCR 中文模型），2026-09-19 实测选型，
详见 Progress.md。注意：该引擎返回的置信度字段为字符串，本模块统一转 float。

实测结论：气泡文本先裁剪再 3x 放大识别，质量优于全窗直识（低置信尾行问题消失），
默认 upscale=3.0。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


@dataclass
class OcrLine:
    x: int
    y: int
    w: int
    h: int
    text: str
    score: float


def _to_lines(result, upscale: float) -> list[OcrLine]:
    lines: list[OcrLine] = []
    for box, text, score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            OcrLine(
                x=int(min(xs) / upscale), y=int(min(ys) / upscale),
                w=int((max(xs) - min(xs)) / upscale),
                h=int((max(ys) - min(ys)) / upscale),
                text=text, score=float(score),
            )
        )
    lines.sort(key=lambda ln: (ln.y, ln.x))
    return lines


def ocr_image(image_bgr: np.ndarray, *, upscale: float = 1.0) -> list[OcrLine]:
    """对图像做 OCR，返回按 (y, x) 排序的文本行；upscale>1 时坐标已还原到原尺度。"""
    img = image_bgr
    if upscale != 1.0:
        img = cv2.resize(
            image_bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC
        )
    result, _ = _engine()(img)
    return _to_lines(result, upscale)


def merged_text(lines: list[OcrLine]) -> str:
    """行文本合并（中文无空格，直接拼接；顺序即 ocr_image 的排序）。"""
    return "".join(ln.text for ln in lines)


def avg_score(lines: list[OcrLine]) -> float:
    if not lines:
        return 0.0
    return sum(ln.score for ln in lines) / len(lines)
