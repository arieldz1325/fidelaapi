"""One-time links to set a first password (invitations) or reset a forgotten one."""

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException, status

from .config import settings
from .db import now

Purpose = Literal["set_password", "reset"]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(db: sqlite3.Connection, user_id: int, purpose: Purpose) -> str:
    """Returns the raw token (only sent by email); the database keeps just its hash."""
    lifetime = (timedelta(hours=settings.set_password_hours) if purpose == "set_password"
                else timedelta(minutes=settings.reset_password_minutes))
    # Any older unused link of the same kind stops working.
    db.execute("UPDATE password_tokens SET used_at = ? WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
               (now(), user_id, purpose))
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + lifetime).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO password_tokens (user_id, token_hash, purpose, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, _hash(token), purpose, expires, now()),
    )
    return token


def consume(db: sqlite3.Connection, token: str) -> int:
    """Validates and burns the token; returns the user id."""
    row = db.execute(
        "SELECT id, user_id, expires_at, used_at FROM password_tokens WHERE token_hash = ?", (_hash(token),)
    ).fetchone()
    if row is None or row["used_at"] is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is no longer valid. Request a new one.")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has expired. Request a new one.")
    db.execute("UPDATE password_tokens SET used_at = ? WHERE id = ?", (now(), row["id"]))
    return row["user_id"]


def link(token: str, purpose: Purpose) -> str:
    suffix = "&welcome=1" if purpose == "set_password" else ""
    return f"{settings.public_url}/reset-password?token={token}{suffix}"
