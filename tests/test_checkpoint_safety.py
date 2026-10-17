"""Credential-free checkpoint transport, restore and retention regressions."""
import gzip
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError, SSLError
import test_database_backup as fixtures
from foia_archive.archive_storage import ArchiveStorageError
from foia_archive.database_backup import (
    _download_checkpoint, _list_backup_objects, _native_json, _native_upload_backup,
    _native_url, _permanently_delete_object, _unpack_checkpoint,
    backup_database_to_b2, restore_database_from_b2,
)


class CheckpointSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DatabaseBackupTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client, self.backend = self.fixture.client, self.fixture.backend
        self.root, self.config = self.fixture.root, self.fixture.config

    def backup(self):
        with self.fixture._patch_backend():
            return backup_database_to_b2(self.config, force=True)

    def test_readback_precedes_any_retention_delete(self):
        first = self.backup()
        self.config.data["database_backup"]["retain_count"] = 1
        events = []
        get, delete = self.client.get_object, self.client.delete_object
        def read(**kwargs):
            events.append("read")
            return get(**kwargs)
        def remove(**kwargs):
            events.append("delete")
            return delete(**kwargs)
        with patch.object(self.client, "get_object", side_effect=read), patch.object(self.client, "delete_object", side_effect=remove):
            # Give the next checkpoint a later timestamp, preserving old retention evidence.
            from datetime import timedelta
            self.client.now = first.created_at + timedelta(days=1)
            with self.fixture._patch_backend():
                backup_database_to_b2(self.config, force=True, now=first.created_at + timedelta(days=1))
        self.assertLess(events.index("read"), events.index("delete"))

    def test_same_second_identical_snapshots_use_fresh_object_keys(self):
        first = self.backup()
        with self.fixture._patch_backend():
            second = backup_database_to_b2(self.config, force=True, now=first.created_at)
        self.assertNotEqual(first.key, second.key)
        self.assertIn((self.backend.bucket, first.key), self.client.objects)
        self.assertIn((self.backend.bucket, second.key), self.client.objects)

    def test_stale_retention_listing_cannot_prune_old_points(self):
        with patch("foia_archive.database_backup._list_backup_objects", return_value=[]), patch.object(self.client, "delete_object") as delete:
            self.backup()  # Verified upload survives; pruning is safely deferred.
        delete.assert_not_called()

    def test_failed_readback_never_prunes_or_removes_local_database(self):
        with patch.object(self.client, "get_object", side_effect=OSError("fixture transport failure")), patch.object(self.client, "delete_object") as delete:
            with self.assertRaises(ArchiveStorageError):
                self.backup()
        delete.assert_not_called()
        self.assertTrue(self.fixture.db_path.exists())

    def test_corrupt_uploaded_object_never_prunes(self):
        original = self.client.get_object
        def corrupt(**kwargs):
            result = original(**kwargs)
            result["Body"].close()
            result["Body"] = io.BytesIO(b"x" * result["ContentLength"])
            return result
        with patch.object(self.client, "get_object", side_effect=corrupt), patch.object(self.client, "delete_object") as delete:
            with self.assertRaises(ArchiveStorageError):
                self.backup()
        delete.assert_not_called()

    def test_wrong_full_hash_never_prunes(self):
        original = self.client.get_object
        def wrong_hash(**kwargs):
            result = original(**kwargs)
            result["Metadata"] = {"sqlite-sha256": "b" * 64}
            return result
        with patch.object(self.client, "get_object", side_effect=wrong_hash), patch.object(self.client, "delete_object") as delete:
            with self.assertRaisesRegex(ArchiveStorageError, "SHA-256"):
                self.backup()
        delete.assert_not_called()

    def test_backup_and_restore_do_not_use_head_or_transfer_download(self):
        with patch.object(self.client, "head_object", side_effect=AssertionError("HEAD forbidden")) as head, patch.object(self.client, "download_file", side_effect=AssertionError("transfer download forbidden")) as transfer:
            result = self.backup()
            with self.fixture._patch_backend():
                restored = restore_database_from_b2(self.config, key=result.key)
        self.assertTrue(restored.exists())
        head.assert_not_called()
        transfer.assert_not_called()

    def test_restore_byte_limits_reject_before_upload(self):
        for field in ("max_compressed_bytes", "max_restored_bytes"):
            with self.subTest(field=field):
                self.config.data["database_backup"][field] = 1
                with self.assertRaisesRegex(ArchiveStorageError, "no upload"):
                    self.backup()
                self.config.data["database_backup"].pop(field)
        self.assertEqual(self.client.version_counter, 0)

    def test_gzip_expansion_is_bounded(self):
        packed, restored = self.root / "bomb.gz", self.root / "expanded.db"
        packed.write_bytes(gzip.compress(b"a" * 100000))
        with self.assertRaisesRegex(ArchiveStorageError, "byte limit"):
            _unpack_checkpoint(packed, restored, "fixture.sqlite.gz", 20)
        self.assertLessEqual(restored.stat().st_size, 20)

    def test_download_rejects_oversize_truncated_and_extra_bytes_and_closes_stream(self):
        for body_bytes, declared, limit in ((b"abc", 10, 5), (b"abc", 4, 5), (b"abcd", 3, 5)):
            with self.subTest(declared=declared):
                body = io.BytesIO(body_bytes)
                with patch.object(self.client, "get_object", return_value={"Body": body, "ContentLength": declared}):
                    with self.assertRaises(ArchiveStorageError):
                        _download_checkpoint(self.backend, "fixture", self.root / "read.gz", limit)
                self.assertTrue(body.closed)

    def test_failed_restore_keeps_existing_destination(self):
        result = self.backup()
        destination = self.root / "retained.db"
        destination.write_bytes(b"previous verified snapshot")
        self.client.objects[(self.backend.bucket, result.key)]["body"] = b"bad gzip"
        with self.fixture._patch_backend(), self.assertRaises(ArchiveStorageError):
            restore_database_from_b2(self.config, key=result.key, destination_path=destination, overwrite=True)
        self.assertEqual(destination.read_bytes(), b"previous verified snapshot")

    def test_failed_atomic_replace_keeps_existing_destination(self):
        result = self.backup()
        destination = self.root / "retained.db"
        destination.write_bytes(b"previous verified snapshot")
        with self.fixture._patch_backend(), patch.object(Path, "replace", side_effect=OSError("fixture rename failure")):
            with self.assertRaises(OSError):
                restore_database_from_b2(self.config, key=result.key, destination_path=destination, overwrite=True)
        self.assertEqual(destination.read_bytes(), b"previous verified snapshot")

    def test_restore_refuses_existing_sqlite_sidecars(self):
        result = self.backup()
        destination = self.root / "in-use.db"
        destination.write_bytes(b"retained")
        Path(str(destination) + "-wal").write_bytes(b"retained WAL")
        with self.fixture._patch_backend(), self.assertRaisesRegex(ArchiveStorageError, "sidecars"):
            restore_database_from_b2(self.config, key=result.key, destination_path=destination, overwrite=True)
        self.assertEqual(destination.read_bytes(), b"retained")

    def test_incomplete_and_cyclic_listings_fail_closed(self):
        for pages in ([{"IsTruncated": True}], [{"IsTruncated": True, "NextContinuationToken": "same"}] * 2):
            with patch.object(self.client, "list_objects_v2", side_effect=pages):
                with self.assertRaises(ArchiveStorageError):
                    _list_backup_objects(self.backend, "database-backups/")

    def test_incomplete_version_inventory_never_deletes_partial_results(self):
        with patch.object(self.client, "list_object_versions", return_value={"IsTruncated": True, "Versions": [{"Key": "fixture", "VersionId": "1"}]}), patch.object(self.client, "delete_object") as delete:
            with self.assertRaises(ArchiveStorageError):
                _permanently_delete_object(self.backend, "fixture")
        delete.assert_not_called()

    def test_partial_object_manifest_is_not_absence_evidence(self):
        with patch.object(self.client, "list_objects_v2", return_value={"IsTruncated": True}), self.assertRaises(ArchiveStorageError):
            self.backend.current_object_manifest(prefix="fixture")

    def test_partial_version_usage_cannot_report_false_headroom(self):
        with patch.object(self.client, "list_object_versions", return_value={"IsTruncated": True}), self.assertRaises(ArchiveStorageError):
            self.backend.usage_summary()

    def test_checkpoint_client_disables_sdk_retries_without_changing_document_client(self):
        from foia_archive.archive_storage import B2ArchiveStorage
        with patch("foia_archive.archive_storage.boto3.client", side_effect=[object(), object()]) as factory:
            backend = B2ArchiveStorage(bucket="fixture", region="us-east-005", endpoint_url="https://s3.us-east-005.backblazeb2.com", key_id="fixture-key", application_key="fixture-value")
        self.assertIsNot(backend.client, backend.checkpoint_client)
        self.assertNotIn("config", factory.call_args_list[0].kwargs)
        self.assertEqual(factory.call_args_list[1].kwargs["config"].retries["total_max_attempts"], 1)

    def test_real_sdk_makes_one_http_attempt_before_returning_control(self):
        # Loopback-only server, explicit fake keys and a clean environment. This
        # exercises the SDK retry machinery rather than mocking upload_file.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from foia_archive.archive_storage import B2ArchiveStorage
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                calls.append("PUT")
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = b"<Error><Code>InternalError</Code><Message>fixture failure</Message></Error>"
                self.send_response(500)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {}, clear=True):
                backend = B2ArchiveStorage(bucket="fixture", region="us-east-005", endpoint_url=f"http://127.0.0.1:{server.server_port}", key_id="fixture-key", application_key="fixture-value")
                with self.assertRaises(ClientError):
                    backend.checkpoint_client.upload_file(str(self.fixture.db_path), "fixture", "fixture.sqlite")
            self.assertEqual(calls, ["PUT"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_checkpoint_never_enters_multipart_upload(self):
        original = self.client.upload_file
        with patch.object(self.client, "upload_file", wraps=original) as upload:
            self.backup()
        self.assertGreater(upload.call_args.kwargs["Config"].multipart_threshold, 5 * 1024 * 1024 * 1024)

    def test_s3_auth_failure_never_retries_or_falls_back(self):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "fixture-secret"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
        with patch.object(self.client, "upload_file", side_effect=error) as upload, patch("foia_archive.database_backup._native_upload_backup") as native:
            with self.assertRaises(ArchiveStorageError) as raised:
                self.backup()
        self.assertNotIn("fixture-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(upload.call_count, 1)
        native.assert_not_called()

    def test_native_fallback_requires_three_tls_failures_not_just_the_last(self):
        tls = SSLError(endpoint_url="https://fixture.invalid", error=OSError("TLS EOF"))
        with patch.dict(os.environ, {"B2_KEY_ID": "fixture-key", "B2_APPLICATION_KEY": "fixture-value"}), patch.object(self.client, "upload_file", side_effect=[OSError("other"), tls, tls]), patch("foia_archive.database_backup._native_upload_backup") as native, patch("foia_archive.database_backup.time.sleep"):
            with self.assertRaises(ArchiveStorageError):
                self.backup()
        native.assert_not_called()


class NativeEndpointTests(unittest.TestCase):
    def test_rejects_nonprovider_credentials_ports_controls_and_lookalikes(self):
        for url in ("https://evil.invalid/b2api/v4/b2_upload_file", "http://pod.backblaze.com/b2api/v4/b2_upload_file", "https://pod.backblaze.com.evil.invalid/b2api/v4/b2_upload_file", "https://user@pod.backblaze.com/b2api/v4/b2_upload_file", "https://pod.backblaze.com:444/b2api/v4/b2_upload_file", "https://pod.backblaze.com/b2api/v4/b2_upload_file#fragment", "https://pod.backblaze.com/other", "https://pod.backblaze.com/\n", "https://pod.backblaze.com\\evil/b2api/v4/b2_upload_file"):
            with self.subTest(url=url), self.assertRaises(ArchiveStorageError):
                _native_url(url)

    def test_accepts_documented_provider_url_shapes(self):
        self.assertEqual(_native_url("https://api005.backblazeb2.com/", api=True), "https://api005.backblazeb2.com")
        url = "https://pod-000-1005-03.backblaze.com/b2api/v4/b2_upload_file?cvt=fixture&bucket=fixture"
        self.assertEqual(_native_url(url), url)

    def test_redirect_and_auth_responses_are_terminal_not_success(self):
        for status in (301, 302, 307, 308, 400, 401, 403):
            response = type("Response", (), {"status_code": status})()
            with self.subTest(status=status), self.assertRaises(ArchiveStorageError):
                _native_json(response)

    def test_native_authorization_does_not_follow_redirect_or_disable_tls(self):
        response = type("Response", (), {"status_code": 302})()
        with patch.dict(os.environ, {"B2_KEY_ID": "fixture-key", "B2_APPLICATION_KEY": "fixture-value"}), patch("foia_archive.database_backup.requests.get", return_value=response) as get, patch("foia_archive.database_backup.requests.post") as post:
            with self.assertRaises(ArchiveStorageError):
                _native_upload_backup(None, None, "fixture", "a" * 64)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.kwargs["verify"])
        post.assert_not_called()
