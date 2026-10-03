import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from foia_archive.discovery import (
    extract_reading_room_sources,
    refresh_metadata,
)
from foia_archive.scraper_core import crawl_reading_room, get_reading_rooms_to_crawl
from foia_archive.storage import (
    get_connection,
    init_db,
    list_reading_rooms,
    upsert_agency,
    upsert_office,
    upsert_reading_room,
)
from foia_archive.utils import Config


class ReadingRoomExtractionTests(unittest.TestCase):
    def test_only_explicit_publication_source_fields_are_kept(self):
        attrs = {
            "reading_rooms": [
                "HTTPS://EXAMPLE.GOV/reading-room/#section",
            ],
            "foia_library": {
                "url": "https://example.gov/foia-library",
                "label": "FOIA Library",
            },
            "nested": {
                "electronic-reading-rooms": "https://example.gov/e-reading",
                "proactive_disclosures": "https://example.gov/proactive",
                "frequently_requested_records": "https://example.gov/frequent",
            },
            "website": "https://example.gov/",
            "request_form": "https://example.gov/request",
            "resources": ["https://example.gov/random-resource"],
            "links": {"records": "https://example.gov/random-link"},
        }

        sources = extract_reading_room_sources(attrs)

        self.assertEqual(
            sources,
            [
                {
                    "url": "https://example.gov/e-reading",
                    "source_type": "reading_room",
                },
                {
                    "url": "https://example.gov/foia-library",
                    "source_type": "foia_library",
                },
                {
                    "url": "https://example.gov/frequent",
                    "source_type": "frequently_requested_records",
                },
                {
                    "url": "https://example.gov/proactive",
                    "source_type": "proactive_disclosure",
                },
                {
                    "url": "https://example.gov/reading-room/",
                    "source_type": "reading_room",
                },
            ],
        )

    def test_same_url_uses_most_specific_priority(self):
        attrs = {
            "foia_library": "https://example.gov/records",
            "reading_room": "https://example.gov/records",
        }
        self.assertEqual(
            extract_reading_room_sources(attrs),
            [
                {
                    "url": "https://example.gov/records",
                    "source_type": "reading_room",
                }
            ],
        )

    def test_credentials_and_non_http_sources_are_ignored(self):
        attrs = {
            "reading_room": [
                "file:///tmp/records",
                "https://user:pass@example.gov/records",
                "mailto:foia@example.gov",
            ]
        }
        self.assertEqual(extract_reading_room_sources(attrs), [])


class ReadingRoomStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)
        self.conn = get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_upserts_refresh_existing_metadata(self):
        agency_id = upsert_agency(
            self.conn,
            "agency",
            "Old Agency",
            {"version": 1},
        )
        same_agency_id = upsert_agency(
            self.conn,
            "agency",
            "Updated Agency",
            {"version": 2},
        )
        self.assertEqual(agency_id, same_agency_id)

        office_id = upsert_office(
            self.conn,
            "office-id",
            "Old Office",
            agency_id,
            {"version": 1},
        )
        same_office_id = upsert_office(
            self.conn,
            "office-id",
            "Updated Office",
            agency_id,
            {"version": 2},
        )
        self.assertEqual(office_id, same_office_id)

        room_id = upsert_reading_room(
            self.conn,
            "https://example.gov/library",
            "Old label",
            "office",
            agency_id,
            office_id,
            source_type="foia_library",
            seen_at="2026-01-01T00:00:00+00:00",
        )
        same_room_id = upsert_reading_room(
            self.conn,
            "https://example.gov/library",
            "Updated label",
            "office",
            agency_id,
            office_id,
            source_type="reading_room",
            seen_at="2026-01-02T00:00:00+00:00",
        )
        self.assertEqual(room_id, same_room_id)

        agency = self.conn.execute(
            "SELECT name, raw_json FROM agencies WHERE id = ?",
            (agency_id,),
        ).fetchone()
        office = self.conn.execute(
            "SELECT name, raw_json FROM offices WHERE id = ?",
            (office_id,),
        ).fetchone()
        room = self.conn.execute(
            """
            SELECT label, source_type, active, last_seen_at
            FROM reading_rooms WHERE id = ?
            """,
            (room_id,),
        ).fetchone()

        self.assertEqual(agency["name"], "Updated Agency")
        self.assertEqual(json.loads(agency["raw_json"])["version"], 2)
        self.assertEqual(office["name"], "Updated Office")
        self.assertEqual(json.loads(office["raw_json"])["version"], 2)
        self.assertEqual(room["label"], "Updated label")
        self.assertEqual(room["source_type"], "reading_room")
        self.assertEqual(room["active"], 1)
        self.assertEqual(room["last_seen_at"], "2026-01-02T00:00:00+00:00")

    def test_list_reading_rooms_excludes_inactive_by_default(self):
        upsert_reading_room(
            self.conn,
            "https://example.gov/active",
            "Active",
            "office",
            None,
            None,
        )
        inactive_id = upsert_reading_room(
            self.conn,
            "https://example.gov/inactive",
            "Inactive",
            "office",
            None,
            None,
        )
        self.conn.execute(
            "UPDATE reading_rooms SET active = 0 WHERE id = ?",
            (inactive_id,),
        )
        self.conn.commit()

        active_urls = {row["url"] for row in list_reading_rooms(self.conn)}
        all_urls = {
            row["url"]
            for row in list_reading_rooms(self.conn, active_only=False)
        }

        self.assertEqual(active_urls, {"https://example.gov/active"})
        self.assertEqual(
            all_urls,
            {
                "https://example.gov/active",
                "https://example.gov/inactive",
            },
        )

    def test_init_db_migrates_old_reading_room_table(self):
        self.conn.close()
        self.tempdir.cleanup()

        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "old.db"
        self.files_dir = root / "files"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE reading_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                label TEXT,
                level TEXT,
                agency_id INTEGER,
                office_id INTEGER,
                last_crawled_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO reading_rooms (url, label) VALUES (?, ?)",
            ("https://example.gov/legacy", "Legacy"),
        )
        conn.commit()
        conn.close()

        init_db(self.db_path, self.files_dir)
        self.conn = get_connection(self.db_path)

        columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(reading_rooms)"
            ).fetchall()
        }
        row = self.conn.execute(
            "SELECT active FROM reading_rooms"
        ).fetchone()

        self.assertTrue(
            {
                "source_type",
                "active",
                "last_seen_at",
                "last_successful_crawl_at",
                "last_error",
                "last_error_at",
            }.issubset(columns)
        )
        self.assertEqual(row["active"], 1)


class MetadataRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        self.config = Config(
            {
                "crawler": {"user_agent": "FOIAArchiveTest/1.0"},
                "foia_hub": {
                    "base_url": "https://api.foia.gov/api",
                    "timeout_seconds": 5,
                    "api_key": "test-key",
                },
                "storage": {
                    "db_path": str(self.db_path),
                    "files_dir": str(self.files_dir),
                },
            }
        )
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        upsert_reading_room(
            conn,
            "https://example.gov/stale",
            "Stale",
            "office",
            None,
            None,
        )
        conn.close()

        self.agencies = [
            {
                "id": "agency-id",
                "attributes": {"name": "Department Test"},
            }
        ]
        self.components = [
            {
                "id": "component-id",
                "attributes": {
                    "title": "Test Office",
                    "reading_rooms": [
                        "https://example.gov/current",
                    ],
                    "website": "https://example.gov/",
                    "request_form": "https://example.gov/request",
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-id"}},
                },
            }
        ]

    def tearDown(self):
        self.tempdir.cleanup()

    def test_complete_refresh_activates_seen_source_and_deactivates_stale(self):
        with (
            patch(
                "foia_archive.discovery.fetch_agencies",
                return_value=self.agencies,
            ),
            patch(
                "foia_archive.discovery.fetch_agency_components",
                return_value=(self.components, self.agencies),
            ),
        ):
            refresh_metadata(self.config)

        conn = get_connection(self.db_path)
        try:
            rows = {
                row["url"]: row
                for row in conn.execute(
                    """
                    SELECT url, active, source_type, last_seen_at
                    FROM reading_rooms
                    """
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(rows["https://example.gov/current"]["active"], 1)
        self.assertEqual(
            rows["https://example.gov/current"]["source_type"],
            "reading_room",
        )
        self.assertIsNotNone(
            rows["https://example.gov/current"]["last_seen_at"]
        )
        self.assertEqual(rows["https://example.gov/stale"]["active"], 0)
        self.assertNotIn("https://example.gov/", rows)
        self.assertNotIn("https://example.gov/request", rows)

    def test_zero_component_refresh_preserves_active_set(self):
        conn = get_connection(self.db_path)
        conn.execute("UPDATE reading_rooms SET active = 1")
        conn.commit()
        conn.close()

        with (
            patch(
                "foia_archive.discovery.fetch_agencies",
                return_value=self.agencies,
            ),
            patch(
                "foia_archive.discovery.fetch_agency_components",
                return_value=([], self.agencies),
            ),
        ):
            refresh_metadata(self.config)

        conn = get_connection(self.db_path)
        try:
            active = conn.execute(
                """
                SELECT active FROM reading_rooms
                WHERE url = 'https://example.gov/stale'
                """
            ).fetchone()["active"]
        finally:
            conn.close()

        self.assertEqual(active, 1)

    def test_schema_drift_returning_zero_sources_preserves_active_set(self):
        conn = get_connection(self.db_path)
        conn.execute(
            "UPDATE reading_rooms SET active = 1"
        )
        conn.commit()
        conn.close()

        components = [
            {
                "id": "component-id",
                "attributes": {
                    "title": "Test Office",
                    "website": "https://example.gov/",
                    "request_form": "https://example.gov/request",
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-id"}},
                },
            }
        ]

        with (
            patch(
                "foia_archive.discovery.fetch_agencies",
                return_value=self.agencies,
            ),
            patch(
                "foia_archive.discovery.fetch_agency_components",
                return_value=(components, self.agencies),
            ),
        ):
            refresh_metadata(self.config)

        conn = get_connection(self.db_path)
        try:
            active = conn.execute(
                """
                SELECT active FROM reading_rooms
                WHERE url = 'https://example.gov/stale'
                """
            ).fetchone()["active"]
        finally:
            conn.close()

        self.assertEqual(active, 1)


class ReadingRoomCrawlHealthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        self.config = Config(
            {
                "crawler": {"user_agent": "FOIAArchiveTest/1.0"},
                "storage": {
                    "db_path": str(self.db_path),
                    "files_dir": str(self.files_dir),
                },
            }
        )
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        self.rr_id = upsert_reading_room(
            conn,
            "https://example.gov/reading-room/",
            "Test",
            "office",
            None,
            None,
        )
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def _row(self):
        conn = get_connection(self.db_path)
        try:
            return conn.execute(
                "SELECT * FROM reading_rooms WHERE id = ?",
                (self.rr_id,),
            ).fetchone()
        finally:
            conn.close()

    def test_failed_fetch_records_error_and_success_clears_it(self):
        with patch(
            "foia_archive.scraper_core.requests.get",
            side_effect=requests.Timeout("timed out"),
        ):
            crawl_reading_room(
                self.rr_id,
                self.config,
                dry_run=True,
                max_docs=10,
            )

        failed = self._row()
        self.assertIsNotNone(failed["last_crawled_at"])
        self.assertIsNone(failed["last_successful_crawl_at"])
        self.assertIn("Timeout", failed["last_error"])
        self.assertIsNotNone(failed["last_error_at"])

        class Response:
            text = "<html></html>"

            def raise_for_status(self):
                return None

        with patch(
            "foia_archive.scraper_core.requests.get",
            return_value=Response(),
        ):
            crawl_reading_room(
                self.rr_id,
                self.config,
                dry_run=True,
                max_docs=10,
            )

        succeeded = self._row()
        self.assertIsNotNone(succeeded["last_successful_crawl_at"])
        self.assertIsNone(succeeded["last_error"])
        self.assertIsNone(succeeded["last_error_at"])

    def test_crawler_selects_only_active_sources(self):
        conn = get_connection(self.db_path)
        inactive_id = upsert_reading_room(
            conn,
            "https://example.gov/inactive/",
            "Inactive",
            "office",
            None,
            None,
        )
        conn.execute(
            "UPDATE reading_rooms SET active = 0 WHERE id = ?",
            (inactive_id,),
        )
        conn.commit()
        conn.close()

        rooms = get_reading_rooms_to_crawl(self.config)
        self.assertEqual([room["id"] for room in rooms], [self.rr_id])


if __name__ == "__main__":
    unittest.main()
