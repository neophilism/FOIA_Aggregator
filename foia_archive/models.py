"""Database schema definitions for FOIA archive."""

AGENCIES_TABLE = """
CREATE TABLE IF NOT EXISTS agencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE,
    name TEXT,
    raw_json TEXT
);
"""

OFFICES_TABLE = """
CREATE TABLE IF NOT EXISTS offices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE,
    name TEXT,
    agency_id INTEGER,
    raw_json TEXT,
    FOREIGN KEY (agency_id) REFERENCES agencies(id)
);
"""

READING_ROOMS_TABLE = """
CREATE TABLE IF NOT EXISTS reading_rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    label TEXT,
    level TEXT,
    agency_id INTEGER,
    office_id INTEGER,
    last_crawled_at TEXT,
    FOREIGN KEY (agency_id) REFERENCES agencies(id),
    FOREIGN KEY (office_id) REFERENCES offices(id)
);
"""

DOCUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    local_path TEXT,
    filename TEXT,
    file_type TEXT,
    title TEXT,
    description TEXT,
    agency_id INTEGER,
    office_id INTEGER,
    reading_room_id INTEGER,
    published_date TEXT,
    discovered_at TEXT,
    downloaded_at TEXT,
    mime_type TEXT,
    file_size INTEGER,
    sha256 TEXT,
    download_status TEXT DEFAULT 'pending',
    download_error TEXT,
    last_download_attempt_at TEXT,
    FOREIGN KEY (agency_id) REFERENCES agencies(id),
    FOREIGN KEY (office_id) REFERENCES offices(id),
    FOREIGN KEY (reading_room_id) REFERENCES reading_rooms(id)
);
"""


DOCUMENTS_ADDITIONAL_COLUMNS = {
    "mime_type": "TEXT",
    "file_size": "INTEGER",
    "sha256": "TEXT",
    "download_status": "TEXT DEFAULT 'pending'",
    "download_error": "TEXT",
    "last_download_attempt_at": "TEXT",
}
