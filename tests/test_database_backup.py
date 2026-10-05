import gzip
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from foia_archive.archive_storage import ArchiveStorageError, B2ArchiveStorage
from foia_archive.database_backup import (
    backup_database_to_b2,
    database_backup_due,
    restore_database_from_b2,
)
from foia_archive.utils import Config


class FakeBackupS3Client:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        self.objects[(Bucket, Key)] = {
            "body": Path(Filename).read_bytes(),
            "metadata": dict((ExtraArgs or {}).get("Metadata") or {}),
            "content_type": (ExtraArgs or {}).get("ContentType"),
            "last_modified": self.now,
        }

    def head_object(self, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(item["body"]),
            "Metadata": item["metadata"],
        }

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        contents = []
        for (bucket, key), item in self.objects.items():
            if bucket == Bucket and key.startswith(Prefix):
                contents.append(
                    {
                        "Key": key,
                        "Size": len(item["body"]),
                        "LastModified": item["last_modified"],
                    }
                )
        return {"Contents": contents, "IsTruncated": False}

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)
        return {}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[(Bucket, Key)]["body"])


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "archive.db"
        self.files_dir = self.root / "files"
        self.files_dir.mkdir()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO sample (value) VALUES ('preserved')")
            conn.commit()
        finally:
            conn.close()

        self.config = Config(
            {
                "storage": {
                    "backend": "b2",
                    "db_path": str(self.db_path),
                    "files_dir": str(self.files_dir),
                },
                "database_backup": {
                    "enabled": True,
                    "interval_hours": 24,
                    "retain_count": 3,
                    "prefix": "database-backups",
                },
            }
        )
        self.client = FakeBackupS3Client()
        self.backend = B2ArchiveStorage(
            bucket="foia-test",
            region="us-east-005",
            endpoint_url="https://example.invalid",
            key_id="key",
            application_key="secret",
            client=self.client,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _patch_backend(self):
        return patch(
            "foia_archive.database_backup.get_archive_storage",
            return_value=self.backend,
        )

    def test_backup_creates_verified_gzipped_sqlite_snapshot(self):
        now = datetime(2026, 10, 5, 16, 30, tzinfo=timezone.utc)
        self.client.now = now

        with self._patch_backend():
            result = backup_database_to_b2(
                self.config,
                force=True,
                now=now,
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.key.startswith("database-backups/20261005T163000Z-"))
        item = self.client.objects[("foia-test", result.key)]
        self.assertEqual(item["content_type"], "application/gzip")
        self.assertIn("sqlite-sha256", item["metadata"])

        restored_bytes = gzip.decompress(item["body"])
        restored_path = self.root / "inspection.db"
        restored_path.write_bytes(restored_bytes)
        conn = sqlite3.connect(restored_path)
        try:
            value = conn.execute("SELECT value FROM sample").fetchone()[0]
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(value, "preserved")
        self.assertEqual(integrity, "ok")

    def test_backup_due_uses_latest_remote_backup_timestamp(self):
        latest = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        self.client.objects[("foia-test", "database-backups/existing.sqlite.gz")] = {
            "body": b"x",
            "metadata": {},
            "content_type": "application/gzip",
            "last_modified": latest,
        }

        with self._patch_backend():
            self.assertFalse(
                database_backup_due(
                    self.config,
                    now=latest + timedelta(hours=23),
                )
            )
            self.assertTrue(
                database_backup_due(
                    self.config,
                    now=latest + timedelta(hours=24),
                )
            )

    def test_retention_keeps_only_newest_three_backups(self):
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for index in range(3):
            self.client.objects[
                ("foia-test", f"database-backups/old-{index}.sqlite.gz")
            ] = {
                "body": bytes([index]),
                "metadata": {},
                "content_type": "application/gzip",
                "last_modified": base + timedelta(days=index),
            }

        now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        self.client.now = now
        with self._patch_backend():
            backup_database_to_b2(self.config, force=True, now=now)

        remaining = sorted(
            key
            for bucket, key in self.client.objects
            if bucket == "foia-test" and key.startswith("database-backups/")
        )
        self.assertEqual(len(remaining), 3)
        self.assertNotIn("database-backups/old-0.sqlite.gz", remaining)
        self.assertEqual(len(self.client.deleted), 1)

    def test_restore_defaults_to_newest_backup_and_preserves_live_database(self):
        first_time = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)
        second_time = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)

        with self._patch_backend():
            self.client.now = first_time
            first = backup_database_to_b2(
                self.config,
                force=True,
                now=first_time,
            )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE sample SET value = 'newer'")
            conn.commit()
        finally:
            conn.close()

        with self._patch_backend():
            self.client.now = second_time
            second = backup_database_to_b2(
                self.config,
                force=True,
                now=second_time,
            )
            restored = restore_database_from_b2(self.config)

        self.assertNotEqual(first.key, second.key)
        self.assertEqual(
            restored,
            self.db_path.with_name("archive.restored.db"),
        )
        self.assertTrue(self.db_path.exists())

        conn = sqlite3.connect(restored)
        try:
            value = conn.execute("SELECT value FROM sample").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "newer")

    def test_restore_rejects_sha256_mismatch(self):
        now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        with self._patch_backend():
            self.client.now = now
            result = backup_database_to_b2(
                self.config,
                force=True,
                now=now,
            )

        item = self.client.objects[("foia-test", result.key)]
        item["metadata"]["sqlite-sha256"] = "0" * 64

        with self._patch_backend():
            with self.assertRaisesRegex(ArchiveStorageError, "SHA-256 mismatch"):
                restore_database_from_b2(
                    self.config,
                    key=result.key,
                    destination_path=self.root / "bad.db",
                )


if __name__ == "__main__":
    unittest.main()
