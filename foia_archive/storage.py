"""Storage helpers for FOIA archive."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import models


def ensure_dirs(db_path: Path, files_dir: Path) -> None:
    files_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)


DB_BUSY_TIMEOUT_MS = 5000


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: Dict[str, str],
) -> None:
    existing = _table_columns(conn, table)
    for column, definition in columns.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


def _migration_1_legacy_metadata_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "reading_rooms",
        models.READING_ROOMS_ADDITIONAL_COLUMNS,
    )
    conn.execute(
        """
        UPDATE reading_rooms
        SET active = 1
        WHERE active IS NULL
        """
    )

    _ensure_columns(
        conn,
        "documents",
        models.DOCUMENTS_ADDITIONAL_COLUMNS,
    )
    document_columns = _table_columns(conn, "documents")
    if {"local_path", "download_status"}.issubset(document_columns):
        conn.execute(
            """
            UPDATE documents
            SET download_status = 'downloaded'
            WHERE local_path IS NOT NULL
              AND local_path != ''
              AND (download_status IS NULL OR download_status IN ('', 'pending'))
            """
        )
        conn.execute(
            """
            UPDATE documents
            SET download_status = 'pending'
            WHERE (local_path IS NULL OR local_path = '')
              AND (download_status IS NULL OR download_status = '')
            """
        )


def _migration_2_document_sources(conn: sqlite3.Connection) -> None:
    conn.execute(models.DOCUMENT_SOURCES_TABLE)
    document_columns = _table_columns(conn, "documents")
    if {"id", "reading_room_id", "discovered_at"}.issubset(document_columns):
        conn.execute(
            """
            INSERT OR IGNORE INTO document_sources (
                document_id,
                reading_room_id,
                first_seen_at,
                last_seen_at
            )
            SELECT
                d.id,
                d.reading_room_id,
                d.discovered_at,
                d.discovered_at
            FROM documents d
            JOIN reading_rooms rr ON rr.id = d.reading_room_id
            WHERE d.reading_room_id IS NOT NULL
            """
        )


def _migration_3_indexes(conn: sqlite3.Connection) -> None:
    for statement in models.INDEX_STATEMENTS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            # Synthetic/very old partial schemas may lack a historical column.
            # Normal production schemas have all indexed columns; skip only a
            # missing-column index so the rest of the database can upgrade.
            if "no such column" not in str(exc).lower():
                raise


def _migration_4_archive_storage(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "documents",
        {
            "storage_backend": "TEXT DEFAULT 'local'",
            "storage_key": "TEXT",
        },
    )
    conn.execute(
        """
        UPDATE documents
        SET storage_backend = 'local'
        WHERE storage_backend IS NULL OR storage_backend = ''
        """
    )
    conn.execute(
        """
        UPDATE documents
        SET storage_key = local_path
        WHERE (storage_key IS NULL OR storage_key = '')
          AND local_path IS NOT NULL
          AND local_path != ''
        """
    )
    for statement in models.INDEX_STATEMENTS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc).lower():
                raise


def _migration_5_full_text_search(conn: sqlite3.Connection) -> None:
    conn.execute(models.DOCUMENT_TEXT_TABLE)
    conn.execute(models.DOCUMENT_FTS_TABLE)
    conn.execute(models.DOCUMENT_FTS_DELETE_TRIGGER)


def _migration_6_extraction_method(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "document_text",
        {"extraction_method": "TEXT"},
    )
    conn.execute(
        """
        UPDATE document_text
        SET extraction_method = 'native_text'
        WHERE extraction_method IS NULL
          AND extraction_status IN ('indexed', 'indexed_truncated')
        """
    )


MIGRATIONS: Tuple[
    Tuple[int, str, Callable[[sqlite3.Connection], None]],
    ...,
] = (
    (1, "legacy metadata columns", _migration_1_legacy_metadata_columns),
    (2, "document source relationships", _migration_2_document_sources),
    (3, "archive query indexes", _migration_3_indexes),
    (4, "archive storage backends", _migration_4_archive_storage),
    (5, "document full text search", _migration_5_full_text_search),
    (6, "text extraction method", _migration_6_extraction_method),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(models.SCHEMA_MIGRATIONS_TABLE)
    applied = {
        row["version"]
        for row in conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }

    for version, name, migration in MIGRATIONS:
        if version in applied:
            continue
        with conn:
            migration(conn)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    version,
                    name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


def get_schema_version(conn: sqlite3.Connection) -> int:
    conn.execute(models.SCHEMA_MIGRATIONS_TABLE)
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"])


def init_db(db_path: Path | str, files_dir: Path | str) -> None:
    db_path = Path(db_path)
    files_dir = Path(files_dir)
    ensure_dirs(db_path, files_dir)
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(models.AGENCIES_TABLE)
        conn.execute(models.OFFICES_TABLE)
        conn.execute(models.READING_ROOMS_TABLE)
        conn.execute(models.DOCUMENTS_TABLE)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def upsert_agency(conn: sqlite3.Connection, slug: str, name: str, raw_json: Dict[str, Any]) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agencies (slug, name, raw_json)
        VALUES (?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            raw_json = excluded.raw_json
        """,
        (slug, name, json.dumps(raw_json)),
    )
    conn.commit()
    cur.execute("SELECT id FROM agencies WHERE slug = ?", (slug,))
    return cur.fetchone()[0]


