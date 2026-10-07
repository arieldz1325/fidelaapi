"""Public endpoints: anyone can upload documents and follow their order with reference + email."""

import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status

from .. import email_templates, mailer, storage
from ..config import settings
from ..db import audit, get_db, now
from ..pipeline import process_document
from ..schemas import TrackResponse, UploadResponse, normalize_email

router = APIRouter(prefix="/uploads", tags=["uploads"])

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I look-alikes
CHUNK = 1024 * 1024


def new_reference(db: sqlite3.Connection) -> str:
    while True:
        reference = "FD-" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM documents WHERE reference = ?", (reference,)).fetchone():
            return reference


async def save_upload(upload: UploadFile, target: Path) -> int:
    """Streams the file to disk and enforces the size limit without loading it in memory."""
    limit = settings.max_file_mb * 1024 * 1024
    size = 0
    with target.open("wb") as out:
        while chunk := await upload.read(CHUNK):
            size += len(chunk)
            if size > limit:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    f"{upload.filename} is larger than {settings.max_file_mb} MB")
            out.write(chunk)
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{upload.filename} is empty")
    return size


@router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def create_upload(
    background: BackgroundTasks,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    files: Annotated[list[UploadFile], File(description="PDF or images of the document")],
    client_name: Annotated[str, Form(min_length=2, max_length=120)],
    client_email: Annotated[str, Form()],
    target_language: Annotated[str, Form(max_length=40)] = "English",
    source_language: Annotated[str, Form(max_length=40)] = "Auto-detect",
    notes: Annotated[str, Form(max_length=2000)] = "",
) -> UploadResponse:
    try:
        email = normalize_email(client_email)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))

    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add at least one file")
    if len(files) > settings.max_files_per_upload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Upload at most {settings.max_files_per_upload} files at once")
    for upload in files:
        if Path(upload.filename or "").suffix.lower() not in settings.allowed_extensions:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                                f"{upload.filename}: use PDF, JPG, PNG, WebP, HEIC or TIFF")

    document_id = uuid.uuid4().hex
    folder = storage.originals_dir(document_id)
    folder.mkdir(parents=True, exist_ok=True)

    saved = []
    for position, upload in enumerate(files, start=1):
        # Stored under our own name; the client's file name is kept only as data.
        stored_name = f"{position}{Path(upload.filename or '').suffix.lower()}"
        size = await save_upload(upload, folder / stored_name)
        saved.append((position, (upload.filename or stored_name)[:200], stored_name, size))

    reference = new_reference(db)
    timestamp = now()
    db.execute(
        """INSERT INTO documents (id, reference, client_name, client_email, source_language, target_language,
                                  notes, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded', ?, ?)""",
        (document_id, reference, client_name.strip(), email, source_language, target_language,
         notes.strip(), timestamp, timestamp),
    )
    db.executemany(
        "INSERT INTO document_files (document_id, position, original_name, stored_name, size_bytes) VALUES (?, ?, ?, ?, ?)",
        [(document_id, *row) for row in saved],
    )
    audit(db, "uploaded", document_id, detail=f"{len(saved)} file(s) from {email}")

    # The background job uses its own connection, so the new rows must be committed first.
    db.commit()
    background.add_task(process_document, document_id)

    name = client_name.strip()
    background.add_task(mailer.send, email_templates.order_received(name, email, reference, len(saved), document_id))
    file_names = [original for _, original, _, _ in saved]
    for staff_email in settings.staff_emails:
        background.add_task(mailer.send, email_templates.staff_new_order(
            staff_email, reference, name, file_names, notes.strip(), document_id))
    return UploadResponse(reference=reference, status="uploaded", files=len(saved))


@router.get("/{reference}", response_model=TrackResponse)
def track(
    reference: str,
    email: Annotated[str, Query()],
    db: Annotated[sqlite3.Connection, Depends(get_db)],
) -> TrackResponse:
    """Order status for the client. Requires the email too, so references cannot be guessed."""
    row = db.execute(
        "SELECT reference, status, page_count, created_at, updated_at FROM documents WHERE reference = ? AND client_email = ?",
        (reference.strip().upper(), email.strip().lower()),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No order matches that reference and email")
    return TrackResponse(**dict(row))
