"""Authentication database backend for AI Voice Bot 4 U web auth."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4


class SqliteAuthStore:
    """Simple SQLite-backed user store."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_users_email
                ON users(email)
                """
            )
            user_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "is_admin" not in user_columns:
                conn.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0
                    """
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guest_demo_leads (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    workspace_client_id TEXT NOT NULL,
                    project_id TEXT,
                    project_name TEXT,
                    full_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    email TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_session_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_demo_leads_created_at
                ON guest_demo_leads(created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_demo_leads_user_id
                ON guest_demo_leads(user_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_demo_leads_phone
                ON guest_demo_leads(phone)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_demo_leads_email
                ON guest_demo_leads(email)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_demo_leads_project_id
                ON guest_demo_leads(project_id)
                """
            )
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(guest_demo_leads)").fetchall()
            }
            if "notification_status" not in existing_columns:
                conn.execute(
                    """
                    ALTER TABLE guest_demo_leads
                    ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'pending'
                    """
                )
            if "notification_sent_at" not in existing_columns:
                conn.execute(
                    """
                    ALTER TABLE guest_demo_leads
                    ADD COLUMN notification_sent_at TEXT
                    """
                )
            if "notification_error" not in existing_columns:
                conn.execute(
                    """
                    ALTER TABLE guest_demo_leads
                    ADD COLUMN notification_error TEXT
                    """
                )
            if "testing_summary" not in existing_columns:
                conn.execute(
                    """
                    ALTER TABLE guest_demo_leads
                    ADD COLUMN testing_summary TEXT
                    """
                )
            if "asked_questions_json" not in existing_columns:
                conn.execute(
                    """
                    ALTER TABLE guest_demo_leads
                    ADD COLUMN asked_questions_json TEXT NOT NULL DEFAULT '[]'
                    """
                )
            if "summary_updated_at" not in existing_columns:
                conn.execute(
                    """
                    ALTER TABLE guest_demo_leads
                    ADD COLUMN summary_updated_at TEXT
                    """
                )
            conn.commit()

    @staticmethod
    def _row_to_user(row: sqlite3.Row | None) -> dict[str, str] | None:
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "email": str(row["email"]),
            "password_hash": str(row["password_hash"]),
            "password_salt": str(row["password_salt"]),
            "created_at": str(row["created_at"]),
            "is_admin": "1" if bool(row["is_admin"]) else "0",
        }

    def create_user(
        self,
        *,
        user_id: str,
        name: str,
        email: str,
        password_hash: str,
        password_salt: str,
        created_at: str,
        is_admin: bool = False,
    ) -> dict[str, str]:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users(id, name, email, password_hash, password_salt, created_at, is_admin)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, name, email, password_hash, password_salt, created_at, 1 if is_admin else 0),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("An account with this email already exists.") from exc
        return {
            "id": user_id,
            "name": name,
            "email": email,
            "password_hash": password_hash,
            "password_salt": password_salt,
            "created_at": created_at,
            "is_admin": "1" if is_admin else "0",
        }

    def get_user_by_email(self, email: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, email, password_hash, password_salt, created_at, is_admin
                FROM users
                WHERE email = ?
                LIMIT 1
                """,
                (email,),
            ).fetchone()
        return self._row_to_user(row)

    def get_user_by_id(self, user_id: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, email, password_hash, password_salt, created_at, is_admin
                FROM users
                WHERE id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_user(row)

    def migrate_from_legacy_json(self, users_path: Path) -> int:
        """Best-effort import from prior file-based auth store."""
        if not users_path.exists():
            return 0
        try:
            raw = json.loads(users_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        users = raw.get("users", []) if isinstance(raw, dict) else []
        if not isinstance(users, list):
            return 0

        imported = 0
        with self._connect() as conn:
            for row in users:
                if not isinstance(row, dict):
                    continue
                user_id = str(row.get("id") or "").strip()
                email = str(row.get("email") or "").strip().lower()
                name = str(row.get("name") or "").strip() or "User"
                password_hash = str(row.get("password_hash") or "").strip()
                password_salt = str(row.get("password_salt") or "").strip()
                created_at = str(row.get("created_at") or "").strip()
                is_admin = bool(row.get("is_admin") or False)
                if not user_id or not email or not password_hash or not password_salt or not created_at:
                    continue
                try:
                    conn.execute(
                        """
                        INSERT INTO users(id, name, email, password_hash, password_salt, created_at, is_admin)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (user_id, name, email, password_hash, password_salt, created_at, 1 if is_admin else 0),
                    )
                    imported += 1
                except sqlite3.IntegrityError:
                    continue
            conn.commit()
        return imported

    def list_guest_demo_leads(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        where_clauses: list[str] = []
        params: list[Any] = []

        normalized_search = str(search or "").strip()
        if normalized_search:
            where_clauses.append(
                """
                (
                    lower(full_name) LIKE ?
                    OR lower(email) LIKE ?
                    OR lower(phone) LIKE ?
                    OR lower(COALESCE(project_name, '')) LIKE ?
                    OR lower(COALESCE(testing_summary, '')) LIKE ?
                )
                """
            )
            search_like = f"%{normalized_search.lower()}%"
            params.extend([search_like, search_like, search_like, search_like, search_like])

        normalized_project_id = str(project_id or "").strip()
        if normalized_project_id:
            where_clauses.append("project_id = ?")
            params.append(normalized_project_id)

        normalized_status = str(status or "").strip()
        if normalized_status:
            where_clauses.append("status = ?")
            params.append(normalized_status)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query_params = [*params, safe_limit, safe_offset]

        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM guest_demo_leads {where_sql}",
                    params,
                ).fetchone()["count"]
            )
            rows = conn.execute(
                f"""
                SELECT
                    id,
                    user_id,
                    workspace_client_id,
                    project_id,
                    project_name,
                    full_name,
                    phone,
                    email,
                    source,
                    status,
                    created_at,
                    updated_at,
                    last_session_id,
                    metadata_json,
                    notification_status,
                    notification_sent_at,
                    notification_error,
                    testing_summary,
                    asked_questions_json,
                    summary_updated_at
                FROM guest_demo_leads
                {where_sql}
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                query_params,
            ).fetchall()
            project_rows = conn.execute(
                """
                SELECT project_id, COALESCE(project_name, project_id, 'Unknown') AS project_name, COUNT(*) AS count
                FROM guest_demo_leads
                GROUP BY project_id, COALESCE(project_name, project_id, 'Unknown')
                ORDER BY count DESC, project_name ASC
                """
            ).fetchall()

        items = [self._row_to_guest_demo_lead(row) for row in rows]
        projects = [
            {
                "project_id": str(row["project_id"] or ""),
                "project_name": str(row["project_name"] or "Unknown"),
                "count": int(row["count"] or 0),
            }
            for row in project_rows
        ]
        return {"items": items, "total": total, "limit": safe_limit, "offset": safe_offset, "projects": projects}

    def _row_to_guest_demo_lead(self, row: sqlite3.Row) -> dict[str, Any]:
        metadata_raw = row["metadata_json"]
        asked_questions_raw = row["asked_questions_json"]
        try:
            metadata = json.loads(str(metadata_raw or "{}"))
        except Exception:
            metadata = {}
        try:
            asked_questions = json.loads(str(asked_questions_raw or "[]"))
        except Exception:
            asked_questions = []
        if not isinstance(metadata, dict):
            metadata = {}
        if not isinstance(asked_questions, list):
            asked_questions = []
        return {
            "id": str(row["id"]),
            "user_id": str(row["user_id"] or ""),
            "workspace_client_id": str(row["workspace_client_id"] or ""),
            "project_id": str(row["project_id"] or ""),
            "project_name": str(row["project_name"] or ""),
            "full_name": str(row["full_name"] or ""),
            "phone": str(row["phone"] or ""),
            "email": str(row["email"] or ""),
            "source": str(row["source"] or ""),
            "status": str(row["status"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "last_session_id": str(row["last_session_id"] or ""),
            "metadata": metadata,
            "notification_status": str(row["notification_status"] or ""),
            "notification_sent_at": str(row["notification_sent_at"] or ""),
            "notification_error": str(row["notification_error"] or ""),
            "testing_summary": str(row["testing_summary"] or ""),
            "asked_questions": [str(item).strip() for item in asked_questions if str(item).strip()],
            "summary_updated_at": str(row["summary_updated_at"] or ""),
        }

    def create_guest_demo_lead(
        self,
        *,
        user_id: str | None,
        workspace_client_id: str,
        project_id: str | None,
        project_name: str | None,
        full_name: str,
        phone: str,
        email: str,
        source: str,
        status: str,
        created_at: str,
        updated_at: str,
        last_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lead_id = str(uuid4())
        payload = {
            "id": lead_id,
            "user_id": user_id,
            "workspace_client_id": workspace_client_id,
            "project_id": project_id,
            "project_name": project_name,
            "full_name": full_name,
            "phone": phone,
            "email": email,
            "source": source,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "last_session_id": last_session_id,
            "metadata": metadata or {},
            "notification_status": "pending",
            "notification_sent_at": None,
            "notification_error": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO guest_demo_leads(
                    id,
                    user_id,
                    workspace_client_id,
                    project_id,
                    project_name,
                    full_name,
                    phone,
                    email,
                    source,
                    status,
                    created_at,
                    updated_at,
                    last_session_id,
                    metadata_json,
                    notification_status,
                    notification_sent_at,
                    notification_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["user_id"],
                    payload["workspace_client_id"],
                    payload["project_id"],
                    payload["project_name"],
                    payload["full_name"],
                    payload["phone"],
                    payload["email"],
                    payload["source"],
                    payload["status"],
                    payload["created_at"],
                    payload["updated_at"],
                    payload["last_session_id"],
                    json.dumps(payload["metadata"], ensure_ascii=False),
                    payload["notification_status"],
                    payload["notification_sent_at"],
                    payload["notification_error"],
                ),
            )
            conn.commit()
        return payload

    def update_guest_demo_lead_notification(
        self,
        *,
        lead_id: str,
        notification_status: str,
        notification_sent_at: str | None = None,
        notification_error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE guest_demo_leads
                SET notification_status = ?,
                    notification_sent_at = ?,
                    notification_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    notification_status,
                    notification_sent_at,
                    notification_error,
                    lead_id,
                ),
            )
            conn.commit()

    def update_guest_demo_lead_summary(
        self,
        *,
        lead_id: str,
        last_session_id: str | None,
        testing_summary: str | None,
        asked_questions: list[str],
        summary_updated_at: str,
        status: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE guest_demo_leads
                SET last_session_id = ?,
                    testing_summary = ?,
                    asked_questions_json = ?,
                    summary_updated_at = ?,
                    status = COALESCE(?, status),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    last_session_id,
                    testing_summary,
                    json.dumps(asked_questions, ensure_ascii=False),
                    summary_updated_at,
                    status,
                    lead_id,
                ),
            )
            conn.commit()
