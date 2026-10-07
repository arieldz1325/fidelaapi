import secrets
import sqlite3
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from .. import email_templates, mailer, password_tokens
from ..db import audit, get_db, now
from ..schemas import TrackResponse, UserCreate, UserOut
from ..security import Client, Manager, hash_password

router = APIRouter(tags=["users"])


@router.get("/users", response_model=list[UserOut])
def list_users(_: Manager, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> list[UserOut]:
    rows = db.execute("SELECT id, email, full_name, role FROM users ORDER BY role, full_name")
    return [UserOut(**dict(r)) for r in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, manager: Manager, background: BackgroundTasks,
                db: Annotated[sqlite3.Connection, Depends(get_db)]) -> UserOut:
    if db.execute("SELECT 1 FROM users WHERE email = ?", (body.email,)).fetchone():
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")

    name = body.full_name.strip()
    # Without a password, the account gets a random one nobody knows until the invitation link is used.
    password = body.password or secrets.token_urlsafe(32)
    cursor = db.execute(
        "INSERT INTO users (email, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (body.email, name, hash_password(password), body.role, now()),
    )
    user = UserOut(id=cursor.lastrowid, email=body.email, full_name=name, role=body.role)
    audit(db, "user_created", user_id=manager.id, detail=f"{body.email} as {body.role}")

    if body.password is None:
        token = password_tokens.create(db, user.id, "set_password")
        db.commit()
        background.add_task(mailer.send, email_templates.welcome_set_password(
            name, user.email, user.role, password_tokens.link(token, "set_password")))
    return user


@router.get("/my/documents", response_model=list[TrackResponse])
def my_documents(client: Client, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> list[TrackResponse]:
    """A signed-in client sees every order sent with their email."""
    rows = db.execute(
        "SELECT reference, status, page_count, created_at, updated_at FROM documents WHERE client_email = ? ORDER BY created_at DESC",
        (client.email,),
    )
    return [TrackResponse(**dict(r)) for r in rows]
