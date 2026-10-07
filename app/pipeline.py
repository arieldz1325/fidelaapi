"""Background processing of an upload: originals -> page images -> Gemini draft (gem_json.py)."""

import logging
import traceback

import gem_json

from . import storage
from .config import settings
from .db import audit, now, transaction

log = logging.getLogger("fidela.pipeline")


class PipelineError(Exception):
    pass


def prepare_pages(document_id: str, original_files: list[str]) -> list[dict]:
    """Pages of every uploaded file, in upload order, numbered 1..N across files."""
    folder = storage.pages_dir(document_id)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("page-*.jpg"):
        old.unlink()

    pages = []
    for stored_name in original_files:
        path = storage.originals_dir(document_id) / stored_name
        images = gem_json.pdf_page_images(str(path)) if path.suffix == ".pdf" else gem_json.file_page_images(str(path))
        try:
            for image in images:
                if len(pages) >= gem_json.MAX_PAGES:
                    raise PipelineError(f"Documents are limited to {gem_json.MAX_PAGES} pages")
                image = gem_json.normalize_image(image)
                number = len(pages) + 1
                target = storage.page_path(document_id, number)
                image.save(target, "JPEG", quality=gem_json.JPEG_QUALITY, optimize=True)
                pages.append({"number": number, "file": target.name, "path": str(target),
                              "width": image.width, "height": image.height})
        except PipelineError:
            raise
        except Exception as error:
            raise PipelineError(f"Could not read {stored_name}: {error}") from error

    if not pages:
        raise PipelineError("The uploaded files contain no pages")
    return pages


def process_document(document_id: str) -> None:
    with transaction() as db:
        files = [row["stored_name"] for row in db.execute(
            "SELECT stored_name FROM document_files WHERE document_id = ? ORDER BY position", (document_id,))]
        db.execute("UPDATE documents SET status = 'processing', error = NULL, updated_at = ? WHERE id = ?",
                   (now(), document_id))

    try:
        pages = prepare_pages(document_id, files)
        with transaction() as db:
            db.execute("UPDATE documents SET page_count = ?, updated_at = ? WHERE id = ?",
                       (len(pages), now(), document_id))

        if not settings.gemini_api_key:
            raise PipelineError("GEMINI_API_KEY is not configured on the server")
        gem_json.GEMINI_API_KEY = settings.gemini_api_key

        try:
            draft = gem_json.extract_document(pages)
        except gem_json.ExtractionError as error:
            raise PipelineError(f"AI extraction failed: {error}") from error

        with transaction() as db:
            version = next_version(db, document_id)
            file_name = storage.write_version(document_id, version, {"document": draft, "review": None})
            db.execute(
                "INSERT INTO document_versions (document_id, version, kind, file_name, created_at) VALUES (?, ?, 'ai_draft', ?, ?)",
                (document_id, version, file_name, now()),
            )
            db.execute("UPDATE documents SET status = 'ready', updated_at = ? WHERE id = ?", (now(), document_id))
            audit(db, "ai_draft_created", document_id, detail=f"version {version}, {len(pages)} page(s)")

    except Exception as error:
        message = str(error) if isinstance(error, PipelineError) else "Unexpected processing error"
        log.error("Processing %s failed: %s\n%s", document_id, error, traceback.format_exc())
        with transaction() as db:
            db.execute("UPDATE documents SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                       (message, now(), document_id))
            audit(db, "processing_failed", document_id, detail=message)


def next_version(db, document_id: str) -> int:
    row = db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM document_versions WHERE document_id = ?",
                     (document_id,)).fetchone()
    return row[0]
