"""审计日报（M4 每日复查入口，DESIGN.md §8/§10）。

用法：python tools/report.py [天数]    # 默认打印今天，可指定最近 N 天
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "audit.db"


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    if not DB.exists():
        print("审计库不存在（尚未有任何回复处理）:", DB)
        return 1
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ts, contact, incoming, reply, sent, block_reason, tokens"
        " FROM reply_audit WHERE ts >= ? ORDER BY ts", (since,)
    ).fetchall()
    sent = sum(r[4] for r in rows)
    print(f"最近 {days} 天（自 {since}）：共 {len(rows)} 条回复，已发送 {sent}，"
          f"未发送 {len(rows) - sent}\n")
    for ts, contact, incoming, reply, sent_flag, reason, tokens in rows:
        flag = "已发" if sent_flag else f"未发[{reason}]"
        print(f"{ts} [{flag}] {contact}")
        print(f"    来信: {(incoming or '')[:46]}")
        print(f"    回复: {(reply or '')[:46]} (tokens={tokens})")
    blocked = [r for r in rows if not r[4]]
    if blocked:
        print(f"\n需关注的未发送记录 {len(blocked)} 条（拦截原因分布）：")
        from collections import Counter
        for reason, n in Counter(r[5].split(":")[0] for r in blocked).items():
            print(f"    {reason}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
