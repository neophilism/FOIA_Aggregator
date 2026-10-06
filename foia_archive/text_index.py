"""Backfill searchable text for already archived FOIA documents."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .archive_storage import (
    ArchiveStorageError,
    B2ArchiveStorage,
    LocalArchiveStorage,
    get_archive_storage,
)
from .storage import get_connection, init_db, upsert_document_text
from .text_extraction import OCRSettings, extract_document_text
from .utils import Config, logger


@dataclass(frozen=True)
class ReindexSummary:
    attempted: int = 0
    indexed: int = 0
    empty: int = 0
    unsupported: int = 0
    failed: int = 0


def _materialize_for_indexing(
    config: Config,
    row,
    temp_root: Path,
) -> tuple[Path, bool]:
    backend_name = row["storage_backend"] or (
        "local" if row["local_path"] else None
    )
    key = row["storage_key"] or row["local_path"]
    if not backend_name or not key:
        raise ArchiveStorageError("Archived document has no storage location")

    backend = get_archive_storage(config, backend_name=backend_name)
    if isinstance(backend, LocalArchiveStorage):
        path = backend.path_for_key(key)
        if not path.is_file():
            raise ArchiveStorageError(f"Local archive object is missing: {key}")
        return path, False

    if isinstance(backend, B2ArchiveStorage):
        suffix = Path(row["filename"] or key).suffix
        destination = temp_root / f"document-{row['id']}{suffix}"
        backend.client.download_file(
            backend.bucket,
            key,
            str(destination),
        )
        return destination, True

    raise ArchiveStorageError(
        f"Unsupported archive backend for reindexing: {backend_name}"
    )


def reindex_downloaded_documents(
    config: Config,
    *,
    limit: Optional[int] = None,
    force: bool = False,
) -> ReindexSummary:
    """Extract/index archived documents without re-crawling agency sources."""
    db_path = Path(config.storage.get("db_path", "data/foia_archive.db"))
    files_dir = Path(config.storage.get("files_dir", "data/files"))
    init_db(db_path, files_dir)

    search_config = config.data.get("search") or {}
    try:
        max_chars = int(
            search_config.get("max_indexed_chars_per_document", 5_000_000)
        )
    except (TypeError, ValueError):
        max_chars = 5_000_000
    max_chars = max(1_000, max_chars)

    conn = get_connection(db_path)
    try:
        query = [
            """
            SELECT
                d.id,
                d.filename,
                d.file_type,
                d.local_path,
                d.storage_backend,
                d.storage_key,
                dt.extraction_status
            FROM documents d
            LEFT JOIN document_text dt ON dt.document_id = d.id
            WHERE d.download_status = 'downloaded'
              AND COALESCE(d.storage_key, d.local_path) IS NOT NULL
            """
        ]
        params: list[object] = []
        if not force:
            query.append(
                """
                AND (
                    dt.document_id IS NULL
                    OR dt.extraction_status IN ('ocr_unavailable', 'ocr_failed')
                    OR (
                        dt.extraction_method IS NULL
                        AND (
                            dt.extraction_status = 'empty'
                            OR (
                                dt.extraction_status = 'unsupported'
                                AND LOWER(COALESCE(d.file_type, '')) IN (
                                    'png', 'jpg', 'jpeg', 'tif', 'tiff',
                                    'doc', 'xls', 'ppt'
                                )
                            )
                        )
                    )
                )
                """
            )
        query.append("ORDER BY d.id")
        if limit is not None:
            query.append("LIMIT ?")
            params.append(max(0, int(limit)))
        rows = conn.execute("\n".join(query), params).fetchall()

        attempted = indexed = empty = unsupported = failed = 0
        staging_root = files_dir
        staging_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix="foia-text-index-",
            dir=str(staging_root),
        ) as tempdir:
            temp_root = Path(tempdir)
            for row in rows:
                attempted += 1
                temporary = False
                try:
                    source, temporary = _materialize_for_indexing(
                        config,
                        row,
                        temp_root,
                    )
                    extraction = extract_document_text(
                        source,
                        row["file_type"],
                        max_chars=max_chars,
                        ocr=OCRSettings.from_mapping(config.data.get("ocr")),
                        legacy_office_timeout_seconds=float(
                            (config.data.get("search") or {}).get(
                                "legacy_office_timeout_seconds",
                                30,
                            )
                        ),
                    )
                    now = datetime.now(timezone.utc).isoformat()
                    upsert_document_text(
                        conn,
                        row["id"],
                        body=extraction.text,
                        extraction_status=extraction.status,
                        extraction_error=extraction.error,
                        extraction_method=extraction.method,
                        extracted_at=now,
                        character_count=extraction.character_count,
                        truncated=extraction.truncated,
                    )

                    if extraction.status in {"indexed", "indexed_truncated"}:
                        indexed += 1
                    elif extraction.status == "empty":
                        empty += 1
                    elif extraction.status == "unsupported":
                        unsupported += 1
                    else:
                        failed += 1
                except Exception as exc:
                    failed += 1
                    upsert_document_text(
                        conn,
                        row["id"],
                        body="",
                        extraction_status="extraction_failed",
                        extraction_error=f"{type(exc).__name__}: {exc}"[:2000],
                        extracted_at=datetime.now(timezone.utc).isoformat(),
                    )
                    logger.warning(
                        "Could not index archived document %s: %s",
                        row["id"],
                        exc,
                    )
                finally:
                    if temporary:
                        try:
                            source.unlink()
                        except (FileNotFoundError, UnboundLocalError):
                            pass

        return ReindexSummary(
            attempted=attempted,
            indexed=indexed,
            empty=empty,
            unsupported=unsupported,
            failed=failed,
        )
    finally:
        conn.close()
