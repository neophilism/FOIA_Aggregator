import sqlite3
import tempfile
import unittest
from pathlib import Path

from foia_archive import models
from foia_archive.storage import (
    associate_document_source,
    get_connection,
    get_schema_version,
    init_db,
    insert_document,
    query_documents,
    upsert_agency,
    upsert_office,
    upsert_reading_room,
)


class DatabaseDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_connections_enable_foreign_keys_busy_timeout_and_wal(self):
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        try:
            foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(foreign_keys, 1)
        self.assertGreaterEqual(busy_timeout, 5000)
        self.assertEqual(str(journal_mode).lower(), "wal")

    def test_migrations_are_versioned_and_idempotent(self):
        init_db(self.db_path, self.files_dir)
        init_db(self.db_path, self.files_dir)

        conn = get_connection(self.db_path)
        try:
            rows = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            version = get_schema_version(conn)
        finally:
            conn.close()

        self.assertEqual([row["version"] for row in rows], [1, 2, 3])
        self.assertEqual(version, 3)

    def test_expected_query_indexes_are_created(self):
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        try:
            indexes = {
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND name LIKE 'idx_%'
                    """
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertTrue(
            {
                "idx_documents_agency_id",
                "idx_documents_office_id",
                "idx_documents_reading_room_id",
                "idx_documents_published_date",
                "idx_documents_file_type",
                "idx_documents_download_status",
                "idx_documents_discovered_at",
                "idx_reading_rooms_active",
                "idx_reading_rooms_agency_id",
                "idx_reading_rooms_office_id",
                "idx_reading_rooms_last_seen_at",
                "idx_document_sources_reading_room_id",
            }.issubset(indexes)
        )

    def test_existing_document_source_is_backfilled_during_upgrade(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            models.AGENCIES_TABLE
            + models.OFFICES_TABLE
            + models.READING_ROOMS_TABLE
            + models.DOCUMENTS_TABLE
        )
        conn.execute(
            """
            INSERT INTO reading_rooms (id, url, label, active)
            VALUES (1, 'https://example.gov/library', 'Library', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO documents (
                id, url, title, reading_room_id, discovered_at
            )
            VALUES (
                10,
                'https://example.gov/report.pdf',
                'Report',
                1,
                '2026-01-01T00:00:00'
            )
            """
        )
        conn.commit()
        conn.close()

        init_db(self.db_path, self.files_dir)

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT document_id, reading_room_id, first_seen_at, last_seen_at
                FROM document_sources
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["document_id"], 10)
        self.assertEqual(row["reading_room_id"], 1)
        self.assertEqual(row["first_seen_at"], "2026-01-01T00:00:00")
        self.assertEqual(row["last_seen_at"], "2026-01-01T00:00:00")

    def test_orphaned_legacy_source_does_not_abort_upgrade(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            models.AGENCIES_TABLE
            + models.OFFICES_TABLE
            + models.READING_ROOMS_TABLE
            + models.DOCUMENTS_TABLE
        )
        conn.execute(
            """
            INSERT INTO documents (
                id, url, title, reading_room_id, discovered_at
            )
            VALUES (
                10,
                'https://example.gov/orphan.pdf',
                'Orphan',
                999,
                '2026-01-01T00:00:00'
            )
            """
        )
        conn.commit()
        conn.close()

        init_db(self.db_path, self.files_dir)

        conn = get_connection(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM document_sources"
            ).fetchone()[0]
            version = get_schema_version(conn)
        finally:
            conn.close()

        self.assertEqual(count, 0)
        self.assertEqual(version, 3)


class DocumentSourceRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)
        self.conn = get_connection(self.db_path)

        self.agency_a = upsert_agency(
            self.conn,
            "agency-a",
            "Agency A",
            {},
        )
        self.agency_b = upsert_agency(
            self.conn,
            "agency-b",
            "Agency B",
            {},
        )
        self.office_a = upsert_office(
            self.conn,
            "office-a",
            "Office A",
            self.agency_a,
            {},
        )
        self.office_b = upsert_office(
            self.conn,
            "office-b",
            "Office B",
            self.agency_b,
            {},
        )
        self.room_a = upsert_reading_room(
            self.conn,
            "https://a.example.gov/library",
            "Library A",
            "office",
            self.agency_a,
            self.office_a,
        )
        self.room_b = upsert_reading_room(
            self.conn,
            "https://b.example.gov/library",
            "Library B",
            "office",
            self.agency_b,
            self.office_b,
        )

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def _insert_document(self):
        return insert_document(
            self.conn,
            url="https://records.example.gov/shared.pdf",
            title="Shared",
            file_type="pdf",
            filename="shared.pdf",
            agency_id=self.agency_a,
            office_id=self.office_a,
            reading_room_id=self.room_a,
            discovered_at="2026-01-01T00:00:00",
        )

    def test_new_document_is_associated_with_primary_source(self):
        doc_id = self._insert_document()
        row = self.conn.execute(
            """
            SELECT reading_room_id
            FROM document_sources
            WHERE document_id = ?
            """,
            (doc_id,),
        ).fetchone()
        self.assertEqual(row["reading_room_id"], self.room_a)

    def test_same_document_can_be_associated_with_multiple_sources(self):
        doc_id = self._insert_document()

        associate_document_source(
            self.conn,
            doc_id,
            self.room_b,
            "2026-01-02T00:00:00",
        )
        associate_document_source(
            self.conn,
            doc_id,
            self.room_b,
            "2026-01-03T00:00:00",
        )

        rows = self.conn.execute(
            """
            SELECT reading_room_id, first_seen_at, last_seen_at
            FROM document_sources
            WHERE document_id = ?
            ORDER BY reading_room_id
            """,
            (doc_id,),
        ).fetchall()

        self.assertEqual(len(rows), 2)
        room_b = next(
            row for row in rows
            if row["reading_room_id"] == self.room_b
        )
        self.assertEqual(room_b["first_seen_at"], "2026-01-02T00:00:00")
        self.assertEqual(room_b["last_seen_at"], "2026-01-03T00:00:00")

    def test_filters_match_secondary_agency_and_office_sources(self):
        doc_id = self._insert_document()
        associate_document_source(
            self.conn,
            doc_id,
            self.room_b,
            "2026-01-02T00:00:00",
        )

        by_agency = query_documents(
            self.conn,
            agency_id=self.agency_b,
        )
        by_office = query_documents(
            self.conn,
            office_id=self.office_b,
        )

        self.assertEqual([row["id"] for row in by_agency], [doc_id])
        self.assertEqual([row["id"] for row in by_office], [doc_id])

    def test_foreign_keys_reject_invalid_source_relationship(self):
        doc_id = self._insert_document()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO document_sources (
                    document_id, reading_room_id, first_seen_at, last_seen_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    doc_id,
                    999999,
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )

    def test_deleting_source_cascades_relationship_not_document(self):
        doc_id = self._insert_document()
        associate_document_source(
            self.conn,
            doc_id,
            self.room_b,
            "2026-01-02T00:00:00",
        )

        self.conn.execute(
            "DELETE FROM reading_rooms WHERE id = ?",
            (self.room_b,),
        )
        self.conn.commit()

        relationship_count = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM document_sources
            WHERE document_id = ? AND reading_room_id = ?
            """,
            (doc_id, self.room_b),
        ).fetchone()[0]
        document_count = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()[0]

        self.assertEqual(relationship_count, 0)
        self.assertEqual(document_count, 1)


if __name__ == "__main__":
    unittest.main()
