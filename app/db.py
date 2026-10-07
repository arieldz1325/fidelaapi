import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('client', 'translator', 'manager')),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    reference       TEXT NOT NULL UNIQUE,
    client_name     TEXT NOT NULL,
    client_email    TEXT NOT NULL COLLATE NOCASE,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    notes           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL CHECK (status IN ('uploaded', 'processing', 'ready', 'in_review', 'approved', 'failed')),
    page_count      INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    assigned_to     INTEGER REFERENCES users (id),
    approved_by     INTEGER REFERENCES users (id),
    approved_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_files (
    id            INTEGER PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents (id),
    position      INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    stored_name   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL
);

-- Every AI draft, saved review and approval is kept as an immutable version.
CREATE TABLE IF NOT EXISTS document_versions (
    id          INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents (id),
    version     INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('ai_draft', 'review', 'approved')),
    file_name   TEXT NOT NULL,
    created_by  INTEGER REFERENCES users (id),
    created_at  TEXT NOT NULL,
    UNIQUE (document_id, version)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY,
    document_id TEXT REFERENCES documents (id),
    user_id     INTEGER REFERENCES users (id),
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- Every email we try to send, for support and audit.
CREATE TABLE IF NOT EXISTS email_log (
    id          INTEGER PRIMARY KEY,
    template    TEXT NOT NULL,
    to_email    TEXT NOT NULL,
    sent_to     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'console')),
    provider_id TEXT,
    error       TEXT,
    document_id TEXT REFERENCES documents (id),
    created_at  TEXT NOT NULL
);

-- One-time links to set a first password (invitation) or reset a forgotten one. Only hashes are stored.
CREATE TABLE IF NOT EXISTS password_tokens (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    token_hash TEXT NOT NULL UNIQUE,
    purpose    TEXT NOT NULL CHECK (purpose IN ('set_password', 'reset')),
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_email ON documents (client_email);
CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions (document_id, version);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(SCHEMA)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """For code outside a request (background jobs, startup)."""
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, committed when the request succeeds."""
    with transaction() as connection:
        yield connection


def audit(db: sqlite3.Connection, action: str, document_id: str | None = None,
          user_id: int | None = None, detail: str = "") -> None:
    db.execute(
        "INSERT INTO audit_events (document_id, user_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (document_id, user_id, action, detail, now()),
    )
