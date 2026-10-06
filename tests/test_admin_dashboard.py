import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.storage import (
    get_admin_dashboard_stats,
    get_connection,
    init_db,
    insert_document,
    record_reading_room_crawl_failure,
    update_download_metadata,
    upsert_agency,
    upsert_document_text,
    upsert_office,
    upsert_reading_room,
)
from ui import server


async def asgi_get(app, path="/", headers=None):
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    raw_headers = [
        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    await app(scope, receive, send)

    start = next(
        message for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    headers_out = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in start.get("headers", [])
    }
    return start["status"], body.decode("utf-8"), headers_out


def basic(username: str, password: str) -> str:
    encoded = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {encoded}"


class AdminDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)

        conn = get_connection(self.db_path)
        agency_id = upsert_agency(conn, "demo", "Demo Agency", {})
        office_id = upsert_office(
            conn, "demo-office", "Demo Office", agency_id, {}
        )
        self.room_id = upsert_reading_room(
            conn,
            "https://example.gov/foia/",
            "Demo Reading Room",
            "office",
            agency_id,
            office_id,
        )
        doc_id = insert_document(
            conn,
            url="https://example.gov/demo.pdf",
            title="Administrator Demo Record",
            file_type="pdf",
            filename="demo.pdf",
            agency_id=agency_id,
            office_id=office_id,
            reading_room_id=self.room_id,
            discovered_at="2099-10-05T01:00:00+00:00",
            published_date="2026-01-01",
        )
        update_download_metadata(
            conn,
            doc_id,
            None,
            "2099-10-05T01:01:00+00:00",
            file_size=4096,
            sha256="a" * 64,
            storage_backend="b2",
            storage_key="documents/aa/demo.pdf",
        )
        upsert_document_text(
            conn,
            doc_id,
            body="Searchable administrator demonstration text.",
            extraction_status="indexed",
            extraction_method="ocr",
            extracted_at="2099-10-05T01:02:00+00:00",
        )
        record_reading_room_crawl_failure(
            conn,
            self.room_id,
            "2099-10-05T01:03:00+00:00",
            "HTTP 403 Forbidden",
        )
        conn.close()

        self.storage_metrics = {
            "backend": "b2",
            "error": None,
            "stored_bytes": 2_000_000_000,
            "document_bytes": 1_900_000_000,
            "backup_bytes": 100_000_000,
            "other_bytes": 0,
            "version_count": 50,
            "current_object_count": 45,
            "document_cap_bytes": 8_500_000_000,
            "target_total_bytes": 9_000_000_000,
            "latest_backup_at": "2099-10-05T01:05:00+00:00",
            "backup_count": 3,
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def get_db(self):
        return get_connection(self.db_path)

    def test_operational_stats_surface_source_failure_and_pipeline_state(self):
        conn = self.get_db()
        try:
            stats = get_admin_dashboard_stats(conn)
        finally:
            conn.close()

        self.assertEqual(stats["sources"]["active"], 1)
        self.assertEqual(stats["sources"]["with_errors"], 1)
        self.assertEqual(stats["sources"]["blocked_403"], 1)
        self.assertEqual(stats["download_statuses"][0]["label"], "downloaded")
        self.assertEqual(stats["extraction_methods"][0]["label"], "ocr")
        self.assertEqual(stats["database_archived_bytes"], 4096)

    def test_admin_route_is_hidden_when_password_is_not_configured(self):
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("ui.server.get_db", side_effect=self.get_db),
            patch("ui.server._admin_storage_metrics", return_value=self.storage_metrics),
        ):
            os.environ.pop("FOIA_ADMIN_PASSWORD", None)
            status, body, _ = asyncio.run(
                asgi_get(server.app, "/admin")
            )

        self.assertEqual(status, 404)
        self.assertIn("Record or page not found", body)

    def test_admin_route_requires_basic_auth(self):
        with patch.dict(
            os.environ,
            {
                "FOIA_ADMIN_USERNAME": "operator",
                "FOIA_ADMIN_PASSWORD": "secret-value",
            },
            clear=False,
        ):
            status, _, headers = asyncio.run(
                asgi_get(server.app, "/admin")
            )

        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], "Basic")

    def test_admin_dashboard_renders_operational_and_storage_metrics(self):
        with (
            patch.dict(
                os.environ,
                {
                    "FOIA_ADMIN_USERNAME": "operator",
                    "FOIA_ADMIN_PASSWORD": "secret-value",
                    "FOIA_AUTONOMOUS_REFRESH_ENABLED": "false",
                },
                clear=False,
            ),
            patch("ui.server.get_db", side_effect=self.get_db),
            patch(
                "ui.server._admin_storage_metrics",
                return_value=dict(self.storage_metrics),
            ),
            patch("ui.server.DB_PATH", self.db_path),
        ):
            status, body, _ = asyncio.run(
                asgi_get(
                    server.app,
                    "/admin",
                    headers={
                        "authorization": basic(
                            "operator",
                            "secret-value",
                        )
                    },
                )
            )

        self.assertEqual(status, 200)
        self.assertIn("Administrator dashboard", body)
        self.assertIn("Backblaze B2 capacity", body)
        self.assertIn("1.9 GB", body)
        self.assertIn("403 / forbidden", body)
        self.assertIn("Demo Reading Room", body)
        self.assertIn("Administrator Demo Record", body)
        self.assertIn("Disabled", body)


if __name__ == "__main__":
    unittest.main()
