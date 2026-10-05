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
    source_type TEXT,
    active INTEGER DEFAULT 1,
    last_seen_at TEXT,
    last_successful_crawl_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
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
    storage_backend TEXT DEFAULT 'local',
    storage_key TEXT,
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
    "storage_backend": "TEXT DEFAULT 'local'",
    "storage_key": "TEXT",
}


READING_ROOMS_ADDITIONAL_COLUMNS = {
    "source_type": "TEXT",
    "active": "INTEGER DEFAULT 1",
    "last_seen_at": "TEXT",
    "last_successful_crawl_at": "TEXT",
    "last_error": "TEXT",
    "last_error_at": "TEXT",
}


SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

DOCUMENT_TEXT_TABLE = """
CREATE TABLE IF NOT EXISTS document_text (
    document_id INTEGER PRIMARY KEY,
    body TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    extraction_error TEXT,
    extracted_at TEXT,
    character_count INTEGER DEFAULT 0,
    truncated INTEGER DEFAULT 0,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);
"""

DOCUMENT_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts
USING fts5(body, tokenize='unicode61 remove_diacritics 2');
"""

DOCUMENT_FTS_DELETE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS documents_delete_fts
AFTER DELETE ON documents
BEGIN
    DELETE FROM document_fts WHERE rowid = OLD.id;
END;
"""


DOCUMENT_SOURCES_TABLE = """
CREATE TABLE IF NOT EXISTS document_sources (
    document_id INTEGER NOT NULL,
    reading_room_id INTEGER NOT NULL,
    first_seen_at TEXT,
    last_seen_at TEXT,
    PRIMARY KEY (document_id, reading_room_id),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    FOREIGN KEY (reading_room_id) REFERENCES reading_rooms(id) ON DELETE CASCADE
);
"""

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_documents_agency_id ON documents(agency_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_office_id ON documents(office_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_reading_room_id ON documents(reading_room_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_published_date ON documents(published_date)",
    "CREATE INDEX IF NOT EXISTS idx_documents_file_type ON documents(file_type)",
    "CREATE INDEX IF NOT EXISTS idx_documents_download_status ON documents(download_status)",
    "CREATE INDEX IF NOT EXISTS idx_documents_discovered_at ON documents(discovered_at)",
    "CREATE INDEX IF NOT EXISTS idx_documents_storage_backend ON documents(storage_backend)",
    "CREATE INDEX IF NOT EXISTS idx_documents_storage_key ON documents(storage_key)",
    "CREATE INDEX IF NOT EXISTS idx_reading_rooms_active ON reading_rooms(active)",
    "CREATE INDEX IF NOT EXISTS idx_reading_rooms_agency_id ON reading_rooms(agency_id)",
    "CREATE INDEX IF NOT EXISTS idx_reading_rooms_office_id ON reading_rooms(office_id)",
    "CREATE INDEX IF NOT EXISTS idx_reading_rooms_last_seen_at ON reading_rooms(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_document_sources_reading_room_id ON document_sources(reading_room_id, document_id)",
)
