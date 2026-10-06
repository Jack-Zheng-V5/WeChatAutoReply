"""闸门 4：频控（DESIGN.md §8/§11）。

同联系人最小回复间隔（+随机抖动）、每联系人/全局每日上限、夜间免扰。
返回"本条若在 M4 是否允许发送"的判定理由列表；M3 干跑期间回复照常生成
供审阅，但判定结果如实记录（夜间/超频 = M4 将不发送）。
"""
from __future__ import annotations

import random
from datetime import datetime


def _in_night(now: datetime, cfg: dict) -> bool:
    start, end = int(cfg["night_start"]), int(cfg["night_end"])
    hour = now.hour
    if start > end:  # 跨午夜区间（如 23~8）
        return hour >= start or hour < end
    return start <= hour < end


def evaluate(state, name: str, cfg: dict, now: datetime | None = None) -> list[str]:
    """返回全部拦截理由；空列表 = 允许发送（M4 语义）。"""
    now = now or datetime.now()
    reasons: list[str] = []
    if _in_night(now, cfg):
        reasons.append(f"夜间免扰({int(cfg['night_start'])}:00–{int(cfg['night_end'])}:00)")
    last = state.get_last_reply(name)
    if last:
        min_interval = float(cfg["reply_min_interval"])
        wait = min_interval + random.uniform(0, float(cfg["reply_jitter"]))
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        if elapsed < wait:
            reasons.append(f"距上次回复 {elapsed:.0f}s < {wait:.0f}s")
    c_count, g_count = state.daily_count(name, today=now.strftime("%Y-%m-%d"))
    if c_count >= int(cfg["daily_per_contact"]):
        reasons.append("联系人日上限")
    if g_count >= int(cfg["daily_global"]):
        reasons.append("全局日上限")
    return reasons
