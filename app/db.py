"""SQLite 存储层：连接、事务、schema 初始化。

并发模型：进程内全局 RLock + BEGIN IMMEDIATE 事务。
任何写操作（含"读后写"的审批/撤销竞争路径）都在同一把锁和
同一个 IMMEDIATE 事务内完成，因此并发请求被严格串行化，
同一时刻只会有一个结果落库提交。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("SEAL_DB_PATH", os.path.join("data", "seal.db"))

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE,
    api_key_hash TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES accounts(id),
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    digest     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'sealed',   -- sealed | opened
    created_at TEXT NOT NULL,
    opened_at  TEXT
);

CREATE TABLE IF NOT EXISTS document_witnesses (
    document_id INTEGER NOT NULL REFERENCES documents(id),
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (document_id, account_id)
);

CREATE TABLE IF NOT EXISTS unseal_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    reason      TEXT NOT NULL,
    deadline    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | opened | revoked | expired
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES unseal_requests(id),
    witness_id INTEGER NOT NULL REFERENCES accounts(id),
    digest     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (request_id, witness_id)              -- 同一见证人对同一请求只计一次
);

CREATE TABLE IF NOT EXISTS operation_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 单调递增，天然有序
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    document_id INTEGER,
    request_id  INTEGER,
    detail      TEXT NOT NULL DEFAULT ''
);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(s: str) -> datetime:
    """解析 ISO 8601 时间戳；无时区信息按 UTC 处理。"""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx():
    """写事务：全局锁 + BEGIN IMMEDIATE，提交或整体回滚（拒绝时状态不变）。"""
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _lock:
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()


def log_op(conn: sqlite3.Connection, actor: str, action: str,
           document_id: int | None = None, request_id: int | None = None,
           detail: str = "") -> None:
    """追加操作记录；必须在事务内调用，随业务变更一起提交。"""
    conn.execute(
        "INSERT INTO operation_log (ts, actor, action, document_id, request_id, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (iso(now_utc()), actor, action, document_id, request_id, detail),
    )
