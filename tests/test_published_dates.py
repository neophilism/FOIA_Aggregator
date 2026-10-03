import sqlite3
import unittest

from foia_archive import models
from foia_archive.scraper_core import extract_document_links
from foia_archive.storage import (
    insert_document,
    query_documents,
    update_document_published_date_if_missing,
)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for statement in (
            models.AGENCIES_TABLE,
            models.OFFICES_TABLE,
            models.READING_ROOMS_TABLE,
            models.DOCUMENTS_TABLE,
        ):
            self.conn.execute(statement)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def insert(self, slug, published_date=None):
        return insert_document(
            self.conn,
            url=f"https://example.gov/{slug}.pdf",
            title=slug,
            file_type="pdf",
            filename=f"{slug}.pdf",
            agency_id=None,
            office_id=None,
            reading_room_id=None,
            discovered_at=f"2024-03-{len(slug):02d}T00:00:00",
            published_date=published_date,
        )


class PublishedDateExtractionTests(unittest.TestCase):
    def test_extracts_labeled_table_publication_date(self):
        html = """
        <table>
          <thead><tr><th>Document</th><th>Published</th></tr></thead>
          <tbody>
            <tr><td><a href="/records/a.pdf">Record A</a></td><td>May 6, 2024</td></tr>
          </tbody>
        </table>
        """
        links = extract_document_links(html, "https://example.gov/reading-room/")
        self.assertEqual(links[0]["published_date"], "2024-05-06")

    def test_extracts_explicit_publication_time_metadata(self):
        html = """
        <article>
          <a href="/records/b.pdf">Record B</a>
          <time itemprop="datePublished" datetime="2023-11-02T14:30:00Z">November 2, 2023</time>
        </article>
        """
        links = extract_document_links(html, "https://example.gov/")
        self.assertEqual(links[0]["published_date"], "2023-11-02")

    def test_plain_nearby_date_is_not_invented_as_publication_date(self):
        html = """
        <li>
          <a href="/records/c.pdf">Record C</a>
          <span>May 6, 2024</span>
        </li>
        """
        links = extract_document_links(html, "https://example.gov/")
        self.assertIsNone(links[0]["published_date"])


class PublishedDateStorageTests(DatabaseTestCase):
    def test_unknown_date_is_stored_as_null(self):
        self.insert("unknown")
        row = self.conn.execute(
            "SELECT published_date FROM documents WHERE title = 'unknown'"
        ).fetchone()
        self.assertIsNone(row["published_date"])

    def test_backfill_only_sets_missing_date(self):
        self.insert("missing")
        self.insert("known", "2020-01-01")

        self.assertTrue(
            update_document_published_date_if_missing(
                self.conn, "https://example.gov/missing.pdf", "2024-05-06"
            )
        )
        self.assertFalse(
            update_document_published_date_if_missing(
                self.conn, "https://example.gov/known.pdf", "2024-05-06"
            )
        )

        rows = {
            row["title"]: row["published_date"]
            for row in self.conn.execute(
                "SELECT title, published_date FROM documents ORDER BY title"
            ).fetchall()
        }
        self.assertEqual(rows["missing"], "2024-05-06")
        self.assertEqual(rows["known"], "2020-01-01")


class PublishedDateFilterTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.insert("january", "2024-01-10")
        self.insert("february", "2024-02-20")
        self.insert("undated")

    def titles(self, start_date=None, end_date=None):
        return {
            row["title"]
            for row in query_documents(
                self.conn,
                start_date=start_date,
                end_date=end_date,
            )
        }

    def test_no_date_filter_keeps_unknown_dates_visible(self):
        self.assertEqual(self.titles(), {"january", "february", "undated"})

    def test_start_date_is_inclusive_and_excludes_unknown_dates(self):
        self.assertEqual(self.titles(start_date="2024-02-20"), {"february"})

    def test_end_date_is_inclusive_and_excludes_unknown_dates(self):
        self.assertEqual(self.titles(end_date="2024-01-10"), {"january"})

    def test_start_and_end_date_bound_the_known_date_range(self):
        self.assertEqual(
            self.titles(start_date="2024-01-01", end_date="2024-01-31"),
            {"january"},
        )


if __name__ == "__main__":
    unittest.main()
