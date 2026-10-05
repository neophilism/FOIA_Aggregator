import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from foia_archive.storage import (
    get_archive_stats,
    get_connection,
    get_document_detail,
    get_document_source_details,
    init_db,
    insert_document,
    query_document_snippets,
    update_download_metadata,
    upsert_agency,
    upsert_document_text,
    upsert_office,
    upsert_reading_room,
)
from ui import server


async def asgi_get(app, path="/", params=None):
    query_string = urlencode(params or {}, doseq=True).encode("utf-8")
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, body.decode("utf-8")


class PresentationDataTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)
        self.conn = get_connection(self.db_path)

        self.agency_id = upsert_agency(
            self.conn,
            "demo-agency",
            "Demo Agency",
            {},
        )
        self.office_id = upsert_office(
            self.conn,
            "demo-office",
            "Demo Office",
            self.agency_id,
            {},
        )
        self.room_id = upsert_reading_room(
            self.conn,
            "https://example.gov/foia/library/",
            "Demo FOIA Library",
            "office",
            self.agency_id,
            self.office_id,
            source_type="reading_room",
            seen_at="2026-10-05T00:00:00",
        )
        self.document_id = insert_document(
            self.conn,
            url="https://example.gov/records/demo.pdf",
            title="Public Demonstration Record",
            file_type="pdf",
            filename="demo.pdf",
            agency_id=self.agency_id,
            office_id=self.office_id,
            reading_room_id=self.room_id,
            discovered_at="2026-10-05T00:01:00",
            published_date="2025-06-15",
        )
        update_download_metadata(
            self.conn,
            self.document_id,
            "demo/demo.pdf",
            "2026-10-05T00:02:00",
            mime_type="application/pdf",
            file_size=4096,
            sha256="a" * 64,
            storage_backend="local",
            storage_key="demo/demo.pdf",
        )
        upsert_document_text(
            self.conn,
            self.document_id,
            body=(
                "This released record contains the distinctive phrase "
                "constitutional restoration protocol for demonstration."
            ),
            extraction_status="indexed",
            extraction_method="ocr",
            extracted_at="2026-10-05T00:03:00",
        )

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_live_archive_stats_are_derived_from_database(self):
        stats = get_archive_stats(self.conn)

        self.assertEqual(stats["agencies"], 1)
        self.assertEqual(stats["offices"], 1)
        self.assertEqual(stats["active_sources"], 1)
        self.assertEqual(stats["records"], 1)
        self.assertEqual(stats["archived_records"], 1)
        self.assertEqual(stats["searchable_records"], 1)
        self.assertEqual(stats["ocr_records"], 1)

    def test_body_search_snippet_is_bounded_and_human_readable(self):
        snippets = query_document_snippets(
            self.conn,
            [self.document_id],
            "constitutional restoration",
            tokens=12,
        )

        self.assertIn(self.document_id, snippets)
        self.assertIn("constitutional restoration", snippets[self.document_id])
        self.assertLess(len(snippets[self.document_id]), 500)

    def test_record_detail_includes_search_and_provenance_metadata(self):
        detail = get_document_detail(self.conn, self.document_id)
        sources = get_document_source_details(self.conn, self.document_id)

        self.assertEqual(detail["agency_name"], "Demo Agency")
        self.assertEqual(detail["office_name"], "Demo Office")
        self.assertEqual(detail["extraction_method"], "ocr")
        self.assertIn("distinctive phrase", detail["text_preview"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["label"], "Demo FOIA Library")
        self.assertEqual(sources[0]["agency_name"], "Demo Agency")


class PresentationPageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)

        conn = get_connection(self.db_path)
        agency_id = upsert_agency(conn, "demo", "Demo Agency", {})
        office_id = upsert_office(
            conn,
            "demo-office",
            "Demo Office",
            agency_id,
            {},
        )
        room_id = upsert_reading_room(
            conn,
            "https://example.gov/foia/",
            "Official Reading Room",
            "office",
            agency_id,
            office_id,
        )
        self.document_id = insert_document(
            conn,
            url="https://example.gov/released.pdf",
            title="Released Demonstration File",
            file_type="pdf",
            filename="released.pdf",
            agency_id=agency_id,
            office_id=office_id,
            reading_room_id=room_id,
            discovered_at="2026-10-05T01:00:00",
            published_date="2024-01-02",
        )
        upsert_document_text(
            conn,
            self.document_id,
            body="Needle phrase found inside the OCR text of this released record.",
            extraction_status="indexed",
            extraction_method="ocr",
            extracted_at="2026-10-05T01:01:00",
        )
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def get_db(self):
        return get_connection(self.db_path)

    def test_search_page_renders_public_stats_and_body_match_snippet(self):
        with patch("ui.server.get_db", side_effect=self.get_db):
            status, body = asyncio.run(
                asgi_get(
                    server.app,
                    "/",
                    {"q": "needle phrase"},
                )
            )

        self.assertEqual(status, 200)
        self.assertIn("FOIA Aggregator", body)
        self.assertIn("Federal public-records search", body)
        self.assertIn("1</span>", body)
        self.assertIn("Released Demonstration File", body)
        self.assertIn("Needle phrase found inside", body)
        self.assertIn(f'href="/record/{self.document_id}"', body)

    def test_record_page_explains_ocr_and_source_provenance(self):
        with patch("ui.server.get_db", side_effect=self.get_db):
            status, body = asyncio.run(
                asgi_get(server.app, f"/record/{self.document_id}")
            )

        self.assertEqual(status, 200)
        self.assertIn("Released Demonstration File", body)
        self.assertIn("Indexed via", body)
        self.assertIn("OCR", body)
        self.assertIn("Official Reading Room", body)
        self.assertIn("Needle phrase found inside", body)

    def test_about_page_describes_cross_agency_archive(self):
        with patch("ui.server.get_db", side_effect=self.get_db):
            status, body = asyncio.run(asgi_get(server.app, "/about"))

        self.assertEqual(status, 200)
        self.assertIn(
            "One search across federal records that agencies have already released.",
            body,
        )
        self.assertIn("official agency reading rooms", body)
        self.assertIn("Search the archive", body)


if __name__ == "__main__":
    unittest.main()
