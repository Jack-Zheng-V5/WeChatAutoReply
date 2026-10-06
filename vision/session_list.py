"""会话列表行结构检测与红点行扫描（DESIGN.md §3-③/§4）。

零点击：本模块只回答"第几行有未读红点、该行文本是什么"；点击会话由
controller 层实现（M2 后期），开发测试期仅允许作用于白名单测试会话（铁律 5）。

行结构：OCR 列表区文本行 → 按 y 聚类成行。实测（2026-09-19，DPI 100%）：
行内线距 ≈20px（名字行/预览行/时间行），行间距 ≈46px，聚类阈值 32px。
红点：复用 reddot 检测后按 x 波段过滤——未读徽标固定在头像右上角
（实测 x≈112-120），功能侧边栏的更新红点（x≈46）借此排除。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from vision import ocr, reddot

logger = logging.getLogger(__name__)

SIDEBAR_W = 65        # 功能侧边栏右边界（图标列 + 边距）
AVATAR_X = (90, 150)  # 未读徽标 x 波段
LIST_TOP = 80         # 列表区起始 y（搜索框之下）
ROW_GAP = 32          # 行内/行间线距分界
BAND_PAD = 10         # 红点归属行的 y 容差


@dataclass
class SessionRow:
    index: int
    y_top: int
    y_bottom: int
    lines: list[ocr.OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(ln.text for ln in sorted(self.lines, key=lambda l: (l.y, l.x)))


@dataclass
class RedRow:
    row: SessionRow
    dot: reddot.RedDot
    by_nearest: bool = False  # True = 红点未落入任何行带，按最近行归类的兜底


def detect_rows(
    image_bgr: np.ndarray,
    split_x: int,
    lines: list[ocr.OcrLine] | None = None,
) -> list[SessionRow]:
    """检测会话行。lines 可传入已算好的全窗 OCR 结果以避免重复识别。"""
    if lines is None:
        lines = ocr.ocr_image(image_bgr)
    list_lines = sorted(
        (ln for ln in lines if SIDEBAR_W <= ln.x < split_x and ln.y >= LIST_TOP),
        key=lambda l: (l.y, l.x),
    )
    rows: list[SessionRow] = []
    for ln in list_lines:
        if rows and ln.y - rows[-1].lines[-1].y <= ROW_GAP:
            row = rows[-1]
            row.lines.append(ln)
            row.y_bottom = max(row.y_bottom, ln.y + ln.h)
        else:
            rows.append(SessionRow(len(rows), ln.y, ln.y + ln.h, [ln]))
    return rows


def scan_red_rows(
    image_bgr: np.ndarray,
    split_x: int,
    lines: list[ocr.OcrLine] | None = None,
) -> tuple[list[RedRow], list[SessionRow]]:
    """扫描有未读红点的会话行。返回（红点行列表, 全部行）。"""
    rows = detect_rows(image_bgr, split_x, lines)
    panel = image_bgr[:, :split_x].copy()  # 列表面板内检测，排除聊天区干扰
    dots, _ = reddot.detect_red_dots(panel)
    dots = [d for d in dots if AVATAR_X[0] <= d.x <= AVATAR_X[1]]

    red_rows: list[RedRow] = []
    for dot in dots:
        match = next(
            (r for r in rows if r.y_top - BAND_PAD <= dot.cy <= r.y_bottom + BAND_PAD),
            None,
        )
        if match is not None:
            red_rows.append(RedRow(match, dot))
            continue
        # 兜底：取中心 y 最近的行，并显式告警（绝不静默）
        nearest = min(rows, key=lambda r: min(abs(dot.cy - r.y_top), abs(dot.cy - r.y_bottom)))
        logger.warning(
            "红点 (cy=%.0f) 未落入任何行带，按最近行归类: 行%d (%.0f~%.0f)",
            dot.cy, nearest.index, nearest.y_top, nearest.y_bottom,
        )
        red_rows.append(RedRow(nearest, dot, by_nearest=True))

    red_rows.sort(key=lambda rr: rr.row.index)
    return red_rows, rows
