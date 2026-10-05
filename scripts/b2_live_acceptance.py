"""Live Backblaze B2 acceptance test for the FOIA archive.

This script intentionally reads credentials only from environment variables.
It never prints secrets or presigned URLs.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError

from foia_archive.archive_storage import B2ArchiveStorage
from foia_archive.database_backup import (
    backup_database_to_b2,
    list_database_backups,
    restore_database_from_b2,
)
from foia_archive.utils import Config


AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"
REQUIRED_CAPABILITIES = {
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
}


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _authorize(key_id: str, application_key: str) -> tuple[dict | None, str | None]:
    response = requests.get(
        AUTHORIZE_URL,
        auth=(key_id, application_key),
        timeout=30,
    )
    if response.status_code == 401:
        return None, "native_401"
    response.raise_for_status()
    return response.json(), None


def _s3_probe(
    key_id: str,
    application_key: str,
    endpoint: str,
    region: str,
) -> tuple[object, list[dict]]:
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=application_key,
    )
    try:
        response = client.list_buckets()
    except ClientError as exc:
        error = (exc.response or {}).get("Error") or {}
        code = error.get("Code") or "unknown"
        raise RuntimeError(
            f"Backblaze S3 authentication/probe failed ({code}). "
            "The stored B2 key pair is not usable by the S3 API."
        ) from exc
    return client, list(response.get("Buckets") or [])


def _storage_info(auth: dict) -> dict:
    storage = ((auth.get("apiInfo") or {}).get("storageApi") or {})
    if not storage:
        raise RuntimeError("Backblaze authorization did not return storageApi")
    return storage


def _select_bucket(storage: dict) -> tuple[str, str | None, set[str]]:
    allowed = storage.get("allowed") or {}
    capabilities = set(allowed.get("capabilities") or [])
    buckets = allowed.get("buckets") or []

    requested_name = (os.getenv("B2_BUCKET") or "").strip()
    requested_id = (os.getenv("B2_BUCKET_ID") or "").strip()

    selected = None
    if requested_name:
        selected = next(
            (item for item in buckets if item.get("name") == requested_name),
            None,
        )
        if selected is None and buckets:
            raise RuntimeError(
                "B2_BUCKET does not match a bucket allowed by this application key"
            )
    elif requested_id:
        selected = next(
            (item for item in buckets if item.get("id") == requested_id),
            None,
        )
        if selected is None and buckets:
            raise RuntimeError(
                "B2_BUCKET_ID does not match a bucket allowed by this application key"
            )
    elif len(buckets) == 1:
        selected = buckets[0]
    elif len(buckets) > 1:
        raise RuntimeError(
            "Application key can access multiple buckets; set B2_BUCKET or B2_BUCKET_ID"
        )

    if selected is None:
        if requested_name:
            return requested_name, requested_id or None, capabilities
        raise RuntimeError(
            "Could not discover a unique bucket name from the application key; "
            "set B2_BUCKET explicitly"
        )

    return str(selected["name"]), selected.get("id"), capabilities


def _endpoint_and_region(storage: dict) -> tuple[str, str]:
    endpoint = (
        (os.getenv("B2_ENDPOINT_URL") or "").strip()
        or str(storage.get("s3ApiUrl") or "").strip()
    )
    if not endpoint:
        raise RuntimeError("Backblaze authorization did not return an S3 endpoint")
    if not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint.lstrip("/")

    region = (os.getenv("B2_REGION") or "").strip()
    if not region:
        match = re.search(
            r"^https://s3\.([a-z0-9-]+)\.backblazeb2\.com/?$",
            endpoint,
            flags=re.IGNORECASE,
        )
        if not match:
            raise RuntimeError(
                "Could not infer B2 region from S3 endpoint; set B2_REGION"
            )
        region = match.group(1)
    return endpoint.rstrip("/"), region


def _list_exact_versions(client, bucket: str, key: str) -> list[dict]:
    results: list[dict] = []
    key_marker = None
    version_marker = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": key}
        if key_marker:
            kwargs["KeyMarker"] = key_marker
        if version_marker:
            kwargs["VersionIdMarker"] = version_marker
        response = client.list_object_versions(**kwargs)

        for group in ("Versions", "DeleteMarkers"):
            for item in response.get(group) or []:
                if item.get("Key") == key and item.get("VersionId"):
                    results.append(
                        {
                            "Key": key,
                            "VersionId": str(item["VersionId"]),
                            "DeleteMarker": group == "DeleteMarkers",
                        }
                    )

        if not response.get("IsTruncated"):
            break
        key_marker = response.get("NextKeyMarker")
        version_marker = response.get("NextVersionIdMarker")
        if not key_marker and not version_marker:
            break
    return results


def _purge_key(client, bucket: str, key: str) -> None:
    for item in _list_exact_versions(client, bucket, key):
        client.delete_object(
            Bucket=bucket,
            Key=key,
            VersionId=item["VersionId"],
        )


def _purge_prefix(client, bucket: str, prefix: str) -> None:
    key_marker = None
    version_marker = None
    items: list[tuple[str, str]] = []
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if key_marker:
            kwargs["KeyMarker"] = key_marker
        if version_marker:
            kwargs["VersionIdMarker"] = version_marker
        response = client.list_object_versions(**kwargs)

        for group in ("Versions", "DeleteMarkers"):
            for item in response.get(group) or []:
                key = item.get("Key")
                version_id = item.get("VersionId")
                if key and version_id:
                    items.append((str(key), str(version_id)))

        if not response.get("IsTruncated"):
            break
        key_marker = response.get("NextKeyMarker")
        version_marker = response.get("NextVersionIdMarker")
        if not key_marker and not version_marker:
            break

    for key, version_id in items:
        client.delete_object(
            Bucket=bucket,
            Key=key,
            VersionId=version_id,
        )


def _verify_sqlite(path: Path, expected: str) -> None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError("Restored SQLite quick_check failed")
        value = conn.execute(
            "SELECT value FROM acceptance WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    if value != expected:
        raise RuntimeError("Restored SQLite content did not match source")


def main() -> None:
    key_id = _required_env("B2_KEY_ID")
    application_key = _required_env("B2_APPLICATION_KEY")

    endpoint = (
        (os.getenv("B2_ENDPOINT_URL") or "").strip()
        or "https://s3.us-east-005.backblazeb2.com"
    ).rstrip("/")
    region = (os.getenv("B2_REGION") or "").strip() or "us-east-005"

    print("1/7 Authorizing application key")
    auth, native_error = _authorize(key_id, application_key)
    bucket_id = None

    if auth is not None:
        storage = _storage_info(auth)
        bucket, bucket_id, capabilities = _select_bucket(storage)
        endpoint, region = _endpoint_and_region(storage)
        missing = sorted(REQUIRED_CAPABILITIES - capabilities)
        if missing:
            raise RuntimeError(
                "Application key is missing required capabilities: "
                + ", ".join(missing)
            )
        print(
            "2/7 Native authorization succeeded; bucket discovered and "
            f"capabilities verified (region={region})"
        )
    else:
        print(
            "2/7 Native authorization returned HTTP 401; probing the same "
            "credentials through the S3-compatible API"
        )
        _, buckets = _s3_probe(
            key_id,
            application_key,
            endpoint,
            region,
        )
        requested_name = (os.getenv("B2_BUCKET") or "").strip()
        if requested_name:
            match = next(
                (item for item in buckets if item.get("Name") == requested_name),
                None,
            )
            if match is None:
                raise RuntimeError(
                    "S3 authentication succeeded but B2_BUCKET was not visible"
                )
            bucket = requested_name
        elif len(buckets) == 1:
            bucket = str(buckets[0]["Name"])
        elif len(buckets) == 0:
            raise RuntimeError(
                "S3 authentication succeeded but no bucket was visible. "
                "Set B2_BUCKET explicitly or grant list-bucket access."
            )
        else:
            raise RuntimeError(
                "S3 authentication succeeded and multiple buckets are visible. "
                "Set B2_BUCKET explicitly."
            )
        print(
            "S3 authentication succeeded; continuing with the uniquely visible bucket"
        )

    backend = B2ArchiveStorage(
        bucket=bucket,
        region=region,
        endpoint_url=endpoint,
        key_id=key_id,
        application_key=application_key,
        max_archive_bytes=9_000_000_000,
    )

    run_id = uuid.uuid4().hex
    document_key = None
    backup_prefix = f"acceptance-tests/{run_id}/database-backups"
    test_payload = (
        b"%PDF-1.4\n"
        + f"% FOIA B2 acceptance {run_id}\n".encode("ascii")
        + b"%%EOF\n"
    )

    with tempfile.TemporaryDirectory(prefix="foia-b2-acceptance-") as tempdir:
        root = Path(tempdir)
        staged = root / "acceptance.pdf"
        staged.write_bytes(test_payload)
        digest = hashlib.sha256(test_payload).hexdigest()

        try:
            print("3/7 Uploading content-addressed document")
            location = backend.store_file(
                staged,
                sha256=digest,
                filename="acceptance.pdf",
                mime_type="application/pdf",
                current_usage_bytes=0,
            )
            document_key = location.key
            if staged.exists():
                raise RuntimeError(
                    "Staged file was not removed after verified B2 upload"
                )
            if not backend.exists(document_key):
                raise RuntimeError("Uploaded B2 object is not visible")

            head = backend.client.head_object(
                Bucket=bucket,
                Key=document_key,
            )
            if int(head.get("ContentLength", -1)) != len(test_payload):
                raise RuntimeError("Uploaded document size does not match")

            initial_versions = _list_exact_versions(
                backend.client,
                bucket,
                document_key,
            )
            if len([x for x in initial_versions if not x["DeleteMarker"]]) != 1:
                raise RuntimeError(
                    "Expected exactly one stored document version after first upload"
                )

            print("4/7 Verifying private signed download and deduplication")
            signed_url = backend.presigned_url(document_key, expires_seconds=300)
            downloaded = requests.get(signed_url, timeout=30)
            downloaded.raise_for_status()
            if downloaded.content != test_payload:
                raise RuntimeError("Presigned B2 download bytes did not match upload")

            duplicate = root / "duplicate.pdf"
            duplicate.write_bytes(test_payload)
            backend.store_file(
                duplicate,
                sha256=digest,
                filename="acceptance.pdf",
                mime_type="application/pdf",
                current_usage_bytes=len(test_payload),
            )
            if duplicate.exists():
                raise RuntimeError(
                    "Duplicate staged file was not removed after dedupe"
                )
            final_versions = _list_exact_versions(
                backend.client,
                bucket,
                document_key,
            )
            if len([x for x in final_versions if not x["DeleteMarker"]]) != 1:
                raise RuntimeError(
                    "Content-addressed duplicate created an extra B2 version"
                )

            print("5/7 Creating two SQLite backups and pruning the older one")
            db_path = root / "archive.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "CREATE TABLE acceptance (id INTEGER PRIMARY KEY, value TEXT)"
                )
                conn.execute(
                    "INSERT INTO acceptance (id, value) VALUES (1, ?)",
                    (run_id,),
                )
                conn.commit()
            finally:
                conn.close()

            files_dir = root / "staging"
            files_dir.mkdir()
            os.environ["FOIA_STORAGE_BACKEND"] = "b2"
            os.environ["B2_BUCKET"] = bucket
            os.environ["B2_REGION"] = region
            os.environ["B2_ENDPOINT_URL"] = endpoint

            config = Config(
                {
                    "storage": {
                        "backend": "b2",
                        "db_path": str(db_path),
                        "files_dir": str(files_dir),
                        "b2": {
                            "bucket": bucket,
                            "region": region,
                            "endpoint_url": endpoint,
                            "max_archive_bytes": 9_000_000_000,
                        },
                    },
                    "database_backup": {
                        "enabled": True,
                        "interval_hours": 24,
                        "retain_count": 1,
                        "prefix": backup_prefix,
                    },
                }
            )

            first_time = datetime.now(timezone.utc).replace(microsecond=0)
            first = backup_database_to_b2(
                config,
                force=True,
                now=first_time,
            )
            if first is None:
                raise RuntimeError("First SQLite backup was not created")

            second = backup_database_to_b2(
                config,
                force=True,
                now=first_time + timedelta(seconds=2),
            )
            if second is None:
                raise RuntimeError("Second SQLite backup was not created")

            visible_backups = list_database_backups(config)
            if [item.get("Key") for item in visible_backups] != [second.key]:
                raise RuntimeError(
                    "Backup retention did not leave exactly the newest backup"
                )
            if _list_exact_versions(backend.client, bucket, first.key):
                raise RuntimeError(
                    "Old SQLite backup versions still exist after retention pruning"
                )

            print("6/7 Restoring newest SQLite backup and verifying integrity")
            restored = restore_database_from_b2(
                config,
                destination_path=root / "restored.db",
            )
            _verify_sqlite(restored, run_id)

            print("7/7 Cleaning acceptance objects with explicit version IDs")
        finally:
            if document_key:
                _purge_key(backend.client, bucket, document_key)
            _purge_prefix(
                backend.client,
                bucket,
                f"acceptance-tests/{run_id}/",
            )

    if document_key and _list_exact_versions(
        backend.client,
        bucket,
        document_key,
    ):
        raise RuntimeError("Acceptance document cleanup left object versions behind")

    remaining_test_versions = []
    response = backend.client.list_object_versions(
        Bucket=bucket,
        Prefix=f"acceptance-tests/{run_id}/",
    )
    remaining_test_versions.extend(response.get("Versions") or [])
    remaining_test_versions.extend(response.get("DeleteMarkers") or [])
    if remaining_test_versions:
        raise RuntimeError("Acceptance backup cleanup left object versions behind")

    print("PASS: live B2 archive, signed download, dedupe, backup, retention, restore, and cleanup")
    print(f"Verified region: {region}")
    if bucket_id:
        print("Verified bucket restriction: yes")


if __name__ == "__main__":
    main()
