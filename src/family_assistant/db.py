import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class StoreError(Exception):
    pass


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=3000")

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                is_owner INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS passes (
                token TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                user_id INTEGER PRIMARY KEY,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                new_body TEXT,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def upsert_user(self, telegram_id: int, phone: str, name: str, is_owner: bool, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO users(telegram_id, phone, name, is_owner, created_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                phone = excluded.phone,
                name = excluded.name,
                is_owner = excluded.is_owner
            """,
            (telegram_id, phone, name, 1 if is_owner else 0, iso(now)),
        )
        self.conn.commit()

    def user_by_telegram(self, telegram_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()

    def user_by_phone(self, phone: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

    def owner(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE is_owner = 1 ORDER BY created_at LIMIT 1").fetchone()

    def users(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM users ORDER BY name"))

    def add_message(self, chat_id: int, user_id: int | None, role: str, text: str, now: datetime) -> int:
        cursor = self.conn.execute(
            "INSERT INTO messages(chat_id, user_id, role, text, created_at) VALUES(?, ?, ?, ?, ?)",
            (chat_id, user_id, role, text, iso(now)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def history(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT role, text FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def issue_pass(self, chat_id: int, user_id: int, ttl_seconds: int, now: datetime) -> str:
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO passes(token, chat_id, user_id, expires_at) VALUES(?, ?, ?, ?)",
            (token, chat_id, user_id, iso(now + timedelta(seconds=ttl_seconds))),
        )
        self.conn.commit()
        return token

    def valid_pass(self, token: str, now: datetime) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM passes WHERE token = ?", (token,)).fetchone()
        if row is None or parse_iso(str(row["expires_at"])) <= now:
            return None
        return row

    def memory(self, user_id: int) -> str:
        row = self.conn.execute("SELECT body FROM memories WHERE user_id = ?", (user_id,)).fetchone()
        return "" if row is None else str(row["body"])

    def append_memory(self, user_id: int, line: str, now: datetime, limit: int = 8000) -> str:
        clean = " ".join(line.split())
        if not clean:
            raise StoreError("пустая строка")
        if len(clean) > 500:
            raise StoreError("строка длиннее 500 символов")
        current = self.memory(user_id)
        addition = f"- {now.astimezone(timezone.utc).strftime('%Y-%m-%d')}: {clean}"
        body = addition if not current else f"{current.rstrip()}\n{addition}"
        if len(body) > limit:
            raise StoreError("заметка переполнена, нужна замена через подтверждение")
        self._write_memory(user_id, body, "append", now, snapshot=bool(current))
        return body

    def replace_memory(self, user_id: int, body: str, reason: str, now: datetime, limit: int = 8000) -> None:
        if len(body) > limit:
            raise StoreError("новая заметка длиннее 8000 символов")
        self._write_memory(user_id, body.strip(), reason, now, snapshot=True)

    def _write_memory(self, user_id: int, body: str, reason: str, now: datetime, snapshot: bool) -> None:
        current = self.memory(user_id)
        with self.conn:
            if snapshot and current:
                self.conn.execute(
                    "INSERT INTO memory_versions(user_id, body, reason, created_at) VALUES(?, ?, ?, ?)",
                    (user_id, current, reason, iso(now)),
                )
            self.conn.execute(
                """
                INSERT INTO memories(user_id, body, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at
                """,
                (user_id, body, iso(now)),
            )

    def versions(self, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT body, reason, created_at FROM memory_versions WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
        )

    def pending_count(self, chat_id: int, now: datetime) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM approvals
            WHERE chat_id = ? AND status = 'pending' AND expires_at > ?
            """,
            (chat_id, iso(now)),
        ).fetchone()
        return int(row["n"])

    def create_approval(
        self,
        chat_id: int,
        user_id: int,
        target_user_id: int,
        kind: str,
        summary: str,
        new_body: str | None,
        ttl_seconds: int,
        now: datetime,
    ) -> str:
        approval_id = secrets.token_hex(16)
        self.conn.execute(
            """
            INSERT INTO approvals(
                id, chat_id, user_id, target_user_id, kind, summary, new_body, status, expires_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                approval_id,
                chat_id,
                user_id,
                target_user_id,
                kind,
                summary,
                new_body,
                iso(now + timedelta(seconds=ttl_seconds)),
                iso(now),
            ),
        )
        self.conn.commit()
        return approval_id

    def approval(self, approval_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()

    def mark_approval(self, approval_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE approvals SET status = ? WHERE id = ? AND status = 'pending'",
            (status, approval_id),
        )
        self.conn.commit()

    def decide(self, approval_id: str, decision: str, now: datetime) -> sqlite3.Row | None:
        """Mark a pending live approval. Returns the row, or None if it was not pending."""
        if decision not in {"approved", "denied"}:
            raise StoreError("неизвестное решение")
        stamp = iso(now)
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE approvals
                SET status = ?
                WHERE id = ? AND status = 'pending' AND expires_at > ?
                """,
                (decision, approval_id, stamp),
            )
            if cursor.rowcount != 1:
                expired = self.conn.execute(
                    """
                    UPDATE approvals SET status = 'expired'
                    WHERE id = ? AND status = 'pending' AND expires_at <= ?
                    """,
                    (approval_id, stamp),
                )
                if expired.rowcount:
                    return None
                return None
        return self.approval(approval_id)


for _store_method in (
    "close",
    "migrate",
    "get_meta",
    "set_meta",
    "upsert_user",
    "user_by_telegram",
    "user_by_phone",
    "owner",
    "users",
    "add_message",
    "history",
    "issue_pass",
    "valid_pass",
    "memory",
    "append_memory",
    "replace_memory",
    "versions",
    "pending_count",
    "create_approval",
    "approval",
    "mark_approval",
    "decide",
):
    setattr(Store, _store_method, _locked(getattr(Store, _store_method)))
