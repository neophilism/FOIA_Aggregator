"""Storage helpers for FOIA archive."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import models


def ensure_dirs(db_path: Path, files_dir: Path) -> None:
    files_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str, files_dir: Path | str) -> None:
    db_path = Path(db_path)
    files_dir = Path(files_dir)
    ensure_dirs(db_path, files_dir)
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(models.AGENCIES_TABLE)
    cur.execute(models.OFFICES_TABLE)
    cur.execute(models.READING_ROOMS_TABLE)
    cur.execute(models.DOCUMENTS_TABLE)

    reading_room_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(reading_rooms)").fetchall()
    }
    for column, definition in models.READING_ROOMS_ADDITIONAL_COLUMNS.items():
        if column not in reading_room_columns:
            conn.execute(f"ALTER TABLE reading_rooms ADD COLUMN {column} {definition}")
    conn.execute(
        """
        UPDATE reading_rooms
        SET active = 1
        WHERE active IS NULL
        """
    )

    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
    }
    for column, definition in models.DOCUMENTS_ADDITIONAL_COLUMNS.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
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
    conn.commit()
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
    conn.commit()
    return cur.lastrowid



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


def query_documents(
    conn: sqlite3.Connection,
    agency_id: Optional[int] = None,
    office_id: Optional[int] = None,
    file_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Return documents matching UI filters.

    Unknown publication dates remain visible when no publication-date bound is
    supplied. If either date bound is supplied, unknown dates are intentionally
    excluded because they cannot be known to satisfy the requested interval.
    """
    query = [
        "SELECT d.id, d.title, d.file_type, d.published_date, d.discovered_at, d.local_path, d.url,",
        "       a.name AS agency_name, o.name AS office_name",
        "FROM documents d",
        "LEFT JOIN agencies a ON d.agency_id = a.id",
        "LEFT JOIN offices o ON d.office_id = o.id",
        "WHERE 1=1",
    ]
    params: List[Any] = []

    if agency_id:
        query.append("AND d.agency_id = ?")
        params.append(agency_id)
    if office_id:
        query.append("AND d.office_id = ?")
        params.append(office_id)
    if file_type:
        query.append("AND d.file_type = ?")
        params.append(file_type)
    if start_date or end_date:
        query.append("AND d.published_date IS NOT NULL AND d.published_date != ''")
    if start_date:
        query.append("AND d.published_date >= ?")
        params.append(start_date)
    if end_date:
        query.append("AND d.published_date <= ?")
        params.append(end_date)

    query.append("ORDER BY d.discovered_at DESC LIMIT 200")
    return conn.execute("\n".join(query), params).fetchall()

def update_download_metadata(
    conn: sqlite3.Connection,
    document_id: int,
    local_path: str,
    downloaded_at: str,
    mime_type: Optional[str] = None,
    file_size: Optional[int] = None,
    sha256: Optional[str] = None,
):
    conn.execute(
        """
        UPDATE documents
        SET local_path = ?,
            downloaded_at = ?,
            mime_type = ?,
            file_size = ?,
            sha256 = ?,
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
    conn.execute(
        """
        UPDATE documents
        SET local_path = NULL,
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
    conn.commit()


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
