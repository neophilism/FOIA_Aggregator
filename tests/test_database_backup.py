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
    bootstrap_database_from_b2,
    database_backup_due,
    restore_database_from_b2,
)
from foia_archive.utils import Config


class FakeBackupS3Client:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.version_counter = 0
        self.now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
        self.version_counter += 1
        self.objects[(Bucket, Key)] = {
            "body": Path(Filename).read_bytes(),
            "metadata": dict((ExtraArgs or {}).get("Metadata") or {}),
            "content_type": (ExtraArgs or {}).get("ContentType"),
            "last_modified": self.now,
            "version_id": f"v{self.version_counter}",
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

    def list_object_versions(
        self,
        Bucket,
        Prefix,
        KeyMarker=None,
        VersionIdMarker=None,
    ):
        versions = []
        for (bucket, key), item in self.objects.items():
            if bucket == Bucket and key.startswith(Prefix):
                versions.append(
                    {
                        "Key": key,
                        "VersionId": item["version_id"],
                        "LastModified": item["last_modified"],
                    }
                )
        return {
            "Versions": versions,
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def delete_object(self, Bucket, Key, VersionId=None):
        self.deleted.append((Bucket, Key, VersionId))
        item = self.objects.get((Bucket, Key))
        if item is not None and item.get("version_id") == VersionId:
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

    def test_checkpoint_upload_recovers_from_transient_tls_failure(self):
        now = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)
        self.client.now = now
        original_upload = self.client.upload_file
        attempts = []

        def fail_once(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("simulated TLS EOF before upload")
            return original_upload(*args, **kwargs)

        with self._patch_backend(), patch.object(
            self.client, "upload_file", side_effect=fail_once
        ), patch("foia_archive.database_backup.time.sleep"):
            result = backup_database_to_b2(self.config, force=True, now=now)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.client.version_counter, 1)
        self.assertIn(("foia-test", result.key), self.client.objects)

    def test_lost_success_response_does_not_upload_duplicate_version(self):
        now = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)
        self.client.now = now
        original_upload = self.client.upload_file
        attempts = []

        def upload_then_lose_response(*args, **kwargs):
            attempts.append(1)
            original_upload(*args, **kwargs)
            raise OSError("simulated TLS EOF after successful upload")

        with self._patch_backend(), patch.object(
            self.client, "upload_file", side_effect=upload_then_lose_response
        ), patch("foia_archive.database_backup.time.sleep"):
            result = backup_database_to_b2(self.config, force=True, now=now)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.client.version_counter, 1)
        self.assertIn(("foia-test", result.key), self.client.objects)

    def test_unknown_upload_state_fails_closed_without_blind_retry(self):
        now = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)
        with self._patch_backend(), patch.object(
            self.client, "upload_file", side_effect=OSError("TLS EOF")
        ) as upload, patch.object(
            self.backend, "current_object_manifest",
            side_effect=OSError("B2 listing also failed"),
        ):
            with self.assertRaisesRegex(ArchiveStorageError, "status is unknown"):
                backup_database_to_b2(self.config, force=True, now=now)
        self.assertEqual(upload.call_count, 1)
        self.assertTrue(self.db_path.is_file())

    def test_native_fallback_after_repeated_s3_tls_failures(self):
        import os
        from botocore.exceptions import SSLError

        tls_error = SSLError(
            endpoint_url="https://s3.example.invalid", error=OSError("TLS EOF")
        )
        with self._patch_backend(), patch.object(
            self.client, "upload_file", side_effect=tls_error
        ) as upload, patch(
            "foia_archive.database_backup._native_upload_backup"
        ) as native, patch(
            "foia_archive.database_backup.time.sleep"
        ), patch.dict(
            os.environ, {"B2_KEY_ID": "key", "B2_APPLICATION_KEY": "value"}
        ):
            backup_database_to_b2(self.config, force=True)

        self.assertEqual(upload.call_count, 3)
        native.assert_called_once()

    def test_backup_due_uses_latest_remote_backup_timestamp(self):
        latest = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        self.client.objects[("foia-test", "database-backups/existing.sqlite.gz")] = {
            "body": b"x",
            "metadata": {},
            "content_type": "application/gzip",
            "last_modified": latest,
            "version_id": "existing-version",
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
                "version_id": f"old-version-{index}",
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
        deleted_bucket, deleted_key, deleted_version = self.client.deleted[0]
        self.assertEqual(deleted_bucket, "foia-test")
        self.assertEqual(deleted_key, "database-backups/old-0.sqlite.gz")
        self.assertEqual(deleted_version, "old-version-0")
        self.assertIsNotNone(deleted_version)

    def test_retention_uses_version_ids_so_old_bytes_are_permanently_removed(self):
        old_key = "database-backups/old.sqlite.gz"
        self.client.objects[("foia-test", old_key)] = {
            "body": b"old",
            "metadata": {},
            "content_type": "application/gzip",
            "last_modified": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "version_id": "old-version",
        }

        now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        self.client.now = now
        self.config.data["database_backup"]["retain_count"] = 1

        with self._patch_backend():
            backup_database_to_b2(self.config, force=True, now=now)

        self.assertNotIn(("foia-test", old_key), self.client.objects)
        self.assertIn(
            ("foia-test", old_key, "old-version"),
            self.client.deleted,
        )
        self.assertTrue(
            all(version_id is not None for _, _, version_id in self.client.deleted)
        )

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

    def test_bootstrap_restores_newest_backup_when_live_db_is_absent(self):
        now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        with self._patch_backend():
            self.client.now = now
            backup = backup_database_to_b2(
                self.config,
                force=True,
                now=now,
            )

        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

        with self._patch_backend():
            restored = bootstrap_database_from_b2(self.config)

        self.assertEqual(restored, self.db_path)
        self.assertTrue(self.db_path.exists())
        conn = sqlite3.connect(self.db_path)
        try:
            value = conn.execute("SELECT value FROM sample").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "preserved")
        self.assertEqual(
            backup.key,
            next(
                key
                for bucket, key in self.client.objects
                if bucket == "foia-test"
                and key.startswith("database-backups/")
            ),
        )

    def test_bootstrap_never_overwrites_existing_live_database(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE sample SET value = 'live'")
            conn.commit()
        finally:
            conn.close()

        with patch(
            "foia_archive.database_backup.list_database_backups"
        ) as listing:
            restored = bootstrap_database_from_b2(self.config)

        self.assertIsNone(restored)
        listing.assert_not_called()

        conn = sqlite3.connect(self.db_path)
        try:
            value = conn.execute("SELECT value FROM sample").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "live")

    def test_restore_rejects_sha256_mismatch_without_head_metadata(self):
        now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        with self._patch_backend():
            self.client.now = now
            result = backup_database_to_b2(
                self.config,
                force=True,
                now=now,
            )

        item = self.client.objects[("foia-test", result.key)]
        restored_bytes = bytearray(gzip.decompress(item["body"]))
        restored_bytes[-1:] = b"X"
        item["body"] = gzip.compress(bytes(restored_bytes), mtime=0)

        with self._patch_backend():
            with self.assertRaisesRegex(ArchiveStorageError, "SHA-256 mismatch"):
                restore_database_from_b2(
                    self.config,
                    key=result.key,
                    destination_path=self.root / "bad.db",
                )


if __name__ == "__main__":
    unittest.main()
