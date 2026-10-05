import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.scraper_core import (
    _filename_hint,
    crawl_reading_room,
)
from foia_archive.storage import (
    get_connection,
    init_db,
    upsert_reading_room,
)
from foia_archive.utils import Config


class FakeResponse:
    def __init__(self, text, status_code=200, headers=None):
        self._raw = text.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=65536):
        yield self._raw

    def close(self):
        return None


class PalCrawlerTests(unittest.TestCase):
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
                    "max_discovered_docs_per_source": 100,
                    "page_max_size_mb": 5,
                    "page_timeout_seconds": 10,
                    "per_host_delay_seconds": 0,
                },
                "downloader": {
                    "max_retries": 0,
                    "retry_backoff_seconds": 0,
                    "max_retry_delay_seconds": 0,
                },
                "storage": {
                    "backend": "local",
                    "db_path": str(self.db_path),
                    "files_dir": str(self.files_dir),
                },
            }
        )
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        self.root_url = "https://securefoia.ntsb.gov/app/ReadingRoom.aspx"
        self.rr_id = upsert_reading_room(
            conn,
            self.root_url,
            "NTSB Reading Room",
            "agency",
            None,
            None,
        )
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_pal_adapter_pages_search_results_and_emits_download_candidates(self):
        root_html = """
        <input id="chk10" lang="10" onclick="javascript: ChildClick(this);">
        <input id="chk9" lang="9" onclick="javascript: ChildClick(this);">
        """
        search_page_1 = """
        <table>
          <tr>
            <td><a href="javascript:showDocs('55','F');">NTSB FY23 FOIA LOG</a></td>
            <td>10/03/2023</td>
          </tr>
          <tr>
            <td><a href="javascript:showDocs('66','F');">DCA22WA102</a></td>
            <td>03/21/2022</td>
          </tr>
        </table>
        <select id="pageIndexOption">
          <option value="0">1</option>
          <option value="1">2</option>
        </select>
        """
        search_page_2 = """
        <table>
          <tr>
            <td><a href="javascript:showDocs('67','F');">DCA85RA032</a></td>
            <td>08/12/1985</td>
          </tr>
        </table>
        <select id="pageIndexOption">
          <option value="0">1</option>
          <option value="1">2</option>
        </select>
        """

        responses = [
            (FakeResponse(root_html), self.root_url),
            (
                FakeResponse(search_page_1),
                "https://securefoia.ntsb.gov/app/SearchDocs.aspx",
            ),
            (
                FakeResponse(search_page_2),
                "https://securefoia.ntsb.gov/app/SearchDocs.aspx",
            ),
        ]

        with (
            patch(
                "foia_archive.scraper_core._fetch_pal_resource",
                side_effect=responses,
            ) as fetch,
            patch(
                "foia_archive.scraper_core._process_document_candidate",
                return_value=True,
            ) as process,
        ):
            crawl_reading_room(
                self.rr_id,
                self.config,
                dry_run=True,
                max_docs=10,
            )

        self.assertEqual(fetch.call_count, 3)
        first_search = fetch.call_args_list[1]
        second_search = fetch.call_args_list[2]
        self.assertEqual(first_search.args[1], "POST")
        self.assertEqual(
            first_search.kwargs["data"]["doctypes"],
            "10,9",
        )
        self.assertEqual(first_search.kwargs["data"]["pageIndex"], "0")
        self.assertEqual(second_search.kwargs["data"]["pageIndex"], "1")

        urls = [call.args[2] for call in process.call_args_list]
        self.assertEqual(
            urls,
            [
                "https://securefoia.ntsb.gov/app/AddAttachment.aspx?docid=55&ispaldoc=F",
                "https://securefoia.ntsb.gov/app/AddAttachment.aspx?docid=66&ispaldoc=F",
                "https://securefoia.ntsb.gov/app/AddAttachment.aspx?docid=67&ispaldoc=F",
            ],
        )
        titles = [call.args[3] for call in process.call_args_list]
        self.assertEqual(
            titles,
            ["NTSB FY23 FOIA LOG", "DCA22WA102", "DCA85RA032"],
        )
        dates = [call.args[4] for call in process.call_args_list]
        self.assertEqual(
            dates,
            ["2023-10-03", "2022-03-21", "1985-08-12"],
        )

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT last_successful_crawl_at, last_error FROM reading_rooms WHERE id = ?",
                (self.rr_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row["last_successful_crawl_at"])
        self.assertIsNone(row["last_error"])

    def test_pal_first_search_failure_records_source_failure(self):
        root_html = """
        <input id="chk10" lang="10" onclick="javascript: ChildClick(this);">
        """
        with patch(
            "foia_archive.scraper_core._fetch_pal_resource",
            side_effect=[
                (FakeResponse(root_html), self.root_url),
                RuntimeError("search unavailable"),
            ],
        ):
            crawl_reading_room(
                self.rr_id,
                self.config,
                dry_run=True,
                max_docs=10,
            )

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT last_successful_crawl_at, last_error FROM reading_rooms WHERE id = ?",
                (self.rr_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNone(row["last_successful_crawl_at"])
        self.assertIn("search unavailable", row["last_error"])

    def test_dynamic_aspx_download_uses_detected_pdf_filename(self):
        self.assertEqual(
            _filename_hint(
                "https://securefoia.ntsb.gov/app/AddAttachment.aspx?docid=55&ispaldoc=F",
                "NTSB FY23 FOIA LOG",
                "pdf",
            ),
            "NTSBFY23FOIALOG.pdf",
        )


if __name__ == "__main__":
    unittest.main()
