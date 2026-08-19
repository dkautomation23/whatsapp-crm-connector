"""SQLite state: de-duplication, CRM records, conversation log, failures.

Why SQLite and not memory: Meta retries a webhook when it does not get a fast
2xx, and it retries after your process restarts too. De-duplication that lives
in RAM loses on exactly the delivery it was built for.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Company, Contact, ConversationEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    company_id TEXT REFERENCES companies(company_id),
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    entry_id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    company_id TEXT,
    direction TEXT NOT NULL,
    body TEXT NOT NULL,
    message_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'whatsapp'
);
CREATE INDEX IF NOT EXISTS idx_conversation_contact ON conversation(contact_id);
CREATE INDEX IF NOT EXISTS idx_conversation_company ON conversation(company_id);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    happened_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    reference TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_phone(raw: str) -> str:
    """Digits only, no '+', no separators - the form Meta uses as wa_id.

    Matching '+49 170 555 24 18' from the CRM against '491705552418' from the
    webhook is the single most common cause of duplicate contacts.
    """
    digits = "".join(character for character in str(raw) if character.isdigit())
    return digits.lstrip("0") if digits.startswith("00") else digits


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    # -- de-duplication ----------------------------------------------------

    def already_processed(self, message_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row is not None

    def mark_processed(self, message_id: str, outcome: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO processed_messages VALUES (?,?,?)",
                (message_id, utc_now(), outcome),
            )

    # -- CRM records -------------------------------------------------------

    def upsert_company(self, company: Company) -> Company:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO companies VALUES (?,?,?)",
                (company.company_id, company.name, company.domain),
            )
        return company

    def find_contact_by_phone(self, phone: str) -> Contact | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM contacts WHERE phone = ?", (normalise_phone(phone),)
            ).fetchone()
        return self._as_contact(row) if row else None

    def create_contact(self, phone: str, name: str, company_id: str | None, needs_review: bool) -> Contact:
        contact = Contact(
            contact_id=f"c_{uuid.uuid4().hex[:10]}",
            phone=normalise_phone(phone),
            name=name or f"WhatsApp {normalise_phone(phone)[-4:]}",
            company_id=company_id,
            needs_review=needs_review,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO contacts VALUES (?,?,?,?,?,?)",
                (
                    contact.contact_id,
                    contact.phone,
                    contact.name,
                    contact.company_id,
                    int(contact.needs_review),
                    contact.created_at,
                ),
            )
        return contact

    def contacts_needing_review(self) -> list[Contact]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM contacts WHERE needs_review = 1 ORDER BY created_at"
            ).fetchall()
        return [self._as_contact(row) for row in rows]

    def company(self, company_id: str) -> Company | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM companies WHERE company_id = ?", (company_id,)
            ).fetchone()
        return Company(row["company_id"], row["name"], row["domain"]) if row else None

    # -- conversation log --------------------------------------------------

    def log_message(
        self, contact: Contact, direction: str, body: str, message_id: str, occurred_at: str
    ) -> ConversationEntry:
        """Write one timeline entry against the contact AND its company."""
        entry = ConversationEntry(
            entry_id=f"e_{uuid.uuid4().hex[:10]}",
            contact_id=contact.contact_id,
            company_id=contact.company_id,
            direction=direction,
            body=body,
            message_id=message_id,
            occurred_at=occurred_at,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversation VALUES (?,?,?,?,?,?,?,?)",
                (
                    entry.entry_id,
                    entry.contact_id,
                    entry.company_id,
                    entry.direction,
                    entry.body,
                    entry.message_id,
                    entry.occurred_at,
                    entry.channel,
                ),
            )
        return entry

    def conversation_for_contact(self, contact_id: str) -> list[ConversationEntry]:
        return self._conversation("contact_id = ?", (contact_id,))

    def conversation_for_company(self, company_id: str) -> list[ConversationEntry]:
        """Every WhatsApp message from every contact of that company."""
        return self._conversation("company_id = ?", (company_id,))

    def last_inbound_at(self, contact_id: str) -> datetime | None:
        """Drives the 24-hour customer service window."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT occurred_at FROM conversation WHERE contact_id = ? AND direction = 'inbound' "
                "ORDER BY occurred_at DESC LIMIT 1",
                (contact_id,),
            ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))

    def _conversation(self, where: str, params: tuple[Any, ...]) -> list[ConversationEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM conversation WHERE {where} ORDER BY occurred_at", params
            ).fetchall()
        return [
            ConversationEntry(
                entry_id=row["entry_id"],
                contact_id=row["contact_id"],
                company_id=row["company_id"],
                direction=row["direction"],
                body=row["body"],
                message_id=row["message_id"],
                occurred_at=row["occurred_at"],
                channel=row["channel"],
            )
            for row in rows
        ]

    # -- failures and key/value -------------------------------------------

    def record_failure(self, stage: str, reference: str, detail: str) -> None:
        """Errors are logged, never swallowed - see them with `failures()`."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO failures (happened_at, stage, reference, detail) VALUES (?,?,?,?)",
                (utc_now(), stage, reference, detail[:2000]),
            )

    def failures(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM failures ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, default=str))
            )

    @staticmethod
    def _as_contact(row: sqlite3.Row) -> Contact:
        return Contact(
            contact_id=row["contact_id"],
            phone=row["phone"],
            name=row["name"],
            company_id=row["company_id"],
            needs_review=bool(row["needs_review"]),
            created_at=row["created_at"],
        )
