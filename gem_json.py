"""
FIEL extraction: turns an uploaded document (PDF or image) into one image per
page plus a JSON draft (structure + translation) for the translator's editor.

Usage
  .venv/bin/python gem_json.py <file> [--out DIR] [--dry-run]
  .venv/bin/python gem_json.py --preview DIR/<document-folder>

Output (DIR defaults to fiel/public/demo, where the Angular editor reads it)
  DIR/<slug>/page-1.jpg ... page-N.jpg   normalized page images
  DIR/<slug>/document.json               pages[] with sections/rows/cells
  DIR/<slug>/preview.html                quick standalone review page
  DIR/index.json                         list of documents the editor offers
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pymupdf
from PIL import Image, ImageOps, ImageSequence

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


# ============================================================
# CONFIGURATION
# ============================================================

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TARGET_LANGUAGE = "English"
LOW_CONFIDENCE = 0.85

PAGES_PER_REQUEST = 3
MAX_PARALLEL_REQUESTS = 3
MAX_PAGES = 30

PDF_DPI = 200
MAX_IMAGE_SIDE = 2400
JPEG_QUALITY = 90
REQUEST_TIMEOUT = 240

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "fiel", "public", "demo")

MODELS = [
    # "gemini-3.8-flash",
    # "gemini-3.7-flash",
    # "gemini-3.6-flash",
    # "gemini-3.5-flash",
    "gemini-3-flash-preview",
    # "gemini-2.5-flash",
    # "gemini-2.5-pro",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]


# ============================================================
# PROMPT
# ============================================================

DOCUMENT_PROMPT = f"""
You are the extraction and draft-translation engine of a CERTIFIED TRANSLATION
platform. A certified human translator will review your output, so accuracy
and honesty matter more than completeness or appearance.

You receive ONE OR MORE PAGE IMAGES from the same upload. Each image is
preceded by a label "PAGE n". Pages can belong to different documents (for
example a passport and a permit scanned together). Treat every page
independently: never move content from one page to another.

Return ONE JSON object that follows the provided schema, with exactly one
entry in "pages" for every page image you received, using the same
page_number, in the same order. Do not return HTML. Do not return
explanations.

You do NOT know the document type. Do not assume one. Do not add fields,
labels or sections that are not visible. The images are the only source of
truth.

==================================================
1. STRUCTURE
==================================================

Describe EACH PAGE as SECTIONS -> ROWS -> CELLS, in natural reading order
(top to bottom, left to right).

- A section is a visually grouped block (a boxed area, a titled group, a
  header, a footer, a stamp area, a margin).
- A row is a horizontal line of cells inside a section.
- A cell is one label/value pair, one text block, one checkbox, or one
  non-text element (annotation).
- "width" is the cell's approximate share of the row width in percent.
  The widths of each row must add up to 100.
- Keep fields that share a line in the same row. Do not turn a multi-column
  layout into a single column.
- Include empty fields that are visibly present (blank witness fields, etc.)
  with an empty value. Do not invent values for them.

Do NOT try to reproduce exact pixel layout. Structure is what matters.

==================================================
2. READ EVERYTHING
==================================================

Read all legible text, including small labels, footers, printer marks,
vertical or rotated text in the margins, and text inside stamps and seals.

Transcribe "original" EXACTLY as printed or written:
- keep accents and special characters (Ñ, á, é, ç, ü...) ONLY where they are
  actually printed. Many official documents print names WITHOUT accents
  (DIAZ, LOPEZ, BOGOTA). Never add an accent that is not on the page.
- keep truncated text truncated (e.g. "BUENAVE-" stays "BUENAVE-";
  never complete it)
- do not correct spelling, do not normalize names, do not change numbers
- if a label is only a number or code (e.g. "8"), keep only that; do not
  invent the missing words

==================================================
3. TRANSLATION INTO {TARGET_LANGUAGE.upper()}
==================================================

Fill "translated" with a draft translation into {TARGET_LANGUAGE}.

TRANSLATE:
- titles, section headings, labels, instructions, notes, certifications
- values that are ordinary words: sex (FEMENINO -> FEMALE), nationality
  (COLOMBIANA -> COLOMBIAN), blood type / RH (POSITIVO -> POSITIVE),
  document types (CERTIFICADO DE NACIDO VIVO -> LIVE BIRTH CERTIFICATE),
  relationships, yes/no, marital status, professions
