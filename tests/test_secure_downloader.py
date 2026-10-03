import hashlib
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.scraper_core import (
    DownloadResult,
    _validate_public_destination,
    download_document,
    extract_document_links,
)
from foia_archive.storage import (
    get_connection,
    init_db,
    insert_document,
    update_download_failure,
    update_download_metadata,
)
from foia_archive.utils import Config


PUBLIC_ADDRINFO = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
]
PRIVATE_ADDRINFO = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
]


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks or [])
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class SecureDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.files_dir = self.root / "files"
        self.config = Config(
            {
                "crawler": {"user_agent": "FOIAArchiveTest/1.0"},
                "downloader": {
                    "timeout_seconds": 5,
                    "max_file_size_mb": 1,
                    "max_redirects": 3,
                    "max_retries": 2,
                    "retry_backoff_seconds": 0,
                },
                "storage": {"files_dir": str(self.files_dir)},
            }
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_link_extraction_rejects_unsafe_schemes_credentials_and_local_ips(self):
        html = """
        <a href="javascript:alert(1)//evil.pdf">bad js</a>
        <a href="file:///tmp/secret.pdf">bad file</a>
        <a href="https://user:pass@example.gov/private.pdf">credentials</a>
        <a href="http://127.0.0.1/private.pdf">loopback</a>
        <a href="https://example.gov/public.pdf">public</a>
        """
        links = extract_document_links(html, "https://example.gov/")
        self.assertEqual(
            [link["url"] for link in links],
            ["https://example.gov/public.pdf"],
        )

    def test_public_destination_rejects_private_dns_resolution(self):
        with patch(
            "foia_archive.scraper_core.socket.getaddrinfo",
            return_value=PRIVATE_ADDRINFO,
        ):
            with self.assertRaises(ValueError):
                _validate_public_destination("http://example.gov/report.pdf")

    def test_redirect_to_private_destination_is_blocked_before_second_request(self):
        redirect = FakeResponse(
            302,
            {"Location": "http://127.0.0.1/internal.pdf"},
        )
        with (
            patch(
                "foia_archive.scraper_core.socket.getaddrinfo",
                return_value=PUBLIC_ADDRINFO,
            ),
            patch(
                "foia_archive.scraper_core.requests.get",
                return_value=redirect,
            ) as request,
        ):
            result = download_document(
                "https://example.gov/report.pdf",
                "report.pdf",
                self.config,
            )

        self.assertEqual(result.status, "blocked_url")
        self.assertEqual(request.call_count, 1)
        self.assertTrue(redirect.closed)

    def test_successful_download_streams_and_returns_integrity_metadata(self):
        body = [b"abc", b"def"]
        response = FakeResponse(
            200,
            {
                "Content-Length": "6",
                "Content-Type": "application/pdf; charset=binary",
            },
            body,
        )
        with (
            patch(
                "foia_archive.scraper_core.socket.getaddrinfo",
                return_value=PUBLIC_ADDRINFO,
            ),
            patch(
                "foia_archive.scraper_core.requests.get",
                return_value=response,
            ),
        ):
            result = download_document(
                "https://example.gov/report.pdf",
                "report.pdf",
                self.config,
            )

        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.file_size, 6)
        self.assertEqual(result.mime_type, "application/pdf")
        self.assertEqual(result.sha256, hashlib.sha256(b"abcdef").hexdigest())
        self.assertIsNotNone(result.path)
        self.assertEqual(result.path.read_bytes(), b"abcdef")
        self.assertTrue(response.closed)
        self.assertEqual(list(self.files_dir.glob(".partial-*")), [])

    def test_declared_oversize_download_is_rejected_without_archive_file(self):
        response = FakeResponse(
            200,
            {"Content-Length": str(2 * 1024 * 1024)},
            [b"unused"],
        )
        with (
            patch(
                "foia_archive.scraper_core.socket.getaddrinfo",
                return_value=PUBLIC_ADDRINFO,
            ),
            patch(
                "foia_archive.scraper_core.requests.get",
                return_value=response,
            ),
        ):
            result = download_document(
                "https://example.gov/report.pdf",
                "report.pdf",
                self.config,
            )

        self.assertEqual(result.status, "too_large")
        self.assertEqual(list(self.files_dir.iterdir()), [])

    def test_streamed_oversize_download_removes_partial_file(self):
        config = Config(
            {
                "crawler": {"user_agent": "FOIAArchiveTest/1.0"},
                "downloader": {
                    "max_file_size_mb": 0.000001,
                    "max_retries": 0,
                    "retry_backoff_seconds": 0,
                },
                "storage": {"files_dir": str(self.files_dir)},
            }
        )
        response = FakeResponse(200, {}, [b"too large"])
        with (
            patch(
                "foia_archive.scraper_core.socket.getaddrinfo",
                return_value=PUBLIC_ADDRINFO,
            ),
            patch(
                "foia_archive.scraper_core.requests.get",
                return_value=response,
            ),
        ):
            result = download_document(
                "https://example.gov/report.pdf",
                "report.pdf",
                config,
            )

        self.assertEqual(result.status, "too_large")
        self.assertEqual(list(self.files_dir.iterdir()), [])

    def test_retryable_http_status_is_retried_then_succeeds(self):
        first = FakeResponse(503)
        second = FakeResponse(
            200,
            {"Content-Type": "application/pdf"},
            [b"ok"],
        )
        with (
            patch(
                "foia_archive.scraper_core.socket.getaddrinfo",
                return_value=PUBLIC_ADDRINFO,
            ),
            patch(
                "foia_archive.scraper_core.requests.get",
                side_effect=[first, second],
            ) as request,
            patch("foia_archive.scraper_core.time.sleep") as sleep,
        ):
            result = download_document(
                "https://example.gov/report.pdf",
                "report.pdf",
                self.config,
            )

        self.assertEqual(result.status, "downloaded")
        self.assertEqual(request.call_count, 2)
        sleep.assert_not_called()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_non_retryable_http_error_is_not_retried(self):
        response = FakeResponse(404)
        with (
            patch(
                "foia_archive.scraper_core.socket.getaddrinfo",
                return_value=PUBLIC_ADDRINFO,
            ),
            patch(
                "foia_archive.scraper_core.requests.get",
                return_value=response,
            ) as request,
        ):
            result = download_document(
                "https://example.gov/missing.pdf",
                "missing.pdf",
                self.config,
            )

        self.assertEqual(result.status, "http_error")
        self.assertEqual(request.call_count, 1)


class DownloadMetadataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "archive.db"
        self.files_dir = self.root / "files"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_init_db_adds_download_metadata_columns_to_existing_database(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                local_path TEXT,
                downloaded_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO documents (url, local_path) VALUES (?, ?)",
            ("https://example.gov/report.pdf", "report.pdf"),
        )
        conn.commit()
        conn.close()

        init_db(self.db_path, self.files_dir)

        conn = get_connection(self.db_path)
        try:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(documents)").fetchall()
            }
            row = conn.execute(
                "SELECT download_status FROM documents"
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(
            {
                "mime_type",
                "file_size",
                "sha256",
                "download_status",
                "download_error",
                "last_download_attempt_at",
            }.issubset(columns)
        )
        self.assertEqual(row["download_status"], "downloaded")

    def test_download_failure_status_and_error_are_persisted(self):
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        try:
            doc_id = insert_document(
                conn,
                url="https://example.gov/report.pdf",
                title="Report",
                file_type="pdf",
                filename="report.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-01-01T00:00:00",
            )
            update_download_failure(
                conn,
                doc_id,
                "blocked_url",
                "URL resolves to a non-public address",
                "2026-01-01T00:00:01",
            )
            row = conn.execute(
                """
                SELECT download_status, download_error,
                       last_download_attempt_at, local_path
                FROM documents WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["download_status"], "blocked_url")
        self.assertIn("non-public", row["download_error"])
        self.assertEqual(
            row["last_download_attempt_at"],
            "2026-01-01T00:00:01",
        )
        self.assertIsNone(row["local_path"])

    def test_failure_clears_stale_archive_metadata(self):
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        try:
            doc_id = insert_document(
                conn,
                url="https://example.gov/stale.pdf",
                title="Stale",
                file_type="pdf",
                filename="stale.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-01-01T00:00:00",
            )
            update_download_metadata(
                conn,
                doc_id,
                "missing.pdf",
                "2026-01-01T00:00:01",
                mime_type="application/pdf",
                file_size=10,
                sha256="deadbeef",
            )
            update_download_failure(
                conn,
                doc_id,
                "retryable_error",
                "replacement failed",
                "2026-01-01T00:00:02",
            )
            row = conn.execute(
                """
                SELECT local_path, downloaded_at, mime_type, file_size,
                       sha256, download_status
                FROM documents WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNone(row["local_path"])
        self.assertIsNone(row["downloaded_at"])
        self.assertIsNone(row["mime_type"])
        self.assertIsNone(row["file_size"])
        self.assertIsNone(row["sha256"])
        self.assertEqual(row["download_status"], "retryable_error")

    def test_download_metadata_is_persisted_with_completed_status(self):
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        try:
            doc_id = insert_document(
                conn,
                url="https://example.gov/report.pdf",
                title="Report",
                file_type="pdf",
                filename="report.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-01-01T00:00:00",
            )
            update_download_metadata(
                conn,
                doc_id,
                "report.pdf",
                "2026-01-01T00:00:01",
                mime_type="application/pdf",
                file_size=6,
                sha256="abc123",
            )
            row = conn.execute(
                """
                SELECT download_status, mime_type, file_size, sha256,
                       download_error, last_download_attempt_at
                FROM documents WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["download_status"], "downloaded")
        self.assertEqual(row["mime_type"], "application/pdf")
        self.assertEqual(row["file_size"], 6)
        self.assertEqual(row["sha256"], "abc123")
        self.assertIsNone(row["download_error"])
        self.assertEqual(
            row["last_download_attempt_at"],
            "2026-01-01T00:00:01",
        )


if __name__ == "__main__":
    unittest.main()
