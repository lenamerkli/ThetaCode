import sqlite3
import time
from pathlib import Path


DB_PATH = Path.home() / '.local' / 'share' / 'ThetaCode' / 'thetacode.db'


class Storage:
    """SQLite-backed persistence for ThetaCode projects, chats, and messages."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    name           TEXT    NOT NULL UNIQUE,
                    path           TEXT    NOT NULL,
                    original_path  TEXT,
                    created_at     REAL    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chats (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    name       TEXT    NOT NULL,
                    created_at REAL    NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id    INTEGER NOT NULL,
                    role       TEXT    NOT NULL,
                    content    TEXT    NOT NULL DEFAULT '',
                    thinking   TEXT    NOT NULL DEFAULT '',
                    cost       REAL    NOT NULL DEFAULT 0.0,
                    llm_model  TEXT    NOT NULL DEFAULT '',
                    created_at REAL    NOT NULL,
                    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
                );
            """)
            # Migrate old DBs that lack original_path
            try:
                conn.execute("SELECT original_path FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE projects ADD COLUMN original_path TEXT")
            conn.commit()

        self._migrate_projects_to_working_copy()

    def _migrate_projects_to_working_copy(self):
        """For projects created before the working-copy feature, set original_path = path."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, path, original_path FROM projects WHERE original_path IS NULL"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE projects SET original_path = ? WHERE id = ?",
                    (row["path"], row["id"]),
                )
            conn.commit()

    def get_project_original_path(self, project_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT original_path FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return row["original_path"] if row else None

    def update_project_paths(self, project_id: int, working_path: str, original_path: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET path = ?, original_path = ? WHERE id = ?",
                (working_path, original_path, project_id),
            )
            conn.commit()



    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, name: str, path: str) -> int:
        """Insert a new project and return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, path, created_at) VALUES (?, ?, ?)",
                (name, path, time.time()),
            )
            conn.commit()
            return cur.lastrowid

    def get_projects(self) -> list[dict]:
        """Return all projects ordered by creation time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, path, original_path, created_at FROM projects ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, path, original_path, created_at FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_project(self, project_id: int):
        """Delete a project (cascades to chats and messages)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()

    def update_project_path(self, project_id: int, new_path: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET path = ? WHERE id = ?",
                (new_path, project_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------

    def create_chat(self, project_id: int, name: str) -> int:
        """Insert a new chat and return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO chats (project_id, name, created_at) VALUES (?, ?, ?)",
                (project_id, name, time.time()),
            )
            conn.commit()
            return cur.lastrowid

    def get_chats(self, project_id: int) -> list[dict]:
        """Return all chats for a project ordered by creation time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, project_id, name, created_at FROM chats "
                "WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_chat(self, chat_id: int):
        """Delete a chat (cascades to messages)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            conn.commit()

    def rename_chat(self, chat_id: int, new_name: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE chats SET name = ? WHERE id = ?",
                (new_name, chat_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def append_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        thinking: str = '',
        cost: float = 0.0,
        llm_model: str = '',
    ) -> int:
        """Append one message to a chat and return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (chat_id, role, content, thinking, cost, llm_model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, role, content, thinking, cost, llm_model, time.time()),
            )
            conn.commit()
            return cur.lastrowid

    def get_messages(self, chat_id: int) -> list[dict]:
        """Return all messages for a chat in order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, chat_id, role, content, thinking, cost, llm_model, created_at "
                "FROM messages WHERE chat_id = ? ORDER BY created_at, id",
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_message(self, message_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.commit()
