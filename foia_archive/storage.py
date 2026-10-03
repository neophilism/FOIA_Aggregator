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
    conn.commit()
    conn.close()


def upsert_agency(conn: sqlite3.Connection, slug: str, name: str, raw_json: Dict[str, Any]) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO agencies (slug, name, raw_json) VALUES (?, ?, ?)",
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
        "INSERT OR IGNORE INTO offices (slug, name, agency_id, raw_json) VALUES (?, ?, ?, ?)",
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
) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO reading_rooms (url, label, level, agency_id, office_id) VALUES (?, ?, ?, ?, ?)",
        (url, label, level, agency_id, office_id),
    )
    conn.commit()
    cur.execute("SELECT id FROM reading_rooms WHERE url = ?", (url,))
    return cur.fetchone()[0]


def list_reading_rooms(conn: sqlite3.Connection, limit: Optional[int] = None) -> List[sqlite3.Row]:
    query = "SELECT * FROM reading_rooms ORDER BY id"
    params: Iterable[Any] = []
    if limit:
        query += " LIMIT ?"
        params = [limit]
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
):
    conn.execute(
        "UPDATE documents SET local_path = ?, downloaded_at = ? WHERE id = ?",
        (local_path, downloaded_at, document_id),
    )
    conn.commit()


def update_reading_room_crawled(conn: sqlite3.Connection, rr_id: int, timestamp: str) -> None:
    conn.execute(
        "UPDATE reading_rooms SET last_crawled_at = ? WHERE id = ?",
        (timestamp, rr_id),
    )
    conn.commit()
