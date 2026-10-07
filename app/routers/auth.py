import sqlite3
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from .. import email_templates, mailer, password_tokens
from ..db import audit, get_db
from ..schemas import ForgotPasswordRequest, LoginRequest, ResetPasswordRequest, TokenResponse, UserOut
from ..security import CurrentUser, create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Checked when the email is unknown, so both cases take the same time.
_DUMMY_HASH = hash_password("timing-equalizer")


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> TokenResponse:
    row = db.execute("SELECT id, email, full_name, role, password_hash FROM users WHERE email = ?",
                     (body.email,)).fetchone()
    valid = verify_password(body.password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    user = UserOut(id=row["id"], email=row["email"], full_name=row["full_name"], role=row["role"])
    audit(db, "login", user_id=user.id)
    return TokenResponse(access_token=create_access_token(user.id, user.role), user=user)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
def forgot_password(body: ForgotPasswordRequest, background: BackgroundTasks,
                    db: Annotated[sqlite3.Connection, Depends(get_db)]) -> dict:
    """Same answer whether or not the email exists, so accounts cannot be discovered."""
    row = db.execute("SELECT id, email, full_name FROM users WHERE email = ?", (body.email,)).fetchone()
    if row:
        token = password_tokens.create(db, row["id"], "reset")
        audit(db, "password_reset_requested", user_id=row["id"])
        db.commit()
        background.add_task(mailer.send, email_templates.password_reset(
            row["full_name"], row["email"], password_tokens.link(token, "reset")))
    return {"detail": "If an account exists for that email, we sent a link to reset the password."}


@router.post("/reset-password", response_model=TokenResponse)
def reset_password(body: ResetPasswordRequest, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> TokenResponse:
    """Sets a new password from an invitation or reset link, and signs the person in."""
    user_id = password_tokens.consume(db, body.token)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.password), user_id))
    row = db.execute("SELECT id, email, full_name, role FROM users WHERE id = ?", (user_id,)).fetchone()
    user = UserOut(**dict(row))
    audit(db, "password_set", user_id=user.id)
    return TokenResponse(access_token=create_access_token(user.id, user.role), user=user)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return user