- month names and abbreviations inside dates (ENE->JAN, ABR->APR, AGO->AUG,
  DIC->DEC); keep the day/year digits and order unchanged
- descriptive institution names (REGISTRADURIA NACIONAL DEL ESTADO CIVIL ->
  NATIONAL CIVIL REGISTRY OFFICE)

DO NOT TRANSLATE (copy exactly, set "is_literal": true):
- personal names, place names, street addresses
- ID numbers, serial numbers, codes, reference numbers, MRZ lines
- numeric dates with no words

For literal values, "translated" must be IDENTICAL to "original", character
by character. Do not remove accents or Ñ (ZUÑIGA stays ZUÑIGA).

Footers, certification text and notary/authentication text at the bottom or
on the back of the page are part of the document: transcribe and translate
them in full, including dates, names and titles that appear in them.

When characters are printed in separate boxes (e.g. "2 0 1 5"), keep the
spacing in "original" and write the compact form in "translated" ("2015").

If a label is already bilingual and contains {TARGET_LANGUAGE}, use that
{TARGET_LANGUAGE} text as the translation.

Preserve capitalization style (ALL CAPS stays ALL CAPS).

==================================================
4. NON-TEXT ELEMENTS -> ANNOTATIONS
==================================================

Signatures, seals, stamps, fingerprints, photos, barcodes, QR codes, logos,
coats of arms and handwriting must be reported as cells with
kind = "annotation" and the proper annotation_type.

Their "translated" value is a translator's bracketed note, for example:
- "[Signature]"
- "[Signature: illegible]"
- "[Fingerprint]"
- "[Photograph of the holder]"
- "[Barcode]"
- "[Seal: Republic of Colombia - Notary 68 of Bogotá - Acting Notary]"
- "[Stamp: Notary 68 - Civil Registry]"
- "[Logo: Ontario]"

EVERY annotation MUST have a "value" object. "value.translated" is the
bracketed note; it is never empty. "value.original" is the legible text of
the element, or "" if it has none.

For seals and stamps, include the legible text inside them, translated.
Never transcribe a signature as if it were printed text.

Only create an annotation when the mark is actually visible. An empty
signature line (e.g. for a witness who did not sign) is NOT a signature:
report it as a field with an empty value.

Illegible text anywhere: use "[Illegible]" in "translated" and lower the
confidence.

==================================================
5. CHECKBOXES
==================================================

Use kind = "checkbox" with "checked" true/false. The label goes in "label".
If a box contains a character or number instead of a mark, use kind = "field"
and put that content in "value".

==================================================
6. CONFIDENCE AND BOXES
==================================================

"confidence" (0.0 to 1.0) is your honest certainty that BOTH the reading and
the translation of that cell are correct. Use this scale:
- 0.95-0.99: clean printed text, unambiguous
- 0.80-0.94: small, faded, folded, crossed by a stamp or signature, or
  any single character you had to infer
- 0.50-0.79: handwritten, partially hidden, truncated, or the translation
  of a legal/technical term that may need a professional decision
- below 0.50: you are guessing
Do not give everything the same value.

"box" is the cell's location in ITS OWN PAGE IMAGE as [ymin, xmin, ymax, xmax],
normalized to 0-1000 (0,0 is the top-left corner of that page). It is only used to highlight the area for the reviewer,
so an approximation is fine.

