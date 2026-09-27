"""会话与消息持久化（SQLite）。

比原来存在浏览器 localStorage 的做法好在：换设备、换浏览器、刷新页面
都能看到同一份历史，而且服务端生成提示词时可以直接读到上下文。
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MIAOZI_DATA_DIR") or (BASE_DIR / "data"))
DB_FILE = DATA_DIR / "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    image      TEXT NOT NULL DEFAULT '',
    character  TEXT NOT NULL DEFAULT '',
    vlm        TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_session_updated ON sessions(updated_at DESC);
"""


def _now() -> float:
    return time.time()


class Store:
    def __init__(self, db_path: Path = DB_FILE) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        # waitress 是多线程的，关掉同线程校验，靠 RLock 串行化写
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    # ---------- 会话 ----------

    def list_sessions(self, limit: int = 50) -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n "
                "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def create_session(self, title: str = "") -> dict:
        sid = uuid.uuid4().hex[:16]
        ts = _now()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) "
                "VALUES (?,?,?,?)", (sid, title or "新对话", ts, ts))
            conn.commit()
        return {"id": sid, "title": title or "新对话",
                "created_at": ts, "updated_at": ts, "n": 0}

    def session_exists(self, session_id: str) -> bool:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT 1 FROM sessions WHERE id = ?",
                               (session_id,)).fetchone()
        return row is not None

    def ensure_session(self, session_id: str) -> str:
        if session_id and self.session_exists(session_id):
            return session_id
        return self.create_session()["id"]

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()

    def rename_session(self, session_id: str, title: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?",
                         (title[:80], session_id))
            conn.commit()

    # ---------- 消息 ----------

    def add_message(self, session_id: str, role: str, content: str = "",
                    image: str = "", character: str = "",
                    vlm: str = "", meta: dict = None) -> int:
        ts = _now()
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, image, "
                "character, vlm, meta, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, role, content, image, character, vlm,
                 meta_json, ts))
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                         (ts, session_id))
            # 首条用户消息顺便当会话标题
            if role == "user" and content:
                row = conn.execute(
                    "SELECT title FROM sessions WHERE id = ?",
                    (session_id,)).fetchone()
                if row is not None and (not row["title"]
                                        or row["title"] == "新对话"):
                    conn.execute("UPDATE sessions SET title = ? WHERE id = ?",
                                 (content[:40].replace("\n", " "), session_id))
            conn.commit()
        return cur.lastrowid

    def list_messages(self, session_id: str, limit: int = 500) -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? "
                "ORDER BY id ASC LIMIT ?", (session_id, limit)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["meta"] = json.loads(item.get("meta") or "{}")
            except Exception:
                item["meta"] = {}
            out.append(item)
        return out

    def delete_message(self, message_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.commit()

    def clear_messages(self, session_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    def context_pairs(self, session_id: str, rounds: int) -> list:
        """取最近 N 轮的 (用户输入, 助手提示词) 用于拼上下文。"""
        if rounds <= 0:
            return []
        msgs = self.list_messages(session_id)
        pairs = []
        pending_user = None
        for m in msgs:
            if m["role"] == "user":
                pending_user = m["content"]
            elif m["role"] == "assistant" and pending_user is not None:
                pairs.append((pending_user, m["content"]))
                pending_user = None
        return pairs[-rounds:]


store = Store()
