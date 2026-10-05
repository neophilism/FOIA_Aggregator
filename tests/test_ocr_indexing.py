import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter

from foia_archive.storage import (
    get_connection,
    get_document_text,
    init_db,
    insert_document,
    query_documents,
    upsert_document_text,
)
from foia_archive.text_extraction import (
    OCRSettings,
    OCRUnavailable,
    TextExtractionResult,
    extract_document_text,
)
from foia_archive.text_index import reindex_downloaded_documents
from foia_archive.utils import Config


class OCRSettingsTests(unittest.TestCase):
    def test_settings_are_bounded_and_parse_string_boolean(self):
        settings = OCRSettings.from_mapping(
            {
                "enabled": "true",
                "languages": "eng+spa",
                "dpi": 9999,
                "max_pages_per_document": 0,
                "page_timeout_seconds": 0,
                "max_image_megapixels": 999,
            }
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.languages, "eng+spa")
        self.assertEqual(settings.dpi, 400)
        self.assertEqual(settings.max_pages_per_document, 1)
        self.assertEqual(settings.page_timeout_seconds, 1.0)
        self.assertEqual(settings.max_image_megapixels, 100.0)


class OCRExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _blank_pdf(self, pages=1):
        path = self.root / "scan.pdf"
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=612, height=792)
        with path.open("wb") as target:
            writer.write(target)
        return path

    def test_scanned_pdf_uses_ocr_and_returns_searchable_text(self):
        path = self._blank_pdf()

        with patch(
            "foia_archive.text_extraction.pytesseract.image_to_string",
            return_value="Scanned constitutional restoration record",
        ) as ocr:
            result = extract_document_text(
                path,
                "pdf",
                ocr=OCRSettings(
                    enabled=True,
                    dpi=150,
                    max_pages_per_document=5,
                ),
            )

        self.assertEqual(result.status, "indexed")
        self.assertEqual(result.method, "ocr")
        self.assertIn("constitutional restoration", result.text)
        ocr.assert_called_once()

    def test_pdf_ocr_page_limit_marks_index_incomplete(self):
        path = self._blank_pdf(pages=2)

        with patch(
            "foia_archive.text_extraction.pytesseract.image_to_string",
            return_value="First scanned page",
        ) as ocr:
            result = extract_document_text(
                path,
                "pdf",
                ocr=OCRSettings(
                    enabled=True,
                    dpi=100,
                    max_pages_per_document=1,
                ),
            )

        self.assertEqual(result.status, "indexed_truncated")
        self.assertTrue(result.truncated)
        self.assertEqual(result.method, "ocr")
        self.assertIn("safety limit", result.error)
        ocr.assert_called_once()

    def test_image_file_is_ocr_indexed(self):
        path = self.root / "scan.png"
        Image.new("RGB", (500, 200), "white").save(path)

        with patch(
            "foia_archive.text_extraction.pytesseract.image_to_string",
            return_value="FOIA scanned image text",
        ):
            result = extract_document_text(
                path,
                "png",
                ocr=OCRSettings(enabled=True),
            )

        self.assertEqual(result.status, "indexed")
        self.assertEqual(result.method, "ocr")
        self.assertIn("scanned image", result.text)

    def test_missing_tesseract_is_explicit_not_extraction_failure(self):
        path = self._blank_pdf()

        with patch(
            "foia_archive.text_extraction._ocr_image",
            side_effect=OCRUnavailable("Tesseract unavailable"),
        ):
            result = extract_document_text(
                path,
                "pdf",
                ocr=OCRSettings(enabled=True),
            )

        self.assertEqual(result.status, "ocr_unavailable")
        self.assertEqual(result.method, "ocr")
        self.assertIn("Tesseract", result.error)

    def test_ocr_disabled_preserves_empty_pdf_classification(self):
        path = self._blank_pdf()

        result = extract_document_text(
            path,
            "pdf",
            ocr=OCRSettings(enabled=False),
        )

        self.assertEqual(result.status, "empty")
        self.assertIsNone(result.method)
        self.assertIn("disabled", result.error)


class OCRIndexPersistenceTests(unittest.TestCase):
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

    def test_ocr_method_is_persisted_and_body_is_fts_searchable(self):
        document_id = insert_document(
            self.conn,
            url="https://example.gov/scanned.pdf",
            title="Generic scanned release",
            file_type="pdf",
            filename="scanned.pdf",
            agency_id=None,
            office_id=None,
            reading_room_id=None,
            discovered_at="2026-10-05T00:00:00",
        )
        upsert_document_text(
            self.conn,
            document_id,
            body="needle phrase found only by optical recognition",
            extraction_status="indexed",
            extraction_method="ocr",
            extracted_at="2026-10-05T00:01:00",
        )

        row = get_document_text(self.conn, document_id)
        results = query_documents(
            self.conn,
            title_query="optical recognition",
        )

        self.assertEqual(row["extraction_method"], "ocr")
        self.assertEqual([item["id"] for item in results], [document_id])


class LegacyOCRCandidateReindexTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)
        self.config = Config(
            {
                "storage": {
                    "backend": "local",
                    "db_path": str(self.db_path),
                    "files_dir": str(self.files_dir),
                },
                "search": {
                    "max_indexed_chars_per_document": 100000,
                },
                "ocr": {
                    "enabled": True,
                },
            }
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_legacy_unsupported_image_is_reindexed_without_force(self):
        archive_path = self.files_dir / "legacy" / "scan.png"
        archive_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 100), "white").save(archive_path)

        conn = get_connection(self.db_path)
        try:
            document_id = insert_document(
                conn,
                url="https://example.gov/legacy-scan.png",
                title="Legacy scan",
                file_type="png",
                filename="scan.png",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-10-05T00:00:00",
            )
            conn.execute(
                """
                UPDATE documents
                SET download_status = 'downloaded',
                    local_path = ?,
                    storage_backend = 'local',
                    storage_key = ?
                WHERE id = ?
                """,
                ("legacy/scan.png", "legacy/scan.png", document_id),
            )
            conn.commit()
            upsert_document_text(
                conn,
                document_id,
                body="",
                extraction_status="unsupported",
                extraction_method=None,
                extracted_at="2026-10-05T00:01:00",
            )
        finally:
            conn.close()

        with patch(
            "foia_archive.text_index.extract_document_text",
            return_value=TextExtractionResult(
                status="indexed",
                text="legacy scan searchable after OCR upgrade",
                method="ocr",
                character_count=40,
            ),
        ) as extract:
            summary = reindex_downloaded_documents(self.config)

        self.assertEqual(summary.attempted, 1)
        self.assertEqual(summary.indexed, 1)
        extract.assert_called_once()

        conn = get_connection(self.db_path)
        try:
            row = get_document_text(conn, document_id)
            results = query_documents(
                conn,
                title_query="searchable after OCR",
            )
        finally:
            conn.close()

        self.assertEqual(row["extraction_method"], "ocr")
        self.assertEqual([item["id"] for item in results], [document_id])


if __name__ == "__main__":
    unittest.main()
