import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional
import json


DEFAULT_DB_DIR = Path.home() / '.local/share/ThetaCode'
DEFAULT_DB_PATH = DEFAULT_DB_DIR / 'thetacode.db'


def _ensure_dir():
    DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)


def _get_conn(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH):
    conn = _get_conn(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_opened TEXT
            );
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thinking TEXT,
                cost REAL DEFAULT 0,
                llm_model TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
        """)
        conn.commit()
    finally:
        conn.close()


class Database:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        init_db(db_path)

    def create_project(self, name: str, path: str) -> int:
        conn = _get_conn(self.db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO projects (name, path, created_at, last_opened) VALUES (?, ?, ?, ?)",
                (name, path, datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def list_projects(self) -> list[dict]:
        conn = _get_conn(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM projects ORDER BY last_opened DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_project(self, project_id: int) -> Optional[dict]:
        conn = _get_conn(self.db_path)
        try:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_project_last_opened(self, project_id: int):
        conn = _get_conn(self.db_path)
        try:
            conn.execute(
                "UPDATE projects SET last_opened = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), project_id)
            )
            conn.commit()
        finally:
            conn.close()

    def delete_project(self, project_id: int):
        conn = _get_conn(self.db_path)
        try:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()
        finally:
            conn.close()

    def create_chat(self, project_id: int, title: str = "New Chat") -> int:
        conn = _get_conn(self.db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO chats (project_id, title, created_at) VALUES (?, ?, ?)",
                (project_id, title, datetime.utcnow().isoformat())
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_chats(self, project_id: int) -> list[dict]:
        conn = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM chats WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_chat(self, chat_id: int) -> Optional[dict]:
        conn = _get_conn(self.db_path)
        try:
            row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def rename_chat(self, chat_id: int, title: str):
        conn = _get_conn(self.db_path)
        try:
            conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
            conn.commit()
        finally:
            conn.close()

    def delete_chat(self, chat_id: int):
        conn = _get_conn(self.db_path)
        try:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            conn.commit()
        finally:
            conn.close()

    def save_message(self, chat_id: int, role: str, content: str, thinking: str = "", cost: float = 0.0, llm_model: str = "") -> int:
        conn = _get_conn(self.db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO messages (chat_id, role, content, thinking, cost, llm_model, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, role, content, thinking, cost, llm_model, datetime.utcnow().isoformat())
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_messages(self, chat_id: int) -> list[dict]:
        conn = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
                (chat_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_total_cost(self, chat_id: int) -> float:
        conn = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT SUM(cost) as total FROM messages WHERE chat_id = ?",
                (chat_id,)
            ).fetchone()
            return row['total'] or 0.0
        finally:
            conn.close()

    def get_setting(self, key: str) -> Optional[str]:
        conn = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,)
            ).fetchone()
            return row['value'] if row else None
        finally:
            conn.close()

    def set_setting(self, key: str, value: str):
        conn = _get_conn(self.db_path)
        try:
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value)
            )
            conn.commit()
        finally:
            conn.close()

    def init_settings_table(self):
        """Ensure settings table exists."""
        conn = _get_conn(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()
