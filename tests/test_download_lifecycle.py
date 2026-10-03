import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.scraper_core import _save_file, crawl_reading_room
from foia_archive.storage import (
    get_connection,
    init_db,
    insert_document,
    update_download_metadata,
    upsert_reading_room,
)
from foia_archive.utils import Config


class PageResponse:
    def __init__(self, html):
        self.text = html

    def raise_for_status(self):
        return None


class DownloadLifecycleTests(unittest.TestCase):
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
        self.room_url = "https://example.gov/reading-room/"
        self.rr_id = upsert_reading_room(
            conn,
            self.room_url,
            "Test room",
            "office",
            None,
            None,
        )
        conn.close()
        self.document_url = "https://example.gov/records/report.pdf"
        self.page_html = '<a href="/records/report.pdf">Report</a>'

    def tearDown(self):
        self.tempdir.cleanup()

    def _page_get(self, *args, **kwargs):
        return PageResponse(self.page_html)

    def _write_download(self, url, filename_hint, config):
        path = self.files_dir / "saved.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pdf")
        return path

    def _document_row(self):
        conn = get_connection(self.db_path)
        try:
            return conn.execute(
                "SELECT * FROM documents WHERE url = ?",
                (self.document_url,),
            ).fetchone()
        finally:
            conn.close()

    def test_dry_run_document_is_downloaded_on_later_live_run(self):
        with patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get):
            crawl_reading_room(self.rr_id, self.config, dry_run=True, max_docs=10)

        row = self._document_row()
        self.assertIsNotNone(row)
        self.assertIsNone(row["local_path"])
        self.assertIsNone(row["downloaded_at"])

        with (
            patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get),
            patch(
                "foia_archive.scraper_core.download_document",
                side_effect=self._write_download,
            ) as download,
        ):
            crawl_reading_room(self.rr_id, self.config, dry_run=False, max_docs=None)

        download.assert_called_once()
        row = self._document_row()
        self.assertEqual(row["local_path"], "saved.pdf")
        self.assertIsNotNone(row["downloaded_at"])

    def test_failed_download_is_retried_on_next_live_run(self):
        with (
            patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get),
            patch("foia_archive.scraper_core.download_document", return_value=None) as first_download,
        ):
            crawl_reading_room(self.rr_id, self.config, dry_run=False, max_docs=None)

        first_download.assert_called_once()
        row = self._document_row()
        self.assertIsNone(row["local_path"])
        self.assertIsNone(row["downloaded_at"])

        with (
            patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get),
            patch(
                "foia_archive.scraper_core.download_document",
                side_effect=self._write_download,
            ) as second_download,
        ):
            crawl_reading_room(self.rr_id, self.config, dry_run=False, max_docs=None)

        second_download.assert_called_once()
        row = self._document_row()
        self.assertEqual(row["local_path"], "saved.pdf")
        self.assertIsNotNone(row["downloaded_at"])

    def test_existing_downloaded_file_is_not_downloaded_again(self):
        with (
            patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get),
            patch(
                "foia_archive.scraper_core.download_document",
                side_effect=self._write_download,
            ),
        ):
            crawl_reading_room(self.rr_id, self.config, dry_run=False, max_docs=None)

        with (
            patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get),
            patch("foia_archive.scraper_core.download_document") as download,
        ):
            crawl_reading_room(self.rr_id, self.config, dry_run=False, max_docs=None)

        download.assert_not_called()

    def test_missing_archived_file_is_downloaded_again(self):
        conn = get_connection(self.db_path)
        try:
            doc_id = insert_document(
                conn,
                url=self.document_url,
                title="Report",
                file_type="pdf",
                filename="report.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=self.rr_id,
                discovered_at="2026-01-01T00:00:00",
            )
            update_download_metadata(
                conn,
                doc_id,
                "missing.pdf",
                "2026-01-01T00:00:01",
            )
        finally:
            conn.close()

        with (
            patch("foia_archive.scraper_core.requests.get", side_effect=self._page_get),
            patch(
                "foia_archive.scraper_core.download_document",
                side_effect=self._write_download,
            ) as download,
        ):
            crawl_reading_room(self.rr_id, self.config, dry_run=False, max_docs=None)

        download.assert_called_once()
        row = self._document_row()
        self.assertEqual(row["local_path"], "saved.pdf")

    def test_save_file_does_not_duplicate_existing_extension(self):
        path = _save_file(
            b"pdf",
            self.document_url,
            self.files_dir,
            "report.pdf",
        )
        self.assertTrue(path.name.endswith("_report.pdf"))
        self.assertFalse(path.name.endswith(".pdf.pdf"))


if __name__ == "__main__":
    unittest.main()
