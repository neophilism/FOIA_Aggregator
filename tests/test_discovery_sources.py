import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from foia_archive.discovery import (
    extract_reading_room_sources,
    fetch_json,
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


class MetadataFetchResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"data": []}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class MetadataFetchRetryTests(unittest.TestCase):
    def test_rate_limit_honors_retry_after_then_succeeds(self):
        rate_limited = MetadataFetchResponse(
            status_code=429,
            headers={"Retry-After": "3"},
        )
        success = MetadataFetchResponse(
            status_code=200,
            payload={"data": [{"id": "ok"}]},
        )
        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[rate_limited, success],
            ) as request,
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            payload = fetch_json(
                "https://api.foia.gov/api/agency_components",
                5,
                {"X-API-Key": "test"},
                max_retries=2,
                retry_backoff_seconds=1,
            )

        self.assertEqual(payload["data"][0]["id"], "ok")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_temporary_server_error_uses_exponential_backoff(self):
        unavailable = MetadataFetchResponse(status_code=503)
        success = MetadataFetchResponse(status_code=200, payload={"ok": True})
        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[unavailable, success],
            ),
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            payload = fetch_json(
                "https://api.foia.gov/api/agency",
                5,
                {},
                max_retries=2,
                retry_backoff_seconds=0.5,
            )

        self.assertTrue(payload["ok"])
        sleep.assert_called_once_with(0.5)

    def test_connection_error_is_retried(self):
        success = MetadataFetchResponse(status_code=200, payload={"ok": True})
        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[requests.ConnectionError("reset"), success],
            ) as request,
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            payload = fetch_json(
                "https://api.foia.gov/api/agency",
                5,
                {},
                max_retries=1,
                retry_backoff_seconds=0.25,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_non_retryable_client_error_fails_immediately(self):
        forbidden = MetadataFetchResponse(status_code=403)
        with (
            patch(
                "foia_archive.discovery.requests.get",
                return_value=forbidden,
            ) as request,
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            with self.assertRaises(requests.HTTPError):
                fetch_json(
                    "https://api.foia.gov/api/agency",
                    5,
                    {},
                    max_retries=4,
                    retry_backoff_seconds=1,
                )

        request.assert_called_once()
        sleep.assert_not_called()


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

    def test_foia_component_website_is_used_only_as_fallback(self):
        attrs = {
            "website": {"uri": "https://example.gov/about/foia/"},
        }
        self.assertEqual(
            extract_reading_room_sources(attrs),
            [
                {
                    "url": "https://example.gov/about/foia/",
                    "source_type": "foia_website",
                }
            ],
        )

    def test_generic_or_request_focused_websites_are_not_fallback_sources(self):
        self.assertEqual(
            extract_reading_room_sources(
                {"website": {"uri": "https://example.gov/"}}
            ),
            [],
        )
        self.assertEqual(
            extract_reading_room_sources(
                {
                    "website": {
                        "uri": "https://example.gov/foia/request-status/"
                    }
                }
            ),
            [],
        )

    def test_explicit_reading_room_wins_over_foia_website_fallback(self):
        attrs = {
            "reading_rooms": ["https://example.gov/records/"],
            "website": {"uri": "https://example.gov/foia/"},
        }
        self.assertEqual(
            extract_reading_room_sources(attrs),
            [
                {
                    "url": "https://example.gov/records/",
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

    def test_curated_source_is_used_when_metadata_has_no_source(self):
        components = [
            {
                "id": "c1efb796-3bb7-4747-a8b7-415992834318",
                "attributes": {
                    "title": "U.S. Election Assistance Commission",
                    "website": {"uri": "https://www.eac.gov/"},
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
            row = conn.execute(
                """
                SELECT url, source_type, active
                FROM reading_rooms
                WHERE url = ?
                """,
                ("https://www.eac.gov/foia/foia-reading-room",),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["source_type"], "curated_foia")
        self.assertEqual(row["active"], 1)

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
                "downloader": {
                    "max_retries": 0,
                    "retry_backoff_seconds": 0,
                },
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
        self.validation_patcher = patch(
            "foia_archive.scraper_core._validate_public_destination",
            return_value=None,
        )
        self.validation_patcher.start()

    def tearDown(self):
        self.validation_patcher.stop()
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
