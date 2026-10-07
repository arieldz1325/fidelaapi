import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["client", "translator", "manager"]
DocumentStatus = Literal["uploaded", "processing", "ready", "in_review", "approved", "failed"]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_PATTERN.match(value):
        raise ValueError("Enter a valid email address")
    return value


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: Role


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return normalize_email(value)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class UserCreate(BaseModel):
    email: str
    full_name: str = Field(min_length=2, max_length=120)
    role: Role
    # Leave empty to email an invitation so the person chooses their own password.
    password: str | None = Field(default=None, min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return normalize_email(value)


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return normalize_email(value)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=10, max_length=128)


class ClearerCopyRequest(BaseModel):
    message: str = Field(default="", max_length=2000)


class UploadResponse(BaseModel):
    reference: str
    status: DocumentStatus
    files: int


class TrackResponse(BaseModel):
    reference: str
    status: DocumentStatus
    page_count: int
    created_at: str
    updated_at: str


class DocumentOut(BaseModel):
    id: str
    reference: str
    client_name: str
    client_email: str
    source_language: str
    target_language: str
    notes: str
    status: DocumentStatus
    page_count: int
    error: str | None
    assigned_to: int | None
    assigned_to_name: str | None
    approved_at: str | None
    created_at: str
    updated_at: str
    files: list[str] = []


class DraftResponse(BaseModel):
    document_id: str
    reference: str
    status: DocumentStatus
    version: int
    document: dict[str, Any]
    review: dict[str, Any] | None


class ReviewPayload(BaseModel):
    """What the editor saves: the corrected document plus the review state (verified fields, page elements...)."""

    document: dict[str, Any]
    review: dict[str, Any]


class VersionOut(BaseModel):
    version: int
    kind: str
    created_at: str
    created_by_name: str | None
