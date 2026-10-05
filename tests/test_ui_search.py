import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from foia_archive.storage import (
    get_connection,
    init_db,
    insert_document,
    query_documents,
    query_documents_page,
    upsert_agency,
    upsert_office,
    upsert_reading_room,
)
from ui import server


class SearchQueryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)
        self.conn = get_connection(self.db_path)

        self.agency_id = upsert_agency(
            self.conn,
            "agency",
            "Test Agency",
            {},
        )
        self.office_id = upsert_office(
            self.conn,
            "office",
            "Test Office",
            self.agency_id,
            {},
        )
        self.room_id = upsert_reading_room(
            self.conn,
            "https://example.gov/reading-room/",
            "Reading Room",
            "office",
            self.agency_id,
            self.office_id,
        )

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def insert(
        self,
        index,
        *,
        title=None,
        filename=None,
        file_type="pdf",
        published_date=None,
    ):
        return insert_document(
            self.conn,
            url=f"https://example.gov/records/{index}.{file_type}",
            title=title if title is not None else f"Document {index:03d}",
            file_type=file_type,
            filename=filename if filename is not None else f"document-{index:03d}.{file_type}",
            agency_id=self.agency_id,
            office_id=self.office_id,
            reading_room_id=self.room_id,
            discovered_at=f"2026-01-{(index % 28) + 1:02d}T{index % 24:02d}:00:00",
            published_date=published_date,
        )

    def test_pagination_has_no_hidden_200_result_ceiling(self):
        for index in range(230):
            self.insert(index)

        rows, total = query_documents_page(
            self.conn,
            page=5,
            page_size=50,
            sort="title_asc",
        )

        self.assertEqual(total, 230)
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["title"], "Document 200")
        self.assertEqual(rows[-1]["title"], "Document 229")

    def test_title_search_is_case_insensitive_and_matches_filename(self):
        self.insert(
            1,
            title="Project MONGOOSE memorandum",
            filename="ordinary.pdf",
        )
        self.insert(
            2,
            title="Ordinary title",
            filename="mongoose-appendix.pdf",
        )
        self.insert(3, title="Unrelated")

        rows, total = query_documents_page(
            self.conn,
            title_query="mongoose",
            page_size=50,
        )

        self.assertEqual(total, 2)
        self.assertEqual(
            {row["title"] for row in rows},
            {"Project MONGOOSE memorandum", "Ordinary title"},
        )

    def test_search_treats_sql_wildcards_as_literal_text(self):
        self.insert(1, title="Budget 100% final")
        self.insert(2, title="Budget 1000 final")
        self.insert(3, title="A_B record")
        self.insert(4, title="ACB record")

        percent_rows, percent_total = query_documents_page(
            self.conn,
            title_query="100%",
        )
        underscore_rows, underscore_total = query_documents_page(
            self.conn,
            title_query="A_B",
        )

        self.assertEqual(percent_total, 1)
        self.assertEqual(percent_rows[0]["title"], "Budget 100% final")
        self.assertEqual(underscore_total, 1)
        self.assertEqual(underscore_rows[0]["title"], "A_B record")

    def test_sort_modes_are_stable_and_put_unknown_publication_dates_last(self):
        self.insert(1, title="Zulu", published_date=None)
        self.insert(2, title="Alpha", published_date="2024-02-01")
        self.insert(3, title="Beta", published_date="2023-01-01")

        title_rows = query_documents(
            self.conn,
            sort="title_asc",
            limit=None,
        )
        published_rows = query_documents(
            self.conn,
            sort="published_desc",
            limit=None,
        )

        self.assertEqual(
            [row["title"] for row in title_rows],
            ["Alpha", "Beta", "Zulu"],
        )
        self.assertEqual(
            [row["title"] for row in published_rows],
            ["Alpha", "Beta", "Zulu"],
        )

    def test_invalid_sort_falls_back_to_discovered_newest(self):
        self.insert(1, title="First")
        self.insert(29, title="Later")

        fallback = query_documents(
            self.conn,
            sort="not-a-sort",
            limit=None,
        )
        explicit = query_documents(
            self.conn,
            sort="discovered_desc",
            limit=None,
        )

        self.assertEqual(
            [row["id"] for row in fallback],
            [row["id"] for row in explicit],
        )


class SearchPageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        agency_id = upsert_agency(conn, "agency", "Test Agency", {})
        office_id = upsert_office(
            conn,
            "office",
            "Test Office",
            agency_id,
            {},
        )
        room_id = upsert_reading_room(
            conn,
            "https://example.gov/reading-room/",
            "Reading Room",
            "office",
            agency_id,
            office_id,
        )
        for index in range(75):
            insert_document(
                conn,
                url=f"https://example.gov/{index}.pdf",
                title=(
                    f"Mongoose Record {index:02d}"
                    if index < 55
                    else f"Other Record {index:02d}"
                ),
                file_type="pdf",
                filename=f"{index}.pdf",
                agency_id=agency_id,
                office_id=office_id,
                reading_room_id=room_id,
                discovered_at=f"2026-02-{(index % 28) + 1:02d}T00:00:00",
                published_date=None,
            )
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def get_db(self):
        return get_connection(self.db_path)

    def render(self, **overrides):
        params = {
            "q": None,
            "agency_id": None,
            "office_id": None,
            "file_type": None,
            "start_date": None,
            "end_date": None,
            "sort": "discovered_desc",
            "page": 1,
            "page_size": 50,
        }
        params.update(overrides)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 1234),
            }
        )
        with patch("ui.server.get_db", side_effect=self.get_db):
            response = asyncio.run(
                server.search_page(
                    request=request,
                    **params,
                )
            )
        return response.body.decode("utf-8")

    def test_page_renders_counts_search_value_and_next_link(self):
        html = self.render(q="mongoose", page=1, page_size=25)

        self.assertIn("Showing 1–25 of 55 results", html)
        self.assertIn('value="mongoose"', html)
        self.assertIn("Page 1 of 3", html)
        self.assertIn("Next →", html)
        self.assertIn("q=mongoose", html)
        self.assertIn("page_size=25", html)
        self.assertIn("page=2", html)

    def test_out_of_range_page_clamps_to_last_page(self):
        html = self.render(q="mongoose", page=99, page_size=25)

        self.assertIn("Showing 51–55 of 55 results", html)
        self.assertIn("Page 3 of 3", html)

    def test_empty_search_has_clear_empty_state(self):
        html = self.render(q="does-not-exist")

        self.assertIn("0 results", html)
        self.assertIn(
            "No documents match the current search and filters.",
            html,
        )

    def test_download_status_is_visible(self):
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM documents ORDER BY id LIMIT 1"
            ).fetchone()
            conn.execute(
                """
                UPDATE documents
                SET download_status = 'content_mismatch',
                    download_error = 'returned text/html'
                WHERE id = ?
                """,
                (row["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        html = self.render(sort="title_asc", page_size=100)

        self.assertIn("Content Mismatch", html)
        self.assertIn('title="returned text/html"', html)


class PaginationURLTests(unittest.TestCase):
    def test_page_url_preserves_filters_and_omits_defaults(self):
        url = server._page_url(
            2,
            title_query="budget memo",
            agency_id=4,
            office_id=9,
            file_type="pdf",
            start_date="2024-01-01",
            end_date="2024-12-31",
            sort="title_asc",
            page_size=25,
        )

        self.assertIn("q=budget+memo", url)
        self.assertIn("agency_id=4", url)
        self.assertIn("office_id=9", url)
        self.assertIn("file_type=pdf", url)
        self.assertIn("start_date=2024-01-01", url)
        self.assertIn("end_date=2024-12-31", url)
        self.assertIn("sort=title_asc", url)
        self.assertIn("page_size=25", url)
        self.assertIn("page=2", url)

        defaults = server._page_url(
            1,
            title_query=None,
            agency_id=None,
            office_id=None,
            file_type=None,
            start_date=None,
            end_date=None,
            sort="discovered_desc",
            page_size=50,
        )
        self.assertEqual(defaults, "/?")


if __name__ == "__main__":
    unittest.main()
