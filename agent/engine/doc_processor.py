"""
Document Processor — Multimodal document understanding for Grippy.

Handles:
  - File upload storage with TTL
  - Vision LLM extraction of structured data from images/PDFs
  - Automatic injection of extracted data into complaint context

Supports: PDF, PNG, JPG, JPEG
"""

import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

UPLOAD_DIR = os.environ.get("GRIPPY_UPLOAD_DIR", "/tmp/grippy_uploads")
UPLOAD_TTL = int(os.environ.get("GRIPPY_UPLOAD_TTL", "3600"))  # 1 hour
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Ensure upload directory exists
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────
# File Storage
# ──────────────────────────────────────────────────────────────────────

_file_registry: dict[str, dict[str, Any]] = {}


def save_upload(filename: str, content: bytes) -> dict[str, Any]:
    """
    Save an uploaded file and return its metadata.

    Returns:
        dict with: file_id, path, filename, size, mime_type
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {
            "success": False,
            "error": f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        }

    if len(content) > MAX_FILE_SIZE:
        return {
            "success": False,
            "error": f"File too large ({len(content)} bytes). Max: {MAX_FILE_SIZE} bytes",
        }

    file_id = str(uuid.uuid4())
    safe_name = f"{file_id}{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    with open(file_path, "wb") as f:
        f.write(content)

    mime_map = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }

    metadata = {
        "file_id": file_id,
        "path": file_path,
        "filename": filename,
        "size": len(content),
        "mime_type": mime_map.get(ext, "application/octet-stream"),
        "uploaded_at": time.time(),
    }

    _file_registry[file_id] = metadata
    logger.info("File saved: %s (%d bytes)", file_id, len(content))

    return {"success": True, **metadata}


def get_upload(file_id: str) -> Optional[dict[str, Any]]:
    """Get file metadata by ID."""
    meta = _file_registry.get(file_id)
    if not meta:
        return None

    # Check TTL
    if time.time() - meta["uploaded_at"] > UPLOAD_TTL:
        _cleanup_file(file_id)
        return None

    return meta


def _cleanup_file(file_id: str) -> None:
    """Remove an expired file."""
    meta = _file_registry.pop(file_id, None)
    if meta and os.path.exists(meta["path"]):
        try:
            os.remove(meta["path"])
        except OSError:
            pass


def cleanup_expired() -> int:
    """Remove all expired uploads. Returns count of removed files."""
    now = time.time()
    expired = [
        fid for fid, meta in _file_registry.items()
        if now - meta["uploaded_at"] > UPLOAD_TTL
    ]
    for fid in expired:
        _cleanup_file(fid)
    return len(expired)


# ──────────────────────────────────────────────────────────────────────
# Vision LLM Extraction
# ──────────────────────────────────────────────────────────────────────

async def extract_data_from_document(file_id: str) -> dict[str, Any]:
    """
    Use a Vision LLM to extract structured data from an uploaded document.

    Extracts: reference numbers, dates, amounts, company names,
    account numbers, PNRs, flight numbers, etc.

    Returns:
        dict with: success, extracted_data (dict), raw_response
    """
    meta = get_upload(file_id)
    if not meta:
        return {"success": False, "error": "File not found or expired"}

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI()

        # Read and encode the file
        with open(meta["path"], "rb") as f:
            file_bytes = f.read()

        b64_data = base64.b64encode(file_bytes).decode("utf-8")
        mime_type = meta["mime_type"]

        # For PDFs, we need to convert to images first or use text extraction
        if mime_type == "application/pdf":
            return await _extract_from_pdf(meta["path"])

        # Guard: reject images that are too small to contain meaningful data
        from PIL import Image as PILImage
        try:
            img = PILImage.open(meta["path"])
            w, h = img.size
            if w < 50 or h < 50:
                return {
                    "success": False,
                    "error": f"Image too small ({w}x{h}px) to contain readable document data. Please upload a clearer image.",
                }
        except Exception:
            pass  # If PIL can't open it, let the Vision LLM try

        # For images, use Vision LLM directly
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a document data extraction assistant. "
                        "Extract ALL structured data from this document image. "
                        "Return ONLY valid JSON with the following fields "
                        "(include only fields that are present in the document):\n"
                        "- reference_number\n"
                        "- pnr\n"
                        "- booking_id\n"
                        "- order_id\n"
                        "- flight_number\n"
                        "- date (YYYY-MM-DD format)\n"
                        "- amount (numeric, with currency)\n"
                        "- company_name\n"
                        "- customer_name\n"
                        "- email\n"
                        "- phone\n"
                        "- address\n"
                        "- account_number\n"
                        "- description\n"
                        "- any_other_relevant_fields\n\n"
                        "Return ONLY the JSON object, no markdown formatting.\n"
                        "IMPORTANT: If the image is blank, unreadable, or does not contain "
                        "a document, return exactly: {\"error\": \"no_document_found\"}\n"
                        "Do NOT hallucinate or invent data that is not visible in the image."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_data}",
                                "detail": "high",
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract all structured data from this document.",
                        },
                    ],
                },
            ],
            max_tokens=1000,
            temperature=0,
        )

        raw_text = response.choices[0].message.content.strip()

        # Parse the JSON response
        # Handle potential markdown code blocks
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        extracted = json.loads(raw_text)

        # Guard: check if the LLM reported no document found
        if extracted.get("error") == "no_document_found":
            return {
                "success": False,
                "error": "No readable document data found in the image. Please upload a clearer photo of your receipt, ticket, or document.",
            }

        logger.info("Extracted %d fields from document %s", len(extracted), file_id)
        return {
            "success": True,
            "extracted_data": extracted,
            "raw_response": raw_text,
        }

    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON")
        return {
            "success": False,
            "error": "Failed to parse extracted data",
            "raw_response": raw_text if 'raw_text' in dir() else "",
        }
    except Exception as exc:
        logger.error("Vision extraction failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def _extract_from_pdf(pdf_path: str) -> dict[str, Any]:
    """Extract data from a PDF by converting to text first."""
    try:
        import subprocess

        # Use pdftotext (from poppler-utils) to extract text
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return {"success": False, "error": "Failed to extract text from PDF"}

        pdf_text = result.stdout.strip()
        if not pdf_text:
            return {"success": False, "error": "PDF appears to be empty or image-only"}

        # Use LLM to extract structured data from the text
        from openai import AsyncOpenAI

        client = AsyncOpenAI()

        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a document data extraction assistant. "
                        "Extract ALL structured data from this document text. "
                        "Return ONLY valid JSON with relevant fields like: "
                        "reference_number, pnr, booking_id, order_id, "
                        "flight_number, date, amount, company_name, "
                        "customer_name, email, phone, address, account_number, "
                        "description. Include only fields present in the text. "
                        "Return ONLY the JSON object."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Extract structured data from this document:\n\n{pdf_text[:3000]}",
                },
            ],
            max_tokens=1000,
            temperature=0,
        )

        raw_text = response.choices[0].message.content.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        extracted = json.loads(raw_text)
        return {
            "success": True,
            "extracted_data": extracted,
            "raw_response": raw_text,
        }

    except Exception as exc:
        logger.error("PDF extraction failed: %s", exc)
        return {"success": False, "error": str(exc)}
