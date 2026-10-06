import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from foia_archive.storage import (
    get_archive_stats,
    get_connection,
    init_db,
    insert_document,
    upsert_agency,
    upsert_office,
    upsert_reading_room,
)
from ui import server


async def asgi_get(app, path="/", params=None, headers=None):
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

    raw_headers = [
        (str(name).lower().encode("latin-1"), str(value).encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
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
        "headers": raw_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    return start["status"], body.decode("utf-8"), response_headers


class PublicLaunchTests(unittest.TestCase):
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
            "Demo Reading Room",
            "office",
            agency_id,
            office_id,
        )
        self.document_id = insert_document(
            conn,
            url="https://example.gov/demo.pdf",
            title="Public Demo Record",
            file_type="pdf",
            filename="demo.pdf",
            agency_id=agency_id,
            office_id=office_id,
            reading_room_id=room_id,
            discovered_at="2026-10-05T21:34:28+00:00",
            published_date="2026-10-01",
        )
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def get_db(self):
        return get_connection(self.db_path)

    def test_archive_stats_expose_latest_activity_timestamp(self):
        conn = self.get_db()
        try:
            stats = get_archive_stats(conn)
        finally:
            conn.close()

        self.assertEqual(
            stats["latest_activity_at"],
            "2026-10-05T21:34:28+00:00",
        )

    def test_public_security_headers_are_present(self):
        with patch("ui.server.get_db", side_effect=self.get_db):
            status, _, headers = asyncio.run(
                asgi_get(server.app, "/healthz")
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(
            headers["referrer-policy"],
            "strict-origin-when-cross-origin",
        )
        self.assertIn("geolocation=()", headers["permissions-policy"])
        self.assertNotIn("strict-transport-security", headers)

    def test_hsts_is_added_for_forwarded_https(self):
        with patch("ui.server.get_db", side_effect=self.get_db):
            status, _, headers = asyncio.run(
                asgi_get(
                    server.app,
                    "/healthz",
                    headers={"x-forwarded-proto": "https"},
                )
            )

        self.assertEqual(status, 200)
        self.assertIn("max-age=31536000", headers["strict-transport-security"])

    def test_robots_and_sitemap_use_public_origin(self):
        with (
            patch("ui.server.get_db", side_effect=self.get_db),
            patch("ui.server.PUBLIC_BASE_URL", "https://records.example"),
        ):
            robots_status, robots_body, _ = asyncio.run(
                asgi_get(server.app, "/robots.txt")
            )
            sitemap_status, sitemap_body, sitemap_headers = asyncio.run(
                asgi_get(server.app, "/sitemap.xml")
            )

        self.assertEqual(robots_status, 200)
        self.assertIn("Disallow: /archive/", robots_body)
        self.assertIn(
            "Sitemap: https://records.example/sitemap.xml",
            robots_body,
        )

        self.assertEqual(sitemap_status, 200)
        self.assertIn("application/xml", sitemap_headers["content-type"])
        self.assertIn(
            "<loc>https://records.example/</loc>",
            sitemap_body,
        )
        self.assertIn(
            "<loc>https://records.example/about</loc>",
            sitemap_body,
        )
        self.assertIn(
            f"<loc>https://records.example/record/{self.document_id}</loc>",
            sitemap_body,
        )
        self.assertNotIn("/archive/", sitemap_body)

    def test_search_page_has_canonical_and_share_metadata(self):
        with (
            patch("ui.server.get_db", side_effect=self.get_db),
            patch("ui.server.PUBLIC_BASE_URL", "https://records.example"),
        ):
            status, body, _ = asyncio.run(
                asgi_get(
                    server.app,
                    "/",
                    {"q": "public demo"},
                )
            )

        self.assertEqual(status, 200)
        self.assertIn(
            'rel="canonical" href="https://records.example/?q=public+demo"',
            body,
        )
        self.assertIn('property="og:url"', body)
        self.assertIn('property="og:title"', body)
        self.assertIn("Archive updated Oct 5, 2026", body)

    def test_missing_page_uses_public_404_template(self):
        with patch("ui.server.get_db", side_effect=self.get_db):
            status, body, _ = asyncio.run(
                asgi_get(server.app, "/does-not-exist")
            )

        self.assertEqual(status, 404)
        self.assertIn("Record or page not found", body)
        self.assertIn("Search the archive", body)
        self.assertNotIn('"detail":"Not Found"', body)


if __name__ == "__main__":
    unittest.main()
