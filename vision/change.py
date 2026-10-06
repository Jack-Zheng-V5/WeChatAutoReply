"""变化检测（DESIGN.md §3-②）：pHash 无变化 → 直接进入下一轮，零成本。

实测依据（M1，2026-09-19）：静止画面双捕获 pHash 距离为 0。
"""
from __future__ import annotations

import cv2
import imagehash
import numpy as np
from PIL import Image


def phash(image_bgr: np.ndarray) -> imagehash.ImageHash:
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    return imagehash.average_hash(Image.fromarray(rgb))


def is_changed(h1: imagehash.ImageHash, h2: imagehash.ImageHash, threshold: int = 2) -> bool:
    """汉明距离 > threshold 判定为画面有变化。"""
    return (h1 - h2) > threshold