Use "note" only for short warnings to the reviewer (e.g. "text cut off in
original", "digit could be 3 or 8"). Otherwise null.

==================================================
7. FINAL CHECK
==================================================

Before answering, verify:
- there is exactly one page entry per PAGE image received, with its number
- every visible text and element is present exactly once, on its own page
- nothing was invented
- names and numbers are copied exactly, including accents and truncation,
  and no accent was added that is not printed
- literal values have translated identical to original
- footer / certification / notary text is included
- every annotation has a non-empty bracketed value.translated
- every translatable word value was translated
- every row's widths add up to 100
- signatures, seals, stamps, photos and fingerprints are annotations
- every box is relative to the page it belongs to
"""


# ============================================================
# RESPONSE SCHEMA
# ============================================================

TEXT_SCHEMA = {
    "type": "object",
    "nullable": True,
    "properties": {
        "original": {"type": "string"},
        "translated": {"type": "string"},
        "is_literal": {"type": "boolean"},
    },
    "required": ["original", "translated", "is_literal"],
    "propertyOrdering": ["original", "translated", "is_literal"],
}

BOX_SCHEMA = {
    "type": "array",
    "items": {"type": "integer"},
    "minItems": 4,
    "maxItems": 4,
}

CELL_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": ["field", "text", "checkbox", "annotation"]},
        "width": {"type": "integer"},
        "label": TEXT_SCHEMA,
        "value": TEXT_SCHEMA,
        "checked": {"type": "boolean", "nullable": True},
        "annotation_type": {
            "type": "string",
            "nullable": True,
            "enum": [
                "signature", "seal", "stamp", "fingerprint", "photo",
                "barcode", "qr_code", "logo", "handwriting", "other",
            ],
        },
        "confidence": {"type": "number"},
        "box": BOX_SCHEMA,
        "note": {"type": "string", "nullable": True},
    },
    "required": ["id", "kind", "width", "confidence", "box"],
    "propertyOrdering": [
        "id", "kind", "width", "label", "value", "checked",
        "annotation_type", "confidence", "box", "note",
    ],
}

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": TEXT_SCHEMA,
        "box": BOX_SCHEMA,
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"cells": {"type": "array", "items": CELL_SCHEMA}},
                "required": ["cells"],
            },
        },
    },
    "required": ["id", "box", "rows"],
    "propertyOrdering": ["id", "title", "box", "rows"],
}

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_number": {"type": "integer"},
        "orientation": {"type": "string", "enum": ["portrait", "landscape"]},
        "document_title": TEXT_SCHEMA,
        "sections": {"type": "array", "items": SECTION_SCHEMA},
    },
    "required": ["page_number", "orientation", "sections"],
    "propertyOrdering": ["page_number", "orientation", "document_title", "sections"],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_language": {"type": "string"},
        "target_language": {"type": "string"},
        "pages": {"type": "array", "items": PAGE_SCHEMA},
    },
    "required": ["source_language", "target_language", "pages"],
    "propertyOrdering": ["source_language", "target_language", "pages"],
}


# ============================================================
# PAGE PREPARATION (PDF / images -> normalized JPEG per page)
# ============================================================

def prepare_pages(source_path, folder):
    """Writes page-N.jpg files into `folder` and returns their metadata."""
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):
        if re.fullmatch(r"page-\d+\.jpg", name):
            os.remove(os.path.join(folder, name))

    if source_path.lower().endswith(".pdf"):
        images = pdf_page_images(source_path)
    else:
        images = file_page_images(source_path)

    pages = []
    for number, image in enumerate(images, start=1):
        if number > MAX_PAGES:
            print(f"WARNING: only the first {MAX_PAGES} pages are processed.")
            break
        image = normalize_image(image)
        file_name = f"page-{number}.jpg"
        path = os.path.join(folder, file_name)
        image.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        pages.append({"number": number, "file": file_name, "path": path,
                      "width": image.width, "height": image.height})
    return pages


def pdf_page_images(path):
    with pymupdf.open(path) as pdf:
        for page in pdf:
            pixmap = page.get_pixmap(dpi=PDF_DPI, alpha=False)
            yield Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def file_page_images(path):
    """JPEG, PNG, WebP, HEIC and single- or multi-page TIFF."""
    with Image.open(path) as image:
        for frame in ImageSequence.Iterator(image):
            yield frame.copy()


def normalize_image(image):
    """Upright, RGB on white, and no larger than MAX_IMAGE_SIDE."""
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    return image


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


# ============================================================
# GEMINI
# ============================================================

class ExtractionError(Exception):
    pass


class TruncatedResponse(ExtractionError):
    pass


_print_lock = threading.Lock()


def log(label, message):
    with _print_lock:
        print(f"  {label:<12} {message}")


def page_label(pages):
    numbers = [p["number"] for p in pages]
    return f"[p{numbers[0]}]" if len(numbers) == 1 else f"[p{numbers[0]}-{numbers[-1]}]"


def call_gemini(model, payload):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def error_message(body):
    try:
        return json.loads(body).get("error", {}).get("message", body)
    except ValueError:
        return body


def build_payload(pages):
    numbers = [p["number"] for p in pages]
    parts = [{"text": DOCUMENT_PROMPT}]
    for page in pages:
        with open(page["path"], "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        parts.append({"text": f"PAGE {page['number']}"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": data}})
    parts.append({"text": f"Return exactly {len(numbers)} page entries, page_number {numbers}."})
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }


def extract_group(pages, exhausted_models, models_lock):
    """One request for up to PAGES_PER_REQUEST pages, falling back across models."""
    label = page_label(pages)
    expected = [p["number"] for p in pages]
    payload = build_payload(pages)

    for model in MODELS:
        with models_lock:
            if model in exhausted_models:
                continue

        started = time.time()
        try:
            status, body = call_gemini(model, payload)
        except (urllib.error.URLError, TimeoutError) as error:
            log(label, f"{model}: request error {error}")
            continue
        elapsed = time.time() - started

        if status == 429:
            # Quota is per model: skip it for the rest of this run, other groups included.
            with models_lock:
                exhausted_models.add(model)
            log(label, f"{model}: quota exceeded, skipping model")
            continue
        if status != 200:
            log(label, f"{model}: HTTP {status} {error_message(body)[:160]}")
            continue

        candidate = (json.loads(body).get("candidates") or [{}])[0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise TruncatedResponse(f"{model} hit the output limit")

        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought"))
        try:
            result = json.loads(text)
        except ValueError:
            log(label, f"{model}: response is not valid JSON")
            continue

        received = sorted(p.get("page_number") for p in result.get("pages", []))
        if received != expected:
            log(label, f"{model}: expected pages {expected}, got {received}")
            continue

        log(label, f"{model}: OK in {elapsed:.1f}s")
        return result, {"pages": expected, "model": model, "seconds": round(elapsed, 1)}

    raise ExtractionError(f"no model could extract pages {expected}")


def extract_group_or_split(pages, exhausted_models, models_lock):
    """If a multi-page answer is cut off by the output limit, retry page by page."""
    try:
        return [extract_group(pages, exhausted_models, models_lock)]
    except TruncatedResponse as error:
        if len(pages) == 1:
            raise ExtractionError(f"page {pages[0]['number']} is too long for one response ({error})")
        log(page_label(pages), f"{error}; retrying one page at a time")
        return [extract_group([page], exhausted_models, models_lock) for page in pages]


def extract_document(pages):
    groups = chunk(pages, PAGES_PER_REQUEST)
    exhausted_models = set()
    models_lock = threading.Lock()

    print(f"\nSending {len(pages)} page(s) in {len(groups)} request(s) "
          f"(max {PAGES_PER_REQUEST} pages each, {min(len(groups), MAX_PARALLEL_REQUESTS)} in parallel)")

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        futures = [pool.submit(extract_group_or_split, group, exhausted_models, models_lock) for group in groups]
        outcomes = [outcome for future in futures for outcome in future.result()]

    results = [result for result, _ in outcomes]
    by_number = {page["page_number"]: page for result in results for page in result["pages"]}
    image_for = {p["number"]: p for p in pages}

    merged_pages = []
    for number in sorted(by_number):
        page = by_number[number]
        page["image"] = image_for[number]["file"]
        page["image_size"] = [image_for[number]["width"], image_for[number]["height"]]
        merged_pages.append(page)

    return {
        "source_language": results[0].get("source_language", ""),
        "target_language": results[0].get("target_language", TARGET_LANGUAGE),
        "pages": merged_pages,
        "extraction": {"requests": [meta for _, meta in outcomes]},
    }


# ============================================================
# VALIDATION
# ============================================================

def validate(doc):
    problems = []
    stats = {"pages": 0, "cells": 0, "low_confidence": 0, "untranslated": 0, "annotations": 0}
    confidences = set()

    for page in doc.get("pages", []):
        stats["pages"] += 1
        p = page.get("page_number")
        seen_ids = set()

        for s in page.get("sections", []):
            for r_index, row in enumerate(s.get("rows", [])):
                cells = row.get("cells", [])
                total = sum(c.get("width", 0) for c in cells)
                if cells and not 95 <= total <= 105:
                    problems.append(f"page {p} section {s.get('id')} row {r_index}: widths sum to {total}")

                for c in cells:
                    stats["cells"] += 1
                    cid = c.get("id")
                    if cid in seen_ids:
                        problems.append(f"page {p}: duplicate cell id {cid}")
                    seen_ids.add(cid)
                    confidences.add(round(c.get("confidence", 0), 2))

                    if c.get("confidence", 0) < LOW_CONFIDENCE:
                        stats["low_confidence"] += 1
                    if c.get("kind") == "annotation":
                        stats["annotations"] += 1
                        if not ((c.get("value") or {}).get("translated") or "").strip():
                            problems.append(f"page {p} cell {cid}: annotation without bracketed note")

                    box = c.get("box", [])
                    if len(box) != 4 or not all(0 <= v <= 1000 for v in box):
                        problems.append(f"page {p} cell {cid}: invalid box {box}")

                    for part in ("label", "value"):
                        t = c.get(part)
                        if not t:
                            continue
                        original = (t.get("original") or "").strip()
                        translated = (t.get("translated") or "").strip()
                        if t.get("is_literal") and original != translated:
                            problems.append(f"page {p} cell {cid}: literal changed '{original}' -> '{translated}'")
                        elif (
                            not t.get("is_literal") and original and original == translated
                            and any(ch.isalpha() for ch in original)
                        ):
                            stats["untranslated"] += 1

    if stats["cells"] > 5 and len(confidences) <= 2:
        problems.append(f"confidence is not discriminating: values {sorted(confidences)}")

    return problems, stats


# ============================================================
# EDITABLE PREVIEW (per page: original image with boxes | editable draft)
# ============================================================

def esc(value):
    return html.escape(value or "", quote=True)


def text_html(t, css_class, path, placeholder=""):
    t = t or {}
    original = t.get("original") or ""
    orig_html = f'<span class="orig">{esc(original)}</span>' if original else ""
    return (
        f'<div class="{css_class}">'
        f'<span class="t" contenteditable="plaintext-only" spellcheck="true" '
        f'data-path="{path}.translated" data-placeholder="{esc(placeholder)}">'
        f'{esc(t.get("translated"))}</span>{orig_html}</div>'
    )


def box_style(box):
    ymin, xmin, ymax, xmax = box
    return f"top:{ymin / 10}%;left:{xmin / 10}%;height:{(ymax - ymin) / 10}%;width:{(xmax - xmin) / 10}%"


def render_page(page, pi):
    overlay, body = [], []
    prefix = f"pages.{pi}"
    body.append(text_html(page.get("document_title"), "doc-title", f"{prefix}.document_title", "Document title"))

    for si, s in enumerate(page.get("sections", [])):
        body.append("<section>")
        if s.get("title"):
            body.append(text_html(s["title"], "section-title", f"{prefix}.sections.{si}.title"))
        for ri, row in enumerate(s.get("rows", [])):
            body.append('<div class="row">')
            for ci, c in enumerate(row.get("cells", [])):
                path = f"{prefix}.sections.{si}.rows.{ri}.cells.{ci}"
                cid = esc(f"{pi}-{c.get('id')}")
                kind = c.get("kind", "")
                classes = ["cell", kind] + (["low"] if c.get("confidence", 1) < LOW_CONFIDENCE else [])

                inner = text_html(c["label"], "label", f"{path}.label") if c.get("label") else ""
                if kind == "checkbox":
                    mark = "☒" if c.get("checked") else "☐"
                    inner += f'<div class="value"><button class="chk" data-path="{path}.checked">{mark}</button></div>'
                else:
                    inner += text_html(c.get("value"), "value", f"{path}.value", "[...]" if kind == "annotation" else "")
                if c.get("note"):
                    inner += f'<div class="note">⚠ {esc(c["note"])}</div>'
                inner += f'<div class="conf">{c.get("confidence", 0):.2f}</div>'

                body.append(f'<div class="{" ".join(classes)}" data-id="{cid}" '
                            f'style="flex-basis:{int(c.get("width", 0))}%">{inner}</div>')
                if len(c.get("box", [])) == 4:
                    overlay.append(f'<div class="hl" data-id="{cid}" style="{box_style(c["box"])}"></div>')
            body.append("</div>")
        body.append("</section>")

    return (
        f'<h2 class="page-label">Page {page.get("page_number", pi + 1)}</h2>'
        f'<div class="wrap"><div class="left"><div class="img">'
        f'<img src="{esc(page.get("image"))}">{"".join(overlay)}</div></div>'
        f'<div class="right">{"".join(body)}</div></div>'
    )


def render_preview(doc):
    pages_html = "".join(render_page(page, pi) for pi, page in enumerate(doc.get("pages", [])))
    doc_json = json.dumps(doc, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>FIEL review</title>
<style>
body {{ margin:0; font-family: Arial, sans-serif; font-size:12px; background:#f4f4f5; }}
.bar {{ position:sticky; top:0; z-index:5; display:flex; gap:12px; align-items:center;
        padding:8px 16px; background:#111827; color:#fff; }}
.bar button {{ background:#2563eb; color:#fff; border:0; padding:6px 12px; border-radius:4px; cursor:pointer; }}
.bar .grow {{ flex:1; }}
.page-label {{ margin:16px 16px 0; font-size:13px; color:#52525b; text-transform:uppercase; letter-spacing:.06em; }}
.wrap {{ display:flex; gap:16px; padding:8px 16px 16px; align-items:flex-start; }}
.left {{ flex:1; position:sticky; top:56px; }}
.img {{ position:relative; }}
.img img {{ width:100%; display:block; }}
.hl {{ position:absolute; border:1px solid rgba(37,99,235,.35); pointer-events:none; }}
.hl.on {{ background:rgba(37,99,235,.25); border:2px solid #2563eb; }}
.right {{ flex:1; background:#fff; padding:24px; border:1px solid #ddd; }}
.doc-title {{ font-size:16px; font-weight:bold; text-align:center; margin-bottom:8px; }}
section {{ border:1px solid #333; margin-bottom:6px; }}
.section-title {{ background:#e5e7eb; font-weight:bold; padding:3px 6px; }}
.row {{ display:flex; }}
.cell {{ border:1px solid #ccc; padding:3px 6px; box-sizing:border-box; position:relative; }}
.cell.low {{ background:#fef3c7; }}
.cell.edited {{ background:#dcfce7; }}
.cell.annotation .value .t {{ font-style:italic; color:#555; }}
.cell.on {{ outline:2px solid #2563eb; }}
.label {{ font-size:10px; color:#555; }}
.value {{ font-weight:bold; min-height:14px; }}
.t {{ display:block; min-height:14px; outline:none; border-radius:2px; white-space:pre-wrap; }}
.t:hover {{ background:rgba(37,99,235,.06); }}
.t:focus {{ background:#fff; box-shadow:0 0 0 1px #2563eb; }}
.t:empty::before {{ content:attr(data-placeholder); color:#bbb; }}
.orig {{ display:block; font-size:9px; color:#999; font-weight:normal; }}
.note {{ color:#b45309; font-size:10px; }}
.conf {{ position:absolute; top:1px; right:3px; font-size:8px; color:#aaa; }}
.chk {{ font-size:14px; background:none; border:0; cursor:pointer; padding:0; }}
</style></head>
<body>
<div class="bar">
  <strong>FIEL review</strong>
  <span>{len(doc.get("pages", []))} page(s)</span>
  <span id="count">0 edited</span>
  <span class="grow"></span>
  <button id="save">Download reviewed JSON</button>
</div>
{pages_html}
<script id="doc-data" type="application/json">{doc_json}</script>
<script>
var doc = JSON.parse(document.getElementById('doc-data').textContent);
var edited = new Set();

function setPath(path, value) {{
  var keys = path.split('.');
  var cur = doc;
  for (var i = 0; i < keys.length - 1; i++) {{
    if (cur[keys[i]] == null) cur[keys[i]] = {{ original: '', translated: '', is_literal: false }};
    cur = cur[keys[i]];
  }}
  cur[keys[keys.length - 1]] = value;
}}

function markEdited(el) {{
  var cell = el.closest('.cell');
  if (cell) {{ cell.classList.add('edited'); edited.add(cell.dataset.id); }}
  document.getElementById('count').textContent = edited.size + ' edited';
}}

document.querySelectorAll('.t').forEach(function (el) {{
  el.addEventListener('input', function () {{
    setPath(el.dataset.path, el.innerText.replace(/\\n$/, ''));
    markEdited(el);
  }});
}});

document.querySelectorAll('.chk').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    var checked = btn.textContent !== '☒';
    btn.textContent = checked ? '☒' : '☐';
    setPath(btn.dataset.path, checked);
    markEdited(btn);
  }});
}});

document.querySelectorAll('.cell').forEach(function (cell) {{
  cell.addEventListener('mouseenter', function () {{
    document.querySelectorAll('.hl[data-id="' + cell.dataset.id + '"]').forEach(function (el) {{ el.classList.add('on'); }});
    cell.classList.add('on');
  }});
  cell.addEventListener('mouseleave', function () {{
    document.querySelectorAll('.on').forEach(function (el) {{ el.classList.remove('on'); }});
  }});
}});

document.getElementById('save').addEventListener('click', function () {{
  var blob = new Blob([JSON.stringify(doc, null, 2)], {{ type: 'application/json' }});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'document.reviewed.json';
  a.click();
  URL.revokeObjectURL(a.href);
}});
</script>
</body></html>"""


# ============================================================
# OUTPUT
# ============================================================

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "document"


def register_document(out_dir, slug, name, page_count):
    """Adds or replaces the document in index.json, which feeds the editor's document picker."""
    index_path = os.path.join(out_dir, "index.json")
    entries = []
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            entries = json.load(f)

    path = f"{slug}/document.json"
    entries = [e for e in entries if e.get("path") != path]
    entries.append({"name": name, "path": path, "pages": page_count})
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def write_preview(folder, doc):
    with open(os.path.join(folder, "preview.html"), "w", encoding="utf-8") as f:
        f.write(render_preview(doc))


def print_report(doc):
    problems, stats = validate(doc)
    print("\nRESULT")
    print(f"  Pages                 : {stats['pages']}")
    print(f"  Cells                 : {stats['cells']}")
    print(f"  Annotations           : {stats['annotations']}")
    print(f"  Low confidence        : {stats['low_confidence']}")
    print(f"  Possibly untranslated : {stats['untranslated']}")
    print(f"  Structure issues      : {len(problems)}")
    for problem in problems[:20]:
        print(f"    - {problem}")


# ============================================================
# COMMANDS
# ============================================================

def run_extraction(source_path, out_dir, dry_run):
    if not os.path.exists(source_path):
        sys.exit(f"ERROR: file not found: {source_path}")
    if not dry_run and not GEMINI_API_KEY:
        sys.exit("ERROR: set GEMINI_API_KEY first: export GEMINI_API_KEY=...")

    stem = os.path.splitext(os.path.basename(source_path))[0]
    slug = slugify(stem)
    folder = os.path.join(out_dir, slug)

    print("=" * 70)
    print(f"FIEL EXTRACTION  |  {os.path.basename(source_path)}  ->  {TARGET_LANGUAGE}")
    print("=" * 70)

    pages = prepare_pages(source_path, folder)
    if not pages:
        sys.exit("ERROR: the file has no pages")
    for page in pages:
        print(f"  page {page['number']:<3} {page['width']}x{page['height']}  ->  {os.path.relpath(page['path'])}")

    groups = chunk(pages, PAGES_PER_REQUEST)
    if dry_run:
        print(f"\nDry run: {len(groups)} request(s) would be sent: "
              + ", ".join(page_label(g) for g in groups))
        return

    started = time.time()
    try:
        doc = extract_document(pages)
    except ExtractionError as error:
        sys.exit(f"\nFAILED: {error}")

    doc["source_file"] = os.path.basename(source_path)
    doc["extraction"]["seconds"] = round(time.time() - started, 1)

    with open(os.path.join(folder, "document.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    write_preview(folder, doc)
    register_document(out_dir, slug, stem, len(pages))

    print_report(doc)
    print(f"\n  Total time : {doc['extraction']['seconds']}s")
    print(f"  JSON       : {os.path.relpath(os.path.join(folder, 'document.json'))}")
    print(f"  Preview    : {os.path.relpath(os.path.join(folder, 'preview.html'))}")


def run_preview(folder):
    with open(os.path.join(folder, "document.json"), encoding="utf-8") as f:
        doc = json.load(f)
    write_preview(folder, doc)
    print_report(doc)
    print(f"\n  Preview : {os.path.relpath(os.path.join(folder, 'preview.html'))}")


def main():
    parser = argparse.ArgumentParser(description="Extract and draft-translate a document for FIEL.")
    parser.add_argument("file", nargs="?", help="PDF or image (JPEG, PNG, WebP, HEIC, TIFF)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory (default: fiel/public/demo)")
    parser.add_argument("--dry-run", action="store_true", help="prepare page images without calling Gemini")
    parser.add_argument("--preview", metavar="FOLDER", help="regenerate preview.html from an existing document folder")
    args = parser.parse_args()

    if args.preview:
        run_preview(args.preview)
    elif args.file:
        run_extraction(args.file, args.out, args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
