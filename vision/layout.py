"""窗口布局检测（DESIGN.md §3-③ 前置：分栏位置）。

分栏 x 检测（内容无关信号，2026-09-19 实测）：列表面板底色灰度稳定 <235，
聊天区背景稳定 ≈250，取暗列游程的结束处即分栏；分栏随窗口宽度/用户拖动变化
（772 宽→x≈228，823 宽→x≈387），不可写死。

教训：初版用"相邻列均值差分最大处"，聊天区内气泡/视频边缘会制造更强的竞争
差分（实测第五个白气泡把检测带偏到 x=442，顶栏名裁剪错位 → 闭环验证失败），
该信号已废弃。锚点模板 + 健康检查（DESIGN.md §9）落地后，本模块与其互为校验。
"""
from __future__ import annotations

import cv2
import numpy as np

X_MIN = 150        # 功能侧边栏 + 会话头像列在此之外
CHAT_MIN_W = 250   # 聊天区最小宽度假设
LIST_DARK = 235    # 列表面板底色灰度低于此值；聊天区背景高于此值

# 顶栏联系人名区域（实测 2026-09-19：全宽裁剪下 2 字名"香梅"检测不出，
# 收窄到 ~250px 后稳定读出——短名必须配窄裁剪）
NAME_BAND_Y = (35, 80)
NAME_CROP_W = 250


def detect_chat_split(image_bgr: np.ndarray) -> int:
    """返回会话列表与聊天区的分栏 x（聊天区首列）。"""
    h, w = image_bgr.shape[:2]
    x_max = w - CHAT_MIN_W
    if x_max <= X_MIN:
        raise ValueError(f"窗口过窄，无法定位分栏: {w}px")
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    band = gray[150: min(h, 800)]
    dark = (band < LIST_DARK).mean(axis=0)
    if dark[X_MIN] < 0.5:
        raise ValueError(f"x={X_MIN} 处不是列表面板底色，分栏检测前置条件不成立")
    x = X_MIN
    while x < x_max and dark[x] >= 0.5:
        x += 1
    return x


def chat_name_region(image_bgr: np.ndarray, split_x: int) -> np.ndarray:
    """顶栏联系人名区域裁剪（白名单闭环验证用）。"""
    return image_bgr[NAME_BAND_Y[0]:NAME_BAND_Y[1], split_x:split_x + NAME_CROP_W]


def top_bar_full_region(image_bgr: np.ndarray, split_x: int) -> np.ndarray:
    """顶栏全宽区域（群聊判定专用）。

    2026-09-27 事故根因：250px 名字裁剪会把长群名尾部的成员数后缀
    （如 "…客户群(300)" 的 "(300)"）裁掉，导致群聊被误判为 1对1 并回复进群。
    群聊判定必须用全宽 OCR。
    """
    return image_bgr[NAME_BAND_Y[0]:NAME_BAND_Y[1], split_x:]


def input_box_region(shape: tuple[int, int], split_x: int) -> tuple[int, int, int, int]:
    """输入框区域 (x, y, w, h)（闸门 5 OCR 用）：聊天区底部、工具栏上方的输入带。

    实测（823x867 / 975x1047 窗口）：输入带底边距 ≈70px（工具栏），高 ≈230px。
    锚点模板（§9）落地后改为模板定位。
    """
    h, w = shape[:2]
    return (split_x + 10, h - 300, w - split_x - 30, 230)
