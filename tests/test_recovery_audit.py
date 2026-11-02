import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from foia_archive import models
from foia_archive.recovery_audit import compare_snapshots, describe_database


class RecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before = self.root / "before.db"
        self.after = self.root / "after #? evidence.db"
        self.key = "documents/aa/" + "a" * 64 + ".pdf"
        self.manifest = {self.key: 42}
        for path in (self.before, self.after):
            with sqlite3.connect(path) as conn:
                for schema in (models.AGENCIES_TABLE, models.OFFICES_TABLE, models.READING_ROOMS_TABLE,
                               models.DOCUMENTS_TABLE, models.SCHEMA_MIGRATIONS_TABLE,
                               models.DOCUMENT_SOURCES_TABLE, models.DOCUMENT_TEXT_TABLE, models.DOCUMENT_FTS_TABLE):
                    conn.execute(schema)
                conn.executemany("INSERT INTO schema_migrations VALUES (?,?,?)", [(n, "fixture", "2026-01-01") for n in range(1, 7)])
                conn.execute("INSERT INTO agencies(id,slug,name) VALUES (1,'agency','Agency')")
                conn.execute("INSERT INTO offices(id,slug,agency_id) VALUES (1,'office',1)")
                conn.execute("INSERT INTO reading_rooms(id,url,agency_id,office_id) VALUES (1,'https://fixture.gov/library',1,1)")
                conn.execute("""INSERT INTO documents(id,url,download_status,storage_key,storage_backend,sha256,file_size,agency_id,office_id,reading_room_id)
                    VALUES (1,'https://fixture.gov/record.pdf','downloaded',?,'b2',?,42,1,1,1)""", (self.key, "a" * 64))
                conn.execute("INSERT INTO document_sources(document_id,reading_room_id) VALUES(1,1)")
                conn.execute("INSERT INTO document_text(document_id,body,extraction_status,extraction_method,character_count,truncated) VALUES(1,'preserved private fixture text','indexed','native_text',30,0)")
                conn.execute("INSERT INTO document_fts(rowid,body) VALUES(1,'preserved private fixture text')")

    def change(self, sql, values=()):
        with sqlite3.connect(self.after) as conn:
            conn.execute(sql, values)

    def report(self):
        return compare_snapshots(describe_database(self.before), describe_database(self.after), self.manifest)

    def test_identical_snapshots_pass_and_summary_contains_no_body_or_urls(self):
        report = self.report()
        self.assertTrue(report["candidate_appears_safe_for_archived_objects"])
        self.assertNotIn("private fixture text", json.dumps(report))
        self.assertNotIn("https://", json.dumps(report))

    def test_superset_and_metadata_enrichment_pass(self):
        self.change("INSERT INTO documents(url) VALUES('https://fixture.gov/new.pdf')")
        self.change("UPDATE documents SET title='Newly known title'")
        self.assertTrue(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_more_rows_cannot_hide_a_lost_url(self):
        self.change("UPDATE documents SET url='https://fixture.gov/replacement.pdf'")
        self.change("INSERT INTO documents(url) VALUES('https://fixture.gov/extra.pdf')")
        report = self.report()
        self.assertEqual(report["b2_backup_document_urls_missing_from_candidate"], 1)
        self.assertFalse(report["candidate_appears_safe_for_archived_objects"])

    def test_lost_secondary_source_link_blocks_same_document_and_key_sets(self):
        self.change("DELETE FROM document_sources")
        self.assertEqual(self.report()["missing_snapshot_evidence"]["document_sources"], 1)
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_lost_source_or_reassigned_agency_blocks(self):
        self.change("UPDATE reading_rooms SET agency_id=NULL")
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_lost_extracted_text_blocks(self):
        self.change("DELETE FROM document_text")
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_changed_text_blocks_without_printing_it(self):
        self.change("UPDATE document_text SET body='different text'")
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_lost_search_row_blocks(self):
        self.change("DELETE FROM document_fts")
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_changed_content_hash_blocks(self):
        self.change("UPDATE documents SET sha256=?", ("b" * 64,))
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_swapped_object_keys_cannot_hide_a_lost_record_association(self):
        for path in (self.before, self.after):
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO documents(url,download_status,storage_backend,storage_key) VALUES('https://fixture.gov/two.pdf','downloaded','b2','documents/two.pdf')")
        self.manifest["documents/two.pdf"] = 42
        self.change("UPDATE documents SET storage_key=CASE id WHEN 1 THEN 'documents/two.pdf' ELSE ? END", (self.key,))
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_downloaded_record_cannot_silently_become_pending(self):
        self.change("UPDATE documents SET download_status='pending'")
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_missing_object_blocks(self):
        self.manifest.clear()
        self.assertEqual(self.report()["candidate_keys_missing_from_b2"], 1)
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_object_size_mismatch_blocks(self):
        self.manifest[self.key] = 41
        self.assertEqual(self.report()["candidate_object_size_mismatches"], 1)
        self.assertFalse(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_surrogate_id_changes_preserve_semantic_evidence(self):
        with sqlite3.connect(self.after) as conn:
            for table, column in (("documents", "id"), ("document_sources", "document_id"), ("document_text", "document_id"), ("document_fts", "rowid")):
                conn.execute(f"UPDATE {table} SET {column}=99")
        self.assertTrue(self.report()["candidate_appears_safe_for_archived_objects"])

    def test_schema_regression_fails_closed(self):
        self.change("DELETE FROM schema_migrations WHERE version=6")
        with self.assertRaises(RuntimeError):
            describe_database(self.after)

    def test_orphaned_relationship_fails_closed(self):
        self.change("UPDATE document_sources SET reading_room_id=999")
        with self.assertRaises(RuntimeError):
            describe_database(self.after)

    def test_missing_file_is_not_created(self):
        missing = self.root / "missing.db"
        with self.assertRaises(FileNotFoundError):
            describe_database(missing)
        self.assertFalse(missing.exists())

    def test_corrupt_database_fails_closed(self):
        self.after.write_bytes(b"not SQLite")
        with self.assertRaises(sqlite3.DatabaseError):
            describe_database(self.after)

    def test_read_only_audit_does_not_change_input_bytes(self):
        before = hashlib.sha256(self.after.read_bytes()).hexdigest()
        self.report()
        self.assertEqual(hashlib.sha256(self.after.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
