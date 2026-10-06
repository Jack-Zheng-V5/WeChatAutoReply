"""未读红点检测（DESIGN.md §3-③）。

M1 阶段只检测与标注（输出日志 + 标注图），不产生任何点击。
检测策略：HSV 双区间红色掩码（覆盖 H 首尾）→ 形态学去噪 → 连通域按
尺寸/面积/填充率过滤。阈值基于实测校准，标注图供人工核对误检/漏检。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class RedDotConfig:
    # 红色在 OpenCV 的 H(0-179) 上横跨 0 与 179 两端，需双区间
    h_lo1: int = 0
    h_hi1: int = 8
    h_lo2: int = 170
    h_hi2: int = 179
    s_min: int = 120
    v_min: int = 120
    min_side: int = 5    # 徽标外接框边长下限。注意：实测（2026-09-19）微信未读徽标约 8px，
                         # 经 3x3 开运算后缩到 7~8px——阈值必须按开运算后的尺寸设，否则整体漏检
    max_side: int = 60
    min_area: int = 40   # 连通域面积下限
    min_fill: float = 0.4  # 面积/外接框，过滤细长噪声
    badge_min_v: int = 235  # 组件中位 V 下限：未读徽标是固定品牌红（#FA5151，实测 V≈250）；
                            # 头像/图标里的深红碎片（如 ITADN logo 红舌头 V≈205）据此排除


@dataclass
class RedDot:
    x: int
    y: int
    w: int
    h: int
    area: int
    cx: float
    cy: float
    v_median: float = 0.0  # 组件红色像素的中位 V（审计用）


def red_mask(image_bgr: np.ndarray, cfg: RedDotConfig | None = None) -> np.ndarray:
    cfg = cfg or RedDotConfig()
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (cfg.h_lo1, cfg.s_min, cfg.v_min), (cfg.h_hi1, 255, 255))
    m2 = cv2.inRange(hsv, (cfg.h_lo2, cfg.s_min, cfg.v_min), (cfg.h_hi2, 255, 255))
    mask = m1 | m2
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)


def detect_red_dots(
    image_bgr: np.ndarray, cfg: RedDotConfig | None = None
) -> tuple[list[RedDot], np.ndarray]:
    """返回（按 y 从上到下排序的红点列表, 掩码图）。"""
    cfg = cfg or RedDotConfig()
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (cfg.h_lo1, cfg.s_min, cfg.v_min), (cfg.h_hi1, 255, 255))
    m2 = cv2.inRange(hsv, (cfg.h_lo2, cfg.s_min, cfg.v_min), (cfg.h_hi2, 255, 255))
    mask = cv2.morphologyEx(
        m1 | m2, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    )
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    dots: list[RedDot] = []
    for i in range(1, count):
        x, y, w, h, area = (int(stats[i, j]) for j in range(5))
        if not (cfg.min_side <= w <= cfg.max_side and cfg.min_side <= h <= cfg.max_side):
            continue
        if area < cfg.min_area:
            continue
        if area / (w * h) < cfg.min_fill:
            continue
        ys, xs = np.where(mask[y:y + h, x:x + w] > 0)
        v_median = float(np.median(hsv[y + ys, x + xs][:, 2]))
        if v_median < cfg.badge_min_v:
            continue  # 深红碎片（头像/logo/图标），非品牌红徽标
        cx, cy = centroids[i]
        dots.append(RedDot(x, y, w, h, area, float(cx), float(cy), v_median))
    dots.sort(key=lambda d: (d.cy, d.cx))
    return dots, mask


def annotate(image_bgr: np.ndarray, dots: list[RedDot]) -> np.ndarray:
    """标注图：绿框 + 序号（自上而下），供人工核对。"""
    out = image_bgr.copy()
    for i, d in enumerate(dots):
        cv2.rectangle(out, (d.x, d.y), (d.x + d.w, d.y + d.h), (0, 255, 0), 2)
        cv2.putText(
            out, str(i), (d.x - 6, d.y - 6),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA,
        )
    return out
