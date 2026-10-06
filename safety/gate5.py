"""闸门 5：输入框闭环验证（DESIGN.md §5/§8）。

粘贴后 OCR 输入框与预期回复一致才按 Enter；Enter 后验证已清空，否则视为
发送失败。纯逻辑函数（OCR 结果 → 判定），单测覆盖；OCR 由调用方注入。
"""
from __future__ import annotations

from difflib import SequenceMatcher

PASTE_RATIO = 0.6  # 粘贴后相似度阈值：OCR 对回复文本的误字容忍（emoji 无法识别，占比小）


def verify_pasted(reply: str, ocr_text: str) -> bool:
    """粘贴后：输入框 OCR 文本应与预期回复足够相似。"""
    return SequenceMatcher(None, reply, ocr_text).ratio() >= PASTE_RATIO


def verify_cleared(reply: str, ocr_text: str) -> bool:
    """回车后：输入框应已清空（与回复不再相似）。"""
    return SequenceMatcher(None, reply, ocr_text).ratio() < PASTE_RATIO
