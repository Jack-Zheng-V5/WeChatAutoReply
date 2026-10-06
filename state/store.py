"""状态存储：已处理消息指纹 + 待回复状态（DESIGN.md §4/§5）。

- 指纹双重：区域 hash + 内容 hash 成对记录——同一文本出现在不同位置
  （重新发的一条相同消息）不算重复，只有"同位置同文本"才视为已处理；
- 待回复状态独立于红点维护（实测红点随点开消失，2026-09-19）；
- 仅主循环串行访问（铁律 7），无锁；JSON 落盘，data/ 不入库。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_KEEP = 100  # 每联系人保留的指纹上限


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def region_hash(rect: tuple[int, int, int, int]) -> str:
    x, y, w, h = rect
    return hashlib.sha1(f"{x // 4},{y // 4},{w},{h}".encode()).hexdigest()[:12]


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"contacts": {}}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        path.parent.mkdir(parents=True, exist_ok=True)

    def _contact(self, name: str) -> dict:
        contact = self.data["contacts"].setdefault(
            name, {
                "processed": [], "pending_reply": False, "history": [],
                "last_reply_ts": None, "daily_date": "", "daily_count": 0,
            }
        )
        # 旧版 state.json 存量条目缺新键时逐键补齐（M2 数据无 history 等，
        # setdefault 只兜底"联系人不存在"，不兜底"键缺失"——2026-09-26 实测踩坑）
        for key, default in (
            ("history", []), ("pending_reply", False), ("last_reply_ts", None),
            ("daily_date", ""), ("daily_count", 0),
        ):
            contact.setdefault(key, default)
        return contact

    # ── 会话历史（DESIGN.md §6：每联系人 8~10 轮滑动窗口，含我方回复）──

    def append_history(self, name: str, role: str, text: str, ts: str) -> None:
        history = self._contact(name)["history"]
        history.append({"role": role, "text": text, "ts": ts})
        del history[:-MAX_KEEP]
        self._save()

    def history_window(self, name: str, max_messages: int) -> list[dict]:
        """最近 max_messages 条（约 max_messages/2 轮）。"""
        return list(self._contact(name)["history"][-max_messages:])

    # ── 闸门 4 频控所需字段 ──

    def get_last_reply(self, name: str) -> str | None:
        return self._contact(name).get("last_reply_ts")

    def daily_count(self, name: str, today: str | None = None) -> tuple[int, int]:
        """返回（该联系人今日回复数, 全局今日回复数）。按本地日期滚动。

        today 由调用方传入（闸门 4 的判定时间与计数日期必须一致，跨午夜才不会错），
        缺省 = 当前日期。
        """
        today = today or datetime.now().strftime("%Y-%m-%d")
        c = self._contact(name)
        c_count = c["daily_count"] if c.get("daily_date") == today else 0
        meta = self.data.setdefault("meta", {})
        g_count = meta["daily_count"] if meta.get("daily_date") == today else 0
        return c_count, g_count

    def record_reply(self, name: str, ts: str) -> None:
        """记录一次回复（M3 干跑=模拟，M4=真实发送时间），日计数按日期滚动。"""
        today = ts[:10]
        c = self._contact(name)
        c["last_reply_ts"] = ts
        c["daily_count"] = c["daily_count"] + 1 if c.get("daily_date") == today else 1
        c["daily_date"] = today
        meta = self.data.setdefault("meta", {})
        meta["daily_count"] = meta["daily_count"] + 1 if meta.get("daily_date") == today else 1
        meta["daily_date"] = today
        self._save()

    # ── 既有指纹/待回复 ──

    def seen(self, name: str, rhash: str, chash: str) -> bool:
        return [rhash, chash] in self._contact(name)["processed"]

    def mark_processed(self, name: str, rhash: str, chash: str) -> None:
        processed = self._contact(name)["processed"]
        processed.append([rhash, chash])
        del processed[:-MAX_KEEP]
        self._save()

    def set_pending(self, name: str, flag: bool) -> None:
        self._contact(name)["pending_reply"] = flag
        self._save()

    def pending_contacts(self) -> list[str]:
        return [n for n, c in self.data["contacts"].items() if c.get("pending_reply")]

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
