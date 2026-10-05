"""Text extraction for archived FOIA documents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from docx import Document
from pypdf import PdfReader


TEXT_LIKE_TYPES = {
    "txt",
    "csv",
    "json",
    "xml",
    "rtf",
    "eml",
}


@dataclass(frozen=True)
class TextExtractionResult:
    status: str
    text: str = ""
    error: Optional[str] = None
    character_count: int = 0
    truncated: bool = False


def _cap_text(text: str, max_chars: int) -> TextExtractionResult:
    normalized = text.replace("\x00", " ").strip()
    if not normalized:
        return TextExtractionResult(
            status="empty",
            error="No extractable text; OCR or another extractor may be required.",
        )

    truncated = len(normalized) > max_chars
    indexed = normalized[:max_chars]
    return TextExtractionResult(
        status="indexed_truncated" if truncated else "indexed",
        text=indexed,
        character_count=len(indexed),
        truncated=truncated,
    )


def _extract_pdf(path: Path, max_chars: int) -> TextExtractionResult:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return TextExtractionResult(
                status="extraction_failed",
                error="Encrypted PDF could not be opened without a password.",
            )

    parts: list[str] = []
    current_chars = 0
    truncated = False

    for page in reader.pages:
        text = page.extract_text() or ""
        if not text:
            continue
        parts.append(text)
        current_chars += len(text) + 1
        if current_chars > max_chars:
            truncated = True
            break

    result = _cap_text("\n".join(parts), max_chars)
    if truncated and result.status == "indexed":
        return TextExtractionResult(
            status="indexed_truncated",
            text=result.text,
            character_count=result.character_count,
            truncated=True,
        )
    return result


def _extract_docx(path: Path, max_chars: int) -> TextExtractionResult:
    document = Document(str(path))
    parts: list[str] = []

    for paragraph in document.paragraphs:
        if paragraph.text:
            parts.append(paragraph.text)

    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))

    return _cap_text("\n".join(parts), max_chars)


def _extract_text_like(path: Path, max_chars: int) -> TextExtractionResult:
    # Read a bounded byte window so indexing cannot create an unbounded memory
    # spike even when the archive download ceiling is much larger.
    byte_limit = max(4096, max_chars * 4)
    with path.open("rb") as source:
        raw = source.read(byte_limit + 1)

    bytes_truncated = len(raw) > byte_limit
    raw = raw[:byte_limit]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    result = _cap_text(text, max_chars)
    if bytes_truncated and result.status == "indexed":
        return TextExtractionResult(
            status="indexed_truncated",
            text=result.text,
            character_count=result.character_count,
            truncated=True,
        )
    return result


def extract_document_text(
    path: Path | str,
    file_type: Optional[str],
    *,
    max_chars: int = 5_000_000,
) -> TextExtractionResult:
    """Extract searchable text without affecting archive success."""
    source = Path(path)
    kind = (file_type or source.suffix.lstrip(".")).strip().lower()
    max_chars = max(1_000, int(max_chars))

    try:
        if kind == "pdf":
            return _extract_pdf(source, max_chars)
        if kind == "docx":
            return _extract_docx(source, max_chars)
        if kind in TEXT_LIKE_TYPES:
            return _extract_text_like(source, max_chars)
        return TextExtractionResult(
            status="unsupported",
            error=f"No text extractor is configured for file type: {kind or 'unknown'}",
        )
    except Exception as exc:
        return TextExtractionResult(
            status="extraction_failed",
            error=f"{type(exc).__name__}: {exc}"[:2000],
        )
