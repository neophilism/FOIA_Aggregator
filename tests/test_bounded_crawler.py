import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.scraper_core import (
    CrawlScope,
    HostRateLimiter,
    _document_type_from_response,
    canonicalize_url,
    crawl_reading_room,
    extract_crawl_targets,
    extract_document_links,
)
from foia_archive.storage import get_connection, init_db, upsert_reading_room
from foia_archive.utils import Config


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None, body=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.closed = False
        if body is None:
            body = text.encode("utf-8")
        self._body = body

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


class CanonicalizationTests(unittest.TestCase):
    def test_canonicalize_removes_fragment_default_port_and_path_noise(self):
        self.assertEqual(
            canonicalize_url(
                "HTTPS://EXAMPLE.GOV:443/reading-room//2024/../page?b=2&a=1#section"
            ),
            "https://example.gov/reading-room/page?b=2&a=1",
        )

    def test_document_link_extraction_deduplicates_fragments_and_supports_more_formats(self):
        html = """
        <a href="records/data.csv#one">CSV one</a>
        <a href="records/data.csv#two">CSV two</a>
        <a href="records/slides.pptx">Slides</a>
        <a href="records/message.eml">Mail</a>
        """
        links = extract_document_links(
            html,
            "https://example.gov/reading-room/",
        )
        self.assertEqual(
            [(link["url"], link["file_type"]) for link in links],
            [
                ("https://example.gov/reading-room/records/data.csv", "csv"),
                ("https://example.gov/reading-room/records/slides.pptx", "pptx"),
                ("https://example.gov/reading-room/records/message.eml", "eml"),
            ],
        )

    def test_page_targets_stay_in_scope_and_deduplicate_fragments(self):
        scope = CrawlScope(
            hostname="example.gov",
            port=None,
            path_prefix="/reading-room",
        )
        html = """
        <a href="page#one">Page one</a>
        <a href="./page#two">Page two</a>
        <a href="/outside/page">Outside</a>
        <a href="asset.js">Script</a>
        <a href="record.pdf">Document</a>
        """
        targets = extract_crawl_targets(
            html,
            "https://example.gov/reading-room/",
            scope,
            depth=1,
        )
        self.assertEqual(
            [target.url for target in targets],
            ["https://example.gov/reading-room/page"],
        )


class MimeDetectionTests(unittest.TestCase):
    def test_content_disposition_identifies_extensionless_document(self):
        response = FakeResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename=records.xlsx",
            }
        )
        self.assertEqual(
            _document_type_from_response(
                response,
                "https://example.gov/reading-room/download?id=10",
            ),
            "xlsx",
        )


class HostRateLimiterTests(unittest.TestCase):
    def test_limiter_waits_only_for_remaining_host_delay(self):
        limiter = HostRateLimiter(1.0)
        with (
            patch(
                "foia_archive.scraper_core.time.monotonic",
                side_effect=[0.0, 0.0, 0.25, 1.0],
            ),
            patch("foia_archive.scraper_core.time.sleep") as sleep,
        ):
            limiter.wait("https://example.gov/one")
            limiter.wait("https://example.gov/two")

        sleep.assert_called_once_with(0.75)


class BoundedReadingRoomCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        self.config = Config(
            {
                "crawler": {
                    "user_agent": "FOIAArchiveTest/1.0",
                    "max_pages_per_source": 10,
                    "max_depth": 2,
                    "max_discovered_docs_per_source": 20,
                    "page_timeout_seconds": 5,
                    "page_max_size_mb": 1,
                    "per_host_delay_seconds": 0,
                },
                "downloader": {
                    "max_redirects": 2,
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
            "Test Reading Room",
            "office",
            None,
            None,
            source_type="reading_room",
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

    def _documents(self):
        conn = get_connection(self.db_path)
        try:
            return {
                row["url"]: row
                for row in conn.execute(
                    """
                    SELECT url, title, file_type, filename
                    FROM documents ORDER BY url
                    """
                ).fetchall()
            }
        finally:
            conn.close()

    def test_crawls_pagination_and_detects_extensionless_document_by_mime(self):
        root_html = """
        <a href="?page=2">Next</a>
        <a href="report.pdf">Report</a>
        <a href="data.csv">Data</a>
        <a href="/outside/page">Outside page</a>
        <a href="https://other.gov/archive/external.pdf">External PDF</a>
        """
        page_two_html = """
        <a href="extensionless">Extensionless PDF</a>
        <a href="page3">Page three</a>
        """
        page_three_html = """
        <a href="slides.pptx">Slides</a>
        <a href="deeper">Too deep</a>
        """

        responses = {
            "https://example.gov/reading-room/": FakeResponse(
                root_html,
                headers={"Content-Type": "text/html"},
            ),
            "https://example.gov/reading-room/?page=2": FakeResponse(
                page_two_html,
                headers={"Content-Type": "text/html"},
            ),
            "https://example.gov/reading-room/extensionless": FakeResponse(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                body=b"%PDF",
            ),
            "https://example.gov/reading-room/page3": FakeResponse(
                page_three_html,
                headers={"Content-Type": "text/html"},
            ),
        }
        requested = []

        def fake_get(url, **kwargs):
            requested.append(url)
            if url not in responses:
                raise AssertionError(f"Unexpected crawl request: {url}")
            return responses[url]

        with patch(
            "foia_archive.scraper_core.requests.get",
            side_effect=fake_get,
        ):
            crawl_reading_room(
                self.rr_id,
                self.config,
                dry_run=True,
                max_docs=20,
            )

        documents = self._documents()
        self.assertEqual(
            set(documents),
            {
                "https://example.gov/reading-room/data.csv",
                "https://example.gov/reading-room/extensionless",
                "https://example.gov/reading-room/report.pdf",
                "https://example.gov/reading-room/slides.pptx",
                "https://other.gov/archive/external.pdf",
            },
        )
        self.assertEqual(
            documents["https://example.gov/reading-room/extensionless"]["file_type"],
            "pdf",
        )
        self.assertEqual(
            documents["https://example.gov/reading-room/extensionless"]["filename"],
            "extensionless.pdf",
        )
        self.assertEqual(
            requested,
            [
                "https://example.gov/reading-room/",
                "https://example.gov/reading-room/?page=2",
                "https://example.gov/reading-room/extensionless",
                "https://example.gov/reading-room/page3",
            ],
        )
        self.assertNotIn("https://example.gov/outside/page", requested)
        self.assertNotIn("https://example.gov/reading-room/deeper", requested)

    def test_page_limit_stops_frontier_growth(self):
        config = Config(
            {
                **self.config.data,
                "crawler": {
                    **self.config.crawler,
                    "max_pages_per_source": 2,
                    "max_depth": 5,
                },
            }
        )
        root_html = """
        <a href="page1">One</a>
        <a href="page2">Two</a>
        <a href="page3">Three</a>
        """
        responses = {
            "https://example.gov/reading-room/": FakeResponse(
                root_html,
                headers={"Content-Type": "text/html"},
            ),
            "https://example.gov/reading-room/page1": FakeResponse(
                "<html></html>",
                headers={"Content-Type": "text/html"},
            ),
        }
        requested = []

        def fake_get(url, **kwargs):
            requested.append(url)
            if url not in responses:
                raise AssertionError(f"Unexpected crawl request: {url}")
            return responses[url]

        with patch(
            "foia_archive.scraper_core.requests.get",
            side_effect=fake_get,
        ):
            crawl_reading_room(
                self.rr_id,
                config,
                dry_run=True,
                max_docs=20,
            )

        self.assertEqual(
            requested,
            [
                "https://example.gov/reading-room/",
                "https://example.gov/reading-room/page1",
            ],
        )

    def test_document_safety_limit_caps_unique_candidates(self):
        config = Config(
            {
                **self.config.data,
                "crawler": {
                    **self.config.crawler,
                    "max_discovered_docs_per_source": 2,
                },
            }
        )
        root_html = """
        <a href="one.pdf">One</a>
        <a href="two.pdf">Two</a>
        <a href="three.pdf">Three</a>
        """
        with patch(
            "foia_archive.scraper_core.requests.get",
            return_value=FakeResponse(
                root_html,
                headers={"Content-Type": "text/html"},
            ),
        ):
            crawl_reading_room(
                self.rr_id,
                config,
                dry_run=True,
                max_docs=20,
            )

        self.assertEqual(len(self._documents()), 2)


if __name__ == "__main__":
    unittest.main()
