import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .db import get_db
from .schemas import Role, UserOut

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def _encode(claims: dict, lifetime: timedelta) -> str:
    issued = datetime.now(timezone.utc)
    return jwt.encode({**claims, "iat": issued, "exp": issued + lifetime}, settings.secret_key, algorithm=ALGORITHM)


def _decode(token: str, scope: str) -> dict:
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if claims.get("scope") != scope:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token not valid for this resource")
    return claims


def create_access_token(user_id: int, role: Role) -> str:
    return _encode({"sub": str(user_id), "role": role, "scope": "api"}, timedelta(hours=settings.access_token_hours))


def create_page_token(document_id: str) -> str:
    """Short-lived token embedded in page image URLs (an <img> cannot send an Authorization header)."""
    return _encode({"doc": document_id, "scope": "pages"}, timedelta(minutes=settings.page_token_minutes))


def verify_page_token(token: str, document_id: str) -> None:
    if _decode(token, "pages").get("doc") != document_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token not valid for this document")


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[sqlite3.Connection, Depends(get_db)],
) -> UserOut:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    claims = _decode(credentials.credentials, "api")
    row = db.execute("SELECT id, email, full_name, role FROM users WHERE id = ?", (int(claims["sub"]),)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return UserOut(**dict(row))


def require_roles(*roles: Role):
    def dependency(user: Annotated[UserOut, Depends(get_current_user)]) -> UserOut:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your profile cannot access this resource")
        return user

    return dependency


CurrentUser = Annotated[UserOut, Depends(get_current_user)]
Staff = Annotated[UserOut, Depends(require_roles("translator", "manager"))]
Manager = Annotated[UserOut, Depends(require_roles("manager"))]
Client = Annotated[UserOut, Depends(require_roles("client"))]
