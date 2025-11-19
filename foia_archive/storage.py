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


def document_exists(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.execute("SELECT 1 FROM documents WHERE url = ?", (url,))
    return cur.fetchone() is not None


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
