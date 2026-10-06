"""闸门 3：高危词过滤（DESIGN.md §8）。

来信文本命中任一高危词 → 转人工，绝不生成回复。词库在配置中可增删。
实现注意：YAML 里未加引号的数字词（如 12315）会被解析成 int——统一 coerce
成字符串，配置书写失误不允许炸主循环（2026-09-26 实测教训，见 Progress.md）。
"""
from __future__ import annotations


def hit(text: str, banned_words: list) -> str | None:
    """返回命中的第一个高危词，无命中返回 None。"""
    for word in banned_words:
        word = str(word)
        if word and word in text:
            return word
    return None