def upsert_office(
    conn: sqlite3.Connection,
    slug: str,
    name: str,
    agency_id: int,
    raw_json: Dict[str, Any],
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO offices (slug, name, agency_id, raw_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            agency_id = excluded.agency_id,
            raw_json = excluded.raw_json
        """,
        (slug, name, agency_id, json.dumps(raw_json)),
    )
    conn.commit()
    cur.execute("SELECT id FROM offices WHERE slug = ?", (slug,))
    return cur.fetchone()[0]


def upsert_reading_room(
    conn: sqlite3.Connection,
    url: str,
    label: str,
    level: str,
    agency_id: Optional[int],
    office_id: Optional[int],
    source_type: Optional[str] = None,
    seen_at: Optional[str] = None,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reading_rooms (
            url, label, level, agency_id, office_id,
            source_type, active, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(url) DO UPDATE SET
            label = excluded.label,
            level = excluded.level,
            agency_id = excluded.agency_id,
            office_id = excluded.office_id,
            source_type = COALESCE(excluded.source_type, reading_rooms.source_type),
            active = 1,
            last_seen_at = COALESCE(excluded.last_seen_at, reading_rooms.last_seen_at)
        """,
        (url, label, level, agency_id, office_id, source_type, seen_at),
    )
    conn.commit()
    cur.execute("SELECT id FROM reading_rooms WHERE url = ?", (url,))
    return cur.fetchone()[0]


def deactivate_reading_rooms_not_seen(
    conn: sqlite3.Connection,
    seen_at: str,
) -> int:
    cur = conn.execute(
        """
        UPDATE reading_rooms
        SET active = 0
        WHERE last_seen_at IS NULL OR last_seen_at != ?
        """,
        (seen_at,),
    )
    conn.commit()
    return cur.rowcount


def list_reading_rooms(
    conn: sqlite3.Connection,
    limit: Optional[int] = None,
    active_only: bool = True,
) -> List[sqlite3.Row]:
    query = "SELECT * FROM reading_rooms"
    params: List[Any] = []
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(query, params)
    return cur.fetchall()


def get_document_by_url(conn: sqlite3.Connection, url: str) -> Optional[sqlite3.Row]:
    """Return the stored document row for a URL, if one exists."""
    return conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()


def document_exists(conn: sqlite3.Connection, url: str) -> bool:
    return get_document_by_url(conn, url) is not None


def _associate_document_source_no_commit(
    conn: sqlite3.Connection,
    document_id: int,
    reading_room_id: int,
    seen_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO document_sources (
            document_id,
            reading_room_id,
            first_seen_at,
            last_seen_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(document_id, reading_room_id) DO UPDATE SET
            last_seen_at = excluded.last_seen_at
        """,
        (document_id, reading_room_id, seen_at, seen_at),
    )


def associate_document_source(
    conn: sqlite3.Connection,
    document_id: int,
    reading_room_id: Optional[int],
    seen_at: str,
) -> None:
    if reading_room_id is None:
        return
    _associate_document_source_no_commit(
        conn,
        document_id,
        reading_room_id,
        seen_at,
    )
    conn.commit()


def insert_document(
    conn: sqlite3.Connection,
    url: str,
    title: str,
    file_type: str,
    filename: str,
    agency_id: Optional[int],
    office_id: Optional[int],
    reading_room_id: Optional[int],
    discovered_at: str,
    published_date: Optional[str] = None,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO documents (
            url, title, file_type, filename, agency_id, office_id, reading_room_id,
            discovered_at, published_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url,
            title,
            file_type,
            filename,
            agency_id,
            office_id,
            reading_room_id,
            discovered_at,
            published_date,
        ),
    )
    document_id = cur.lastrowid
    if reading_room_id is not None:
        _associate_document_source_no_commit(
            conn,
            document_id,
            reading_room_id,
            discovered_at,
        )
    conn.commit()
    return document_id



def update_document_published_date_if_missing(
    conn: sqlite3.Connection,
    url: str,
    published_date: Optional[str],
) -> bool:
    """Fill an unknown publication date without replacing existing metadata."""
    if not published_date:
        return False
    cur = conn.execute(
        """
        UPDATE documents
        SET published_date = ?
        WHERE url = ? AND (published_date IS NULL OR published_date = '')
        """,
        (published_date, url),
    )
    conn.commit()
    return cur.rowcount > 0


SORT_ORDERS = {
    "discovered_desc": "d.discovered_at DESC, d.id DESC",
    "discovered_asc": "d.discovered_at ASC, d.id ASC",
    "published_desc": (
        "CASE WHEN d.published_date IS NULL OR d.published_date = '' THEN 1 ELSE 0 END ASC, "
        "d.published_date DESC, d.id DESC"
    ),
    "published_asc": (
        "CASE WHEN d.published_date IS NULL OR d.published_date = '' THEN 1 ELSE 0 END ASC, "
        "d.published_date ASC, d.id ASC"
    ),
    "title_asc": "LOWER(COALESCE(d.title, d.filename, '')) ASC, d.id ASC",
    "title_desc": "LOWER(COALESCE(d.title, d.filename, '')) DESC, d.id DESC",
}


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _fts5_query(value: str) -> Optional[str]:
    """Convert plain user text into a safe FTS5 AND query.

    UI search is intentionally not an FTS query-language surface. Treat words
    as literals so punctuation or quotes cannot produce MATCH syntax errors.
    """
    tokens = re.findall(r"\w+", value, flags=re.UNICODE)
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _document_filter_sql(
    agency_id: Optional[int] = None,
    office_id: Optional[int] = None,
    file_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    title_query: Optional[str] = None,
) -> tuple[List[str], List[Any]]:
    clauses = ["1=1"]
    params: List[Any] = []

    if agency_id:
        clauses.append(
            """
            (
                d.agency_id = ?
                OR EXISTS (
                    SELECT 1
                    FROM document_sources ds
                    JOIN reading_rooms rr ON rr.id = ds.reading_room_id
                    WHERE ds.document_id = d.id
                      AND rr.agency_id = ?
                )
            )
            """
        )
        params.extend([agency_id, agency_id])
    if office_id:
        clauses.append(
            """
            (
                d.office_id = ?
                OR EXISTS (
                    SELECT 1
                    FROM document_sources ds
                    JOIN reading_rooms rr ON rr.id = ds.reading_room_id
                    WHERE ds.document_id = d.id
                      AND rr.office_id = ?
                )
            )
            """
        )
        params.extend([office_id, office_id])
    if file_type:
        clauses.append("d.file_type = ?")
        params.append(file_type)
    if start_date or end_date:
        clauses.append("d.published_date IS NOT NULL AND d.published_date != ''")
    if start_date:
        clauses.append("d.published_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("d.published_date <= ?")
        params.append(end_date)
    if title_query:
        raw_query = title_query.strip()
        escaped = _escape_like(raw_query)
        if escaped:
            pattern = f"%{escaped}%"
            fts_query = _fts5_query(raw_query)
            if fts_query:
                clauses.append(
                    """
                    (
                        COALESCE(d.title, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(d.filename, '') LIKE ? ESCAPE '\\'
                        OR d.id IN (
                            SELECT rowid
                            FROM document_fts
                            WHERE document_fts MATCH ?
                        )
                    )
                    """
                )
                params.extend([pattern, pattern, fts_query])
            else:
                clauses.append(
                    """
                    (
                        COALESCE(d.title, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(d.filename, '') LIKE ? ESCAPE '\\'
                    )
                    """
                )
                params.extend([pattern, pattern])

    return clauses, params


def count_documents(
    conn: sqlite3.Connection,
    agency_id: Optional[int] = None,
    office_id: Optional[int] = None,
    file_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    title_query: Optional[str] = None,
) -> int:
    clauses, params = _document_filter_sql(
        agency_id=agency_id,
        office_id=office_id,
        file_type=file_type,
        start_date=start_date,
        end_date=end_date,
        title_query=title_query,
    )
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM documents d WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()
    return int(row["total"])


def query_documents(
    conn: sqlite3.Connection,
    agency_id: Optional[int] = None,
    office_id: Optional[int] = None,
    file_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    title_query: Optional[str] = None,
    sort: str = "discovered_desc",
    limit: Optional[int] = 200,
    offset: int = 0,
) -> List[sqlite3.Row]:
    """Return documents matching UI filters.

    Unknown publication dates remain visible when no publication-date bound is
    supplied. If either date bound is supplied, unknown dates are intentionally
    excluded because they cannot be known to satisfy the requested interval.
    """
    clauses, params = _document_filter_sql(
        agency_id=agency_id,
        office_id=office_id,
        file_type=file_type,
        start_date=start_date,
        end_date=end_date,
        title_query=title_query,
    )
    order_by = SORT_ORDERS.get(sort, SORT_ORDERS["discovered_desc"])

    query = [
        "SELECT d.id, d.title, d.filename, d.file_type, d.published_date,",
        "       d.discovered_at, d.local_path, d.storage_backend, d.storage_key,",
        "       d.url, d.download_status, d.download_error, d.file_size,",
        "       a.name AS agency_name, o.name AS office_name",
        "FROM documents d",
        "LEFT JOIN agencies a ON d.agency_id = a.id",
        "LEFT JOIN offices o ON d.office_id = o.id",
        "WHERE " + " AND ".join(clauses),
        f"ORDER BY {order_by}",
    ]
    if limit is not None:
        query.append("LIMIT ? OFFSET ?")
        params.extend([max(0, int(limit)), max(0, int(offset))])

    return conn.execute("\n".join(query), params).fetchall()


def query_documents_page(
    conn: sqlite3.Connection,
    agency_id: Optional[int] = None,
    office_id: Optional[int] = None,
    file_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    title_query: Optional[str] = None,
    sort: str = "discovered_desc",
    page: int = 1,
    page_size: int = 50,
) -> tuple[List[sqlite3.Row], int]:
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    total = count_documents(
        conn,
        agency_id=agency_id,
        office_id=office_id,
        file_type=file_type,
        start_date=start_date,
        end_date=end_date,
        title_query=title_query,
    )
    rows = query_documents(
        conn,
        agency_id=agency_id,
        office_id=office_id,
        file_type=file_type,
        start_date=start_date,
        end_date=end_date,
        title_query=title_query,
        sort=sort,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return rows, total


def upsert_document_text(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    body: str,
    extraction_status: str,
    extracted_at: str,
    extraction_error: Optional[str] = None,
    extraction_method: Optional[str] = None,
    character_count: Optional[int] = None,
    truncated: bool = False,
) -> None:
    """Persist extraction state and refresh the FTS5 row atomically."""
    if character_count is None:
        character_count = len(body or "")

    with conn:
        conn.execute(
            """
            INSERT INTO document_text (
                document_id,
                body,
                extraction_status,
                extraction_error,
                extraction_method,
                extracted_at,
                character_count,
                truncated
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                body = excluded.body,
                extraction_status = excluded.extraction_status,
                extraction_error = excluded.extraction_error,
                extraction_method = excluded.extraction_method,
                extracted_at = excluded.extracted_at,
                character_count = excluded.character_count,
                truncated = excluded.truncated
            """,
            (
                document_id,
                body,
                extraction_status,
                extraction_error,
                extraction_method,
                extracted_at,
                int(character_count),
                1 if truncated else 0,
            ),
        )
        conn.execute("DELETE FROM document_fts WHERE rowid = ?", (document_id,))
        if body and extraction_status in {"indexed", "indexed_truncated"}:
            conn.execute(
                "INSERT INTO document_fts(rowid, body) VALUES (?, ?)",
                (document_id, body),
            )


def get_document_text(
    conn: sqlite3.Connection,
    document_id: int,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM document_text WHERE document_id = ?",
        (document_id,),
    ).fetchone()


def update_download_metadata(
    conn: sqlite3.Connection,
    document_id: int,
    local_path: Optional[str],
    downloaded_at: str,
    mime_type: Optional[str] = None,
    file_size: Optional[int] = None,
    sha256: Optional[str] = None,
    storage_backend: str = "local",
    storage_key: Optional[str] = None,
):
    conn.execute(
        """
        UPDATE documents
        SET local_path = ?,
            downloaded_at = ?,
            mime_type = ?,
            file_size = ?,
            sha256 = ?,
            storage_backend = ?,
            storage_key = ?,
            download_status = 'downloaded',
            download_error = NULL,
            last_download_attempt_at = ?
        WHERE id = ?
        """,
        (
            local_path,
            downloaded_at,
            mime_type,
            file_size,
            sha256,
            storage_backend,
            storage_key if storage_key is not None else local_path,
            downloaded_at,
            document_id,
        ),
    )
    conn.commit()


def update_download_failure(
    conn: sqlite3.Connection,
    document_id: int,
    status: str,
    error: str,
    attempted_at: str,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE documents
            SET local_path = NULL,
                storage_backend = NULL,
                storage_key = NULL,
                downloaded_at = NULL,
                mime_type = NULL,
                file_size = NULL,
                sha256 = NULL,
                download_status = ?,
                download_error = ?,
                last_download_attempt_at = ?
            WHERE id = ?
            """,
            (status, error, attempted_at, document_id),
        )
        conn.execute("DELETE FROM document_fts WHERE rowid = ?", (document_id,))
        conn.execute(
            "DELETE FROM document_text WHERE document_id = ?",
            (document_id,),
        )


def archived_remote_bytes(conn: sqlite3.Connection, backend: str) -> int:
    """Return unique archived bytes for a remote backend.

    Content-addressed objects can be referenced by more than one document row,
    so count each storage key once.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(file_size), 0) AS total
        FROM (
            SELECT storage_key, MAX(COALESCE(file_size, 0)) AS file_size
            FROM documents
            WHERE storage_backend = ?
              AND storage_key IS NOT NULL
              AND storage_key != ''
              AND download_status = 'downloaded'
            GROUP BY storage_key
        )
        """,
        (backend,),
    ).fetchone()
    return int(row["total"] or 0)


def record_reading_room_crawl_success(
    conn: sqlite3.Connection,
    rr_id: int,
    timestamp: str,
) -> None:
    conn.execute(
        """
        UPDATE reading_rooms
        SET last_crawled_at = ?,
            last_successful_crawl_at = ?,
            last_error = NULL,
            last_error_at = NULL
        WHERE id = ?
        """,
        (timestamp, timestamp, rr_id),
    )
    conn.commit()


def record_reading_room_crawl_failure(
    conn: sqlite3.Connection,
    rr_id: int,
    timestamp: str,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE reading_rooms
        SET last_crawled_at = ?,
            last_error = ?,
            last_error_at = ?
        WHERE id = ?
        """,
        (timestamp, error, timestamp, rr_id),
    )
    conn.commit()


def update_reading_room_crawled(conn: sqlite3.Connection, rr_id: int, timestamp: str) -> None:
    """Backward-compatible alias for a successful crawl."""
    record_reading_room_crawl_success(conn, rr_id, timestamp)
