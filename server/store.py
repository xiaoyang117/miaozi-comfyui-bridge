"""按访问 IP 保存会话、消息与创作偏好（SQLite）。"""

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
    owner_ip   TEXT NOT NULL DEFAULT '127.0.0.1',
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
CREATE TABLE IF NOT EXISTS client_preferences (
    owner_ip          TEXT PRIMARY KEY,
    nsfw_enabled      INTEGER NOT NULL DEFAULT 0,
    workflow_controls TEXT NOT NULL DEFAULT '{}'
);
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
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "owner_ip" not in columns:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN owner_ip TEXT "
                    "NOT NULL DEFAULT '127.0.0.1'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_owner "
                "ON sessions(owner_ip, updated_at DESC)")
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        # waitress 是多线程的，关掉同线程校验，靠 RLock 串行化写
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def get_preferences(self, owner_ip: str = "127.0.0.1") -> dict:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT nsfw_enabled, workflow_controls FROM client_preferences "
                "WHERE owner_ip = ?", (owner_ip,)).fetchone()
        if row is None:
            return {"nsfw_enabled": False, "workflow_controls": {}}
        try:
            controls = json.loads(row["workflow_controls"])
        except (ValueError, TypeError):
            controls = {}
        return {"nsfw_enabled": bool(row["nsfw_enabled"]),
                "workflow_controls": controls if isinstance(controls, dict) else {}}

    def update_preferences(self, owner_ip: str, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError("创作偏好格式不正确")
        if "nsfw_enabled" in data and type(data["nsfw_enabled"]) is not bool:
            raise ValueError("NSFW 开关必须为布尔值")
        if "workflow_controls" in data:
            controls = data["workflow_controls"]
            if (not isinstance(controls, dict)
                    or any(not isinstance(value, dict) for value in controls.values())):
                raise ValueError("工作流参数格式不正确")
        with self._lock, self._conn() as conn:
            preferences = self.get_preferences(owner_ip)
            for key in preferences:
                if key in data:
                    preferences[key] = data[key]
            conn.execute(
                "INSERT INTO client_preferences(owner_ip, nsfw_enabled, workflow_controls) "
                "VALUES (?,?,?) ON CONFLICT(owner_ip) DO UPDATE SET "
                "nsfw_enabled=excluded.nsfw_enabled, workflow_controls=excluded.workflow_controls",
                (owner_ip, int(preferences["nsfw_enabled"]),
                 json.dumps(preferences["workflow_controls"], ensure_ascii=False)))
            conn.commit()
        return preferences

    # ---------- 会话 ----------

    def list_sessions(self, limit: int = 50, owner_ip: str = "127.0.0.1") -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n "
                "FROM sessions s WHERE s.owner_ip = ? "
                "ORDER BY s.updated_at DESC LIMIT ?",
                (owner_ip, limit)).fetchall()
        return [dict(r) for r in rows]

    def create_session(self, title: str = "", owner_ip: str = "127.0.0.1") -> dict:
        sid = uuid.uuid4().hex[:16]
        ts = _now()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions(id, title, owner_ip, created_at, updated_at) "
                "VALUES (?,?,?,?,?)", (sid, title or "新对话", owner_ip, ts, ts))
            conn.commit()
        return {"id": sid, "title": title or "新对话",
                "created_at": ts, "updated_at": ts, "n": 0}

    def session_exists(self, session_id: str, owner_ip: str = "127.0.0.1") -> bool:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT 1 FROM sessions WHERE id = ? AND owner_ip = ?",
                               (session_id, owner_ip)).fetchone()
        return row is not None

    def ensure_session(self, session_id: str, owner_ip: str = "127.0.0.1") -> str:
        if session_id:
            if not self.session_exists(session_id, owner_ip):
                raise ValueError("会话不存在")
            return session_id
        return self.create_session(owner_ip=owner_ip)["id"]

    def delete_session(self, session_id: str, owner_ip: str = "127.0.0.1") -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND session_id IN "
                "(SELECT id FROM sessions WHERE owner_ip = ?)", (session_id, owner_ip))
            conn.execute("DELETE FROM sessions WHERE id = ? AND owner_ip = ?",
                         (session_id, owner_ip))
            conn.commit()

    def rename_session(self, session_id: str, title: str,
                       owner_ip: str = "127.0.0.1") -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ? AND owner_ip = ?",
                         (title[:80], session_id, owner_ip))
            conn.commit()

    # ---------- 消息 ----------

    def add_message(self, session_id: str, role: str, content: str = "",
                    image: str = "", character: str = "",
                    vlm: str = "", meta: dict = None,
                    owner_ip: str = "127.0.0.1") -> int:
        ts = _now()
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock, self._conn() as conn:
            if conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? AND owner_ip = ?",
                    (session_id, owner_ip)).fetchone() is None:
                raise ValueError("会话不存在")
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

    def list_messages(self, session_id: str, limit: int = 500,
                      owner_ip: str = "127.0.0.1") -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT m.* FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.session_id = ? AND s.owner_ip = ? "
                "ORDER BY m.id DESC LIMIT ?", (session_id, owner_ip, limit)).fetchall()
        out = []
        for r in reversed(rows):
            item = dict(r)
            try:
                meta = json.loads(item.get("meta") or "{}")
            except Exception:
                meta = {}
            item["meta"] = meta if isinstance(meta, dict) else {}
            out.append(item)
        return out

    def get_message(self, session_id: str, message_id: int,
                    owner_ip: str = "127.0.0.1") -> dict | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT m.* FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.session_id = ? AND m.id = ? AND s.owner_ip = ?",
                (session_id, message_id, owner_ip)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            meta = json.loads(item["meta"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        item["meta"] = meta if isinstance(meta, dict) else {}
        return item

    def delete_message(self, message_id: int, owner_ip: str = "127.0.0.1") -> bool:
        with self._lock, self._conn() as conn:
            deleted = conn.execute(
                "DELETE FROM messages WHERE id = ? AND session_id IN "
                "(SELECT id FROM sessions WHERE owner_ip = ?)", (message_id, owner_ip))
            conn.commit()
        return deleted.rowcount > 0

    def clear_messages(self, session_id: str, owner_ip: str = "127.0.0.1") -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND session_id IN "
                "(SELECT id FROM sessions WHERE owner_ip = ?)", (session_id, owner_ip))
            conn.commit()

    def context_pairs(self, session_id: str, rounds: int,
                      owner_ip: str = "127.0.0.1", include_nsfw: bool = True) -> list:
        """取最近 N 轮的 (用户输入, 助手提示词) 用于拼上下文。"""
        if rounds <= 0:
            return []
        msgs = self.list_messages(session_id, owner_ip=owner_ip)
        pairs = []
        pending_user = None
        for m in msgs:
            if m["role"] == "user":
                pending_user = m["content"]
            elif m["role"] == "assistant" and pending_user is not None:
                if include_nsfw or m["meta"].get("nsfw_enabled") is not True:
                    pairs.append((pending_user, m["content"]))
                pending_user = None
        return pairs[-rounds:]

    def last_assistant_character_ids(self, session_id: str,
                                     owner_ip: str = "127.0.0.1") -> list[str]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT m.meta, m.character FROM messages m "
                "JOIN sessions s ON s.id = m.session_id WHERE m.session_id = ? "
                "AND m.role = 'assistant' AND s.owner_ip = ? ORDER BY m.id DESC LIMIT 1",
                (session_id, owner_ip)).fetchone()
        if not row:
            return []
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if isinstance(meta, dict):
            ids = meta.get("character_ids")
            if isinstance(ids, list):
                return [value for value in ids if isinstance(value, str) and value]
            single = meta.get("character_id")
            if isinstance(single, str):
                return [single] if single else []
        first_line = (row["character"] or "").split("\n", 1)[0]
        if first_line.startswith("角色: "):
            return [first_line.removeprefix("角色: ").strip()]
        return []

    def last_assistant_character_id(self, session_id: str,
                                    owner_ip: str = "127.0.0.1") -> str:
        ids = self.last_assistant_character_ids(session_id, owner_ip)
        return ids[0] if ids else ""


store = Store()
