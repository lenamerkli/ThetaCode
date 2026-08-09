import json
import re
import sqlite3
import time
import uuid
from pathlib import Path


DB_PATH = Path.home() / '.local' / 'share' / 'ThetaCode' / 'thetacode.db'

# Schema version for migrations
SCHEMA_VERSION = 2


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
                    mode           TEXT    NOT NULL DEFAULT 'docker',
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
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id      INTEGER NOT NULL,
                    role         TEXT    NOT NULL,
                    content      TEXT    NOT NULL DEFAULT '',
                    thinking     TEXT    NOT NULL DEFAULT '',
                    cost         REAL    NOT NULL DEFAULT 0.0,
                    llm_model    TEXT    NOT NULL DEFAULT '',
                    created_at   REAL    NOT NULL,
                    tool_calls   TEXT,
                    tool_call_id TEXT,
                    name         TEXT,
                    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
            """)
            # Migrate old DBs that lack original_path
            try:
                conn.execute("SELECT original_path FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE projects ADD COLUMN original_path TEXT")

            # Migrate old DBs that lack mode
            try:
                conn.execute("SELECT mode FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'docker'")

            # Migrate old DBs that lack tool_calls column (official tool calling format)
            try:
                conn.execute("SELECT tool_calls FROM messages LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
                conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
                conn.execute("ALTER TABLE messages ADD COLUMN name TEXT")

            conn.commit()

        self._migrate_projects_to_working_copy()
        self._migrate_messages_to_official_format()

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

    def _migrate_messages_to_official_format(self):
        """Convert legacy XML tool calls in messages to official format.

        This migration:
        1. Finds assistant messages with <tool_call> XML in content
        2. Extracts tool calls to the tool_calls JSON column
        3. Converts user messages with <tool_response> to role='tool'
        """
        with self._connect() as conn:
            # Check if migration is needed by looking for legacy format messages
            legacy_assistant = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE role = 'assistant' AND content LIKE '%<tool_call>%' AND tool_calls IS NULL"
            ).fetchone()
            legacy_tool_response = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE role = 'user' AND content LIKE '%<tool_response>%'"
            ).fetchone()

            if legacy_assistant['cnt'] == 0 and legacy_tool_response['cnt'] == 0:
                return  # No migration needed

            print(f"[Storage] Migrating {legacy_assistant['cnt']} assistant messages and "
                  f"{legacy_tool_response['cnt']} tool responses to official format...")

            # Get all chats to process them in order
            chats = conn.execute("SELECT id FROM chats").fetchall()

            for chat_row in chats:
                chat_id = chat_row['id']
                messages = conn.execute(
                    "SELECT id, role, content FROM messages WHERE chat_id = ? ORDER BY created_at, id",
                    (chat_id,)
                ).fetchall()

                pending_tool_call_ids = []  # Queue of tool call IDs waiting for responses

                for msg in messages:
                    msg_id = msg['id']
                    role = msg['role']
                    content = msg['content']

                    if role == 'assistant' and '<tool_call>' in content:
                        # Parse XML tool calls and convert to official format
                        text_content, tool_calls = self._xml_to_tool_calls(content)
                        if tool_calls:
                            conn.execute(
                                "UPDATE messages SET content = ?, tool_calls = ? WHERE id = ?",
                                (text_content or '', json.dumps(tool_calls), msg_id)
                            )
                            for tc in tool_calls:
                                pending_tool_call_ids.append(tc['id'])

                    elif role == 'user' and content.lstrip().startswith('<tool_response>'):
                        # Convert tool response to official format
                        match = re.search(r'<tool_response>\s*(.*?)\s*</tool_response>', content, re.DOTALL)
                        if match:
                            tool_content = match.group(1)
                            tool_call_id = pending_tool_call_ids.pop(0) if pending_tool_call_ids else f'call_{uuid.uuid4().hex[:24]}'
                            conn.execute(
                                "UPDATE messages SET role = 'tool', content = ?, tool_call_id = ? WHERE id = ?",
                                (tool_content, tool_call_id, msg_id)
                            )

            conn.commit()
            print("[Storage] Migration to official tool calling format complete.")

    @staticmethod
    def _xml_to_tool_calls(content: str) -> tuple[str, list[dict]]:
        """Parse legacy XML tool calls from content and convert to official format.

        Returns:
            Tuple of (text_content_without_tool_calls, list_of_tool_calls).
        """
        tool_calls = []
        pattern = r'<tool_call>(.*?)</tool_call>'
        matches = list(re.finditer(pattern, content, re.DOTALL))

        if not matches:
            return content, []

        # Extract text before first tool call
        text_before = content[:matches[0].start()].strip()

        for match in matches:
            block = match.group(1)

            # Extract tool name
            name_match = re.search(r'<tool_name>(.*?)</tool_name>', block, re.DOTALL)
            if not name_match:
                continue
            tool_name = name_match.group(1).strip()

            # Extract parameters (any tag that's not tool_name)
            args = {}
            param_pattern = r'<(\w+)>(.*?)</\1>'
            for param_match in re.finditer(param_pattern, block, re.DOTALL):
                param_name = param_match.group(1)
                if param_name != 'tool_name':
                    args[param_name] = param_match.group(2).strip()

            tool_calls.append({
                'id': f'call_{uuid.uuid4().hex[:24]}',
                'type': 'function',
                'function': {
                    'name': tool_name,
                    'arguments': json.dumps(args)
                }
            })

        return text_before, tool_calls

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

    def create_project(self, name: str, path: str, mode: str = 'docker') -> int:
        """Insert a new project and return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, path, mode, created_at) VALUES (?, ?, ?, ?)",
                (name, path, mode, time.time()),
            )
            conn.commit()
            return cur.lastrowid

    def get_projects(self, mode_filter: str | None = None) -> list[dict]:
        """Return all projects ordered by creation time, optionally filtered by mode."""
        with self._connect() as conn:
            if mode_filter:
                rows = conn.execute(
                    "SELECT id, name, path, original_path, mode, created_at FROM projects WHERE mode = ? ORDER BY created_at",
                    (mode_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, path, original_path, mode, created_at FROM projects ORDER BY created_at"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, path, original_path, mode, created_at FROM projects WHERE id = ?",
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
        tool_calls: list[dict] | None = None,
        tool_call_id: str = '',
        name: str = '',
    ) -> int:
        """Append one message to a chat and return its id.
        
        Args:
            chat_id: The chat to append to.
            role: Message role ('system', 'user', 'assistant', 'tool').
            content: Text content of the message.
            thinking: Reasoning/thinking content.
            cost: Cost of the LLM call.
            llm_model: Model used.
            tool_calls: List of tool calls (for assistant messages with official tool calling).
            tool_call_id: ID linking this tool result to its call (for role='tool').
            name: Tool name (for role='tool').
        """
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (chat_id, role, content, thinking, cost, llm_model, created_at, tool_calls, tool_call_id, name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, role, content, thinking, cost, llm_model, time.time(),
                 tool_calls_json, tool_call_id or None, name or None),
            )
            conn.commit()
            return cur.lastrowid

    def get_messages(self, chat_id: int) -> list[dict]:
        """Return all messages for a chat in order.
        
        Messages are returned in official tool calling format:
        - Assistant messages may have 'tool_calls' (list of dicts)
        - Tool results have role='tool' with 'tool_call_id' and 'name'
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, chat_id, role, content, thinking, cost, llm_model, created_at, tool_calls, tool_call_id, name "
                "FROM messages WHERE chat_id = ? ORDER BY created_at, id",
                (chat_id,),
            ).fetchall()
        
        result = []
        for r in rows:
            msg = dict(r)
            # Parse tool_calls JSON if present
            if msg.get('tool_calls'):
                try:
                    msg['tool_calls'] = json.loads(msg['tool_calls'])
                except json.JSONDecodeError:
                    msg['tool_calls'] = None
            result.append(msg)
        return result

    def delete_message(self, message_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.commit()
