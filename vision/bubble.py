"""消息气泡分割（DESIGN.md §3-⑤/⑥）。

M2 现阶段：右侧绿色气泡（自己发出的消息；文件传输助手的手机来消息也显示为绿色）。
左侧白色气泡（真实联系人来信）的分类待真实样本后补充，暂缺——见 Progress.md 遗留项。

阈值实测依据（2026-09-19，微信 4.1.13.65，DPI 100%）：
- 4.x 气泡绿为低饱和软绿，HSV ≈ (61, 90, 242)，与资料常见的经典 #95EC69 不同，
  阈值必须按 4.x 实采样设定；
- 会话列表内也有绿色元素（选中项底色、图标），分割前必须先按聊天区左边界裁掉。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class BubbleConfig:
    h_lo: int = 35
    h_hi: int = 70
    s_min: int = 60
    v_min: int = 120
    min_w: int = 50      # 最短气泡（"短句测试"级）实测 85x36，留余量
    min_h: int = 25
    min_area: int = 1500
    chat_x: int = 0      # 聊天区左边界（窗口分栏位置，随窗口宽度变化，需调用方提供）


@dataclass
class WhiteBubbleConfig:
    v_min: int = 225
    v_max: int = 248
    s_max: int = 25
    min_w: int = 60
    min_h: int = 24
    min_area: int = 1200
    min_fill: float = 0.5
    chat_x: int = 0      # 聊天区左边界，0=全图


def find_green_bubbles(
    image_bgr: np.ndarray, cfg: BubbleConfig | None = None
) -> list[tuple[int, int, int, int]]:
    """返回按 y 排序的绿色气泡外接框 (x, y, w, h)，窗口相对坐标。"""
    cfg = cfg or BubbleConfig()
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (cfg.h_lo, cfg.s_min, cfg.v_min), (cfg.h_hi, 255, 255))
    if cfg.chat_x > 0:
        mask[:, : cfg.chat_x] = 0
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    rects = [
        (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]), int(stats[i, 3]))
        for i in range(1, count)
        if stats[i][2] >= cfg.min_w and stats[i][3] >= cfg.min_h
        and stats[i][4] >= cfg.min_area
    ]
    rects.sort(key=lambda r: (r[1], r[0]))
    return rects


def find_white_bubbles(
    image_bgr: np.ndarray, cfg: "WhiteBubbleConfig | None" = None
) -> list[tuple[int, int, int, int]]:
    """左侧白气泡（对方发来的文本消息），按 y 排序。

    实测依据（2026-09-19，微信 4.1.13.65）：气泡内部 BGR≈(240,238,238) 的低饱和
    浅灰，聊天区背景 ≈250 近白；用低饱和亮度带掩码取连通域，细长的边框/滚动条
    组件靠填充率过滤，"发送"按钮等小块靠宽度过滤。
    """
    cfg = cfg or WhiteBubbleConfig()
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, cfg.v_min), (180, cfg.s_max, cfg.v_max))
    if cfg.chat_x > 0:
        mask[:, : cfg.chat_x] = 0
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    rects = [
        (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]), int(stats[i, 3]))
        for i in range(1, count)
        if stats[i][2] >= cfg.min_w and stats[i][3] >= cfg.min_h
        and stats[i][4] >= cfg.min_area
        and stats[i][4] / (stats[i][2] * stats[i][3]) >= cfg.min_fill
    ]
    rects.sort(key=lambda r: (r[1], r[0]))
    return rects


def crop_bubble(
    image_bgr: np.ndarray, rect: tuple[int, int, int, int], pad: int = 8
) -> np.ndarray:
    """气泡裁剪（含边距），自动收在图像边界内。"""
    h, w = image_bgr.shape[:2]
    x, y, bw, bh = rect
    return image_bgr[max(0, y - pad): min(h, y + bh + pad),
                     max(0, x - pad): min(w, x + bw + pad)]
