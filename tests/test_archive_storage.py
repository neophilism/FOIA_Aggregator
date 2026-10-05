import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

from foia_archive.archive_storage import (
    B2ArchiveStorage,
    LocalArchiveStorage,
    StorageQuotaReached,
    get_archive_storage,
)
from foia_archive.storage import (
    archived_remote_bytes,
    get_connection,
    init_db,
    insert_document,
    update_download_metadata,
)
from foia_archive.utils import Config


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def upload_file(self, Filename, Bucket, Key, **kwargs):
        data = Path(Filename).read_bytes()
        self.objects[(Bucket, Key)] = data
        self.uploads.append((Bucket, Key, kwargs))

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return (
            f"https://signed.example/{Params['Bucket']}/{Params['Key']}"
            f"?expires={ExpiresIn}"
        )


class ArchiveStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _b2(self, client=None, max_archive_bytes=9_000_000_000):
        return B2ArchiveStorage(
            bucket="foia-test",
            region="us-east-005",
            endpoint_url="https://s3.us-east-005.backblazeb2.com",
            key_id="test-key-id",
            application_key="test-application-key",
            max_archive_bytes=max_archive_bytes,
            client=client or FakeS3Client(),
        )

    def test_local_backend_preserves_relative_archive_path(self):
        files_dir = self.root / "files"
        source = files_dir / "aa" / "report.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"pdf")

        backend = LocalArchiveStorage(files_dir)
        location = backend.store_file(
            source,
            sha256="a" * 64,
            filename="report.pdf",
        )

        self.assertEqual(location.backend, "local")
        self.assertEqual(location.key, "aa/report.pdf")
        self.assertEqual(location.local_path, "aa/report.pdf")
        self.assertTrue(backend.exists(location.key))

    def test_b2_upload_is_content_addressed_verified_and_removes_stage_file(self):
        client = FakeS3Client()
        backend = self._b2(client=client)
        source = self.root / "report.pdf"
        source.write_bytes(b"released record")
        digest = "ab" + "1" * 62

        location = backend.store_file(
            source,
            sha256=digest,
            filename="report.pdf",
            mime_type="application/pdf",
            current_usage_bytes=0,
        )

        self.assertEqual(location.backend, "b2")
        self.assertEqual(
            location.key,
            f"documents/ab/{digest}.pdf",
        )
        self.assertIsNone(location.local_path)
        self.assertFalse(source.exists())
        self.assertEqual(len(client.uploads), 1)
        self.assertEqual(
            client.uploads[0][2]["ExtraArgs"]["ContentType"],
            "application/pdf",
        )
        self.assertTrue(backend.exists(location.key))

    def test_existing_content_addressed_b2_object_is_not_uploaded_twice(self):
        client = FakeS3Client()
        backend = self._b2(client=client)
        digest = "cd" + "2" * 62
        key = f"documents/cd/{digest}.pdf"
        client.objects[("foia-test", key)] = b"same"

        source = self.root / "same.pdf"
        source.write_bytes(b"same")
        location = backend.store_file(
            source,
            sha256=digest,
            filename="same.pdf",
            current_usage_bytes=4,
        )

        self.assertEqual(location.key, key)
        self.assertEqual(client.uploads, [])
        self.assertFalse(source.exists())

    def test_b2_safety_cap_blocks_new_object_without_deleting_stage_file(self):
        client = FakeS3Client()
        backend = self._b2(client=client, max_archive_bytes=10)
        source = self.root / "large.pdf"
        source.write_bytes(b"12345")

        with self.assertRaises(StorageQuotaReached):
            backend.store_file(
                source,
                sha256="ef" + "3" * 62,
                filename="large.pdf",
                current_usage_bytes=8,
            )

        self.assertTrue(source.exists())
        self.assertEqual(client.uploads, [])

    def test_b2_presigned_url_uses_private_object_key(self):
        backend = self._b2(client=FakeS3Client())
        url = backend.presigned_url("documents/aa/hash.pdf", expires_seconds=300)
        self.assertIn("foia-test/documents/aa/hash.pdf", url)
        self.assertIn("expires=300", url)

    def test_factory_reads_b2_credentials_from_environment(self):
        config = Config(
            {
                "storage": {
                    "backend": "b2",
                    "files_dir": str(self.root / "files"),
                    "b2": {
                        "bucket": "bucket",
                        "region": "region",
                        "endpoint_url": "https://endpoint.example",
                        "max_archive_bytes": 123,
                    },
                }
            }
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "B2_KEY_ID": "key-id",
                    "B2_APPLICATION_KEY": "secret",
                },
                clear=False,
            ),
            patch("foia_archive.archive_storage.boto3.client") as create_client,
        ):
            create_client.return_value = FakeS3Client()
            backend = get_archive_storage(config)

        self.assertIsInstance(backend, B2ArchiveStorage)
        self.assertEqual(backend.max_archive_bytes, 123)
        create_client.assert_called_once()

    def test_migration_backfills_existing_local_archive_location(self):
        db_path = self.root / "archive.db"
        files_dir = self.root / "files"
        init_db(db_path, files_dir)
        conn = get_connection(db_path)
        try:
            doc_id = insert_document(
                conn,
                url="https://example.gov/report.pdf",
                title="Report",
                file_type="pdf",
                filename="report.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-01-01T00:00:00",
            )
            update_download_metadata(
                conn,
                doc_id,
                "old/report.pdf",
                "2026-01-01T00:01:00",
                file_size=10,
            )
            row = conn.execute(
                """
                SELECT storage_backend, storage_key
                FROM documents WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["storage_backend"], "local")
        self.assertEqual(row["storage_key"], "old/report.pdf")

    def test_remote_usage_counts_one_content_addressed_object_once(self):
        db_path = self.root / "archive.db"
        files_dir = self.root / "files"
        init_db(db_path, files_dir)
        conn = get_connection(db_path)
        try:
            first = insert_document(
                conn,
                url="https://example.gov/a.pdf",
                title="A",
                file_type="pdf",
                filename="a.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-01-01T00:00:00",
            )
            second = insert_document(
                conn,
                url="https://example.gov/b.pdf",
                title="B",
                file_type="pdf",
                filename="b.pdf",
                agency_id=None,
                office_id=None,
                reading_room_id=None,
                discovered_at="2026-01-01T00:00:01",
            )
            for doc_id in (first, second):
                update_download_metadata(
                    conn,
                    doc_id,
                    None,
                    "2026-01-01T00:01:00",
                    file_size=50,
                    sha256="f" * 64,
                    storage_backend="b2",
                    storage_key="documents/ff/shared.pdf",
                )
            total = archived_remote_bytes(conn, "b2")
        finally:
            conn.close()

        self.assertEqual(total, 50)


if __name__ == "__main__":
    unittest.main()
