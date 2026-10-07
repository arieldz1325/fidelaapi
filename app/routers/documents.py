"""Translator / manager work queue, the editor's draft, saving reviews and approval."""

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from .. import email_templates, mailer, storage
from ..db import audit, get_db, now
from ..pipeline import next_version, process_document
from ..schemas import ClearerCopyRequest, DocumentOut, DocumentStatus, DraftResponse, ReviewPayload, UserOut, VersionOut
from ..security import Staff, create_page_token, verify_page_token

router = APIRouter(prefix="/documents", tags=["documents"])

DOCUMENT_SELECT = """
    SELECT d.*, u.full_name AS assigned_to_name
    FROM documents d LEFT JOIN users u ON u.id = d.assigned_to
"""


def to_out(db: sqlite3.Connection, row: sqlite3.Row) -> DocumentOut:
    files = [f["original_name"] for f in db.execute(
        "SELECT original_name FROM document_files WHERE document_id = ? ORDER BY position", (row["id"],))]
    data = {k: row[k] for k in row.keys() if k in DocumentOut.model_fields}
    return DocumentOut(**data, files=files)


def load_document(db: sqlite3.Connection, document_id: str, user: UserOut) -> sqlite3.Row:
    row = db.execute(DOCUMENT_SELECT + " WHERE d.id = ?", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if user.role == "translator" and row["assigned_to"] not in (None, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This document is assigned to another translator")
    return row


def ensure_editable(db: sqlite3.Connection, row: sqlite3.Row, user: UserOut) -> None:
    if row["status"] in ("uploaded", "processing"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The AI draft is not ready yet")
    if row["status"] == "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Processing failed; retry it first")
    if row["status"] == "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, "Approved documents are locked; reopen it to make changes")
    if row["assigned_to"] is None:
        # Whoever starts working on an unassigned document takes it.
        db.execute("UPDATE documents SET assigned_to = ? WHERE id = ?", (user.id, row["id"]))
        audit(db, "claimed", row["id"], user.id)


def latest_version(db: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT version, kind, file_name FROM document_versions WHERE document_id = ? ORDER BY version DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no draft for this document yet")
    return row


def store_version(db: sqlite3.Connection, document_id: str, kind: str, payload: ReviewPayload, user: UserOut) -> int:
    document = payload.document
    pages = document.get("pages")
    if not isinstance(pages, list) or not pages:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "document.pages is required")
    # The editor works with signed image URLs; versions keep our own stable file names.
    for page in pages:
        page["image"] = f"page-{int(page.get('page_number', 0))}.jpg"

    version = next_version(db, document_id)
    file_name = storage.write_version(document_id, version, {"document": document, "review": payload.review})
    db.execute(
        "INSERT INTO document_versions (document_id, version, kind, file_name, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (document_id, version, kind, file_name, user.id, now()),
    )
    return version


def count_cells(document: dict[str, Any]) -> int:
    return sum(len(row.get("cells", [])) for page in document.get("pages", [])
               for section in page.get("sections", []) for row in section.get("rows", []))


@router.get("", response_model=list[DocumentOut])
def list_documents(
    user: Staff,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> list[DocumentOut]:
    """Managers see everything; translators see unassigned work plus their own."""
    where, params = [], []
    if user.role == "translator":
        where.append("(d.assigned_to IS NULL OR d.assigned_to = ?)")
        params.append(user.id)
    if status_filter:
        where.append("d.status = ?")
        params.append(status_filter)
    sql = DOCUMENT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.created_at DESC"
    return [to_out(db, row) for row in db.execute(sql, params)]


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: str, user: Staff, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> DocumentOut:
    return to_out(db, load_document(db, document_id, user))


@router.post("/{document_id}/claim", response_model=DocumentOut)
def claim(document_id: str, user: Staff, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> DocumentOut:
    row = load_document(db, document_id, user)
    db.execute("UPDATE documents SET assigned_to = ?, updated_at = ? WHERE id = ?", (user.id, now(), row["id"]))
    audit(db, "claimed", row["id"], user.id)
    return to_out(db, load_document(db, document_id, user))


@router.post("/{document_id}/process", response_model=DocumentOut)
def retry_processing(document_id: str, user: Staff, background: BackgroundTasks,
                     db: Annotated[sqlite3.Connection, Depends(get_db)]) -> DocumentOut:
    row = load_document(db, document_id, user)
    if row["status"] not in ("failed", "uploaded"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed or pending uploads can be processed again")
    db.execute("UPDATE documents SET status = 'uploaded', error = NULL, updated_at = ? WHERE id = ?", (now(), row["id"]))
    audit(db, "processing_retried", row["id"], user.id)
    db.commit()
    background.add_task(process_document, row["id"])
    return to_out(db, load_document(db, document_id, user))


@router.get("/{document_id}/draft", response_model=DraftResponse)
def get_draft(document_id: str, user: Staff, request: Request,
              db: Annotated[sqlite3.Connection, Depends(get_db)]) -> DraftResponse:
    """Latest version for the editor, with page images as short-lived signed URLs."""
    row = load_document(db, document_id, user)
    latest = latest_version(db, document_id)
    payload = storage.read_version(document_id, latest["file_name"])

    token = create_page_token(document_id)
    document = payload["document"]
    for page in document["pages"]:
        url = request.url_for("page_image", document_id=document_id, number=page["page_number"])
        page["image"] = f"{url}?token={token}"

    return DraftResponse(document_id=document_id, reference=row["reference"], status=row["status"],
                         version=latest["version"], document=document, review=payload.get("review"))


@router.get("/{document_id}/pages/{number}", name="page_image")
def page_image(document_id: str, number: int, token: Annotated[str | None, Query()] = None) -> FileResponse:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing page token")
    verify_page_token(token, document_id)
    path = storage.page_path(document_id, number)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.put("/{document_id}/review", response_model=VersionOut)
def save_review(document_id: str, body: ReviewPayload, user: Staff,
                db: Annotated[sqlite3.Connection, Depends(get_db)]) -> VersionOut:
    row = load_document(db, document_id, user)
    ensure_editable(db, row, user)
    version = store_version(db, document_id, "review", body, user)
    db.execute("UPDATE documents SET status = 'in_review', updated_at = ? WHERE id = ?", (now(), document_id))
    audit(db, "review_saved", document_id, user.id, f"version {version}")
    return VersionOut(version=version, kind="review", created_at=now(), created_by_name=user.full_name)


@router.post("/{document_id}/approve", response_model=VersionOut)
def approve(document_id: str, body: ReviewPayload, user: Staff, background: BackgroundTasks,
            db: Annotated[sqlite3.Connection, Depends(get_db)]) -> VersionOut:
    row = load_document(db, document_id, user)
    ensure_editable(db, row, user)

    verified = body.review.get("verified") or []
    if len(set(verified)) < count_cells(body.document):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Every field must be verified before approval")

    version = store_version(db, document_id, "approved", body, user)
    timestamp = now()
    db.execute(
        "UPDATE documents SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ? WHERE id = ?",
        (user.id, timestamp, timestamp, document_id),
    )
    audit(db, "approved", document_id, user.id, f"version {version}")
    db.commit()
    background.add_task(mailer.send, email_templates.translation_ready(
        row["client_name"], row["client_email"], row["reference"], document_id))
    return VersionOut(version=version, kind="approved", created_at=timestamp, created_by_name=user.full_name)


@router.post("/{document_id}/request-clearer-copy", status_code=status.HTTP_202_ACCEPTED)
def request_clearer_copy(document_id: str, body: ClearerCopyRequest, user: Staff, background: BackgroundTasks,
                         db: Annotated[sqlite3.Connection, Depends(get_db)]) -> dict:
    """Sent by a person on purpose: a failed AI run is not necessarily the client's fault."""
    row = load_document(db, document_id, user)
    audit(db, "clearer_copy_requested", document_id, user.id, body.message[:200])
    db.commit()
    background.add_task(mailer.send, email_templates.clearer_copy_requested(
        row["client_name"], row["client_email"], row["reference"], body.message, document_id))
    return {"detail": f"We emailed {row['client_email']} asking for a clearer copy."}


@router.post("/{document_id}/reopen", response_model=DocumentOut)
def reopen(document_id: str, user: Staff, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> DocumentOut:
    """Approved versions stay untouched; the next save simply creates a newer version."""
    row = load_document(db, document_id, user)
    if row["status"] != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only approved documents can be reopened")
    db.execute("UPDATE documents SET status = 'in_review', approved_at = NULL, approved_by = NULL, updated_at = ? WHERE id = ?",
               (now(), document_id))
    audit(db, "reopened", document_id, user.id)
    return to_out(db, load_document(db, document_id, user))


@router.get("/{document_id}/versions", response_model=list[VersionOut])
def versions(document_id: str, user: Staff, db: Annotated[sqlite3.Connection, Depends(get_db)]) -> list[VersionOut]:
    load_document(db, document_id, user)
    rows = db.execute(
        """SELECT v.version, v.kind, v.created_at, u.full_name AS created_by_name
           FROM document_versions v LEFT JOIN users u ON u.id = v.created_by
           WHERE v.document_id = ? ORDER BY v.version DESC""",
        (document_id,),
    )
    return [VersionOut(**dict(r)) for r in rows]
