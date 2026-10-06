"""审计日志（DESIGN.md §8）：SQLite data/audit.db。

字段含时间、联系人、来信、回复、是否发送、拦截原因、模型、token 数、耗时。
全自动模式的信任来自事后可查（data/ 不入库）。
主循环串行访问（铁律 7），单连接无锁。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    contact TEXT NOT NULL,
    incoming TEXT,
    reply TEXT,
    sent INTEGER NOT NULL,
    block_reason TEXT,
    model TEXT,
    tokens INTEGER,
    elapsed_ms INTEGER
)
"""


class AuditLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def log(self, ts: str, contact: str, incoming: str, reply: str,
            sent: bool, block_reason: str = "", model: str | None = None,
            tokens: int | None = None, elapsed_ms: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO reply_audit (ts, contact, incoming, reply, sent,"
            " block_reason, model, tokens, elapsed_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, contact, incoming, reply, int(sent), block_reason, model,
             tokens, elapsed_ms),
        )
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self.conn.execute(sql, params).fetchall()
