"""Consistent SQLite backups to Backblaze B2."""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from botocore.exceptions import SSLError as BotocoreSSLError
from boto3.s3.transfer import TransferConfig

from .archive_storage import ArchiveStorageError, B2ArchiveStorage, get_archive_storage
from .utils import Config, logger


@dataclass(frozen=True)
class DatabaseBackupResult:
    key: str
    compressed_bytes: int
    sha256: str
    created_at: datetime


def _utc_timestamp(value: Optional[datetime] = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _snapshot_sqlite(source_path: Path, destination_path: Path) -> None:
    """Create a transactionally consistent SQLite snapshot."""
    source = sqlite3.connect(str(source_path), timeout=30)
    destination = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source.backup(destination)
        row = destination.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise ArchiveStorageError(
                f"SQLite backup integrity check failed: {row[0] if row else 'no result'}"
            )
        destination.commit()
    finally:
        destination.close()
        source.close()


def _gzip_file(source_path: Path, destination_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as source, gzip.GzipFile(
        filename=str(destination_path),
        mode="wb",
        compresslevel=6,
        mtime=0,
    ) as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


def _list_backup_objects(
    backend: B2ArchiveStorage,
    prefix: str,
) -> list[dict]:
    objects: list[dict] = []
    token = None
    while True:
        kwargs = {"Bucket": backend.bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = backend.client.list_objects_v2(**kwargs)
        objects.extend(response.get("Contents") or [])
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not token:
            break
    return objects


def _latest_backup_time(
    backend: B2ArchiveStorage,
    prefix: str,
) -> Optional[datetime]:
    objects = _list_backup_objects(backend, prefix)
    times = [
        item.get("LastModified")
        for item in objects
        if item.get("LastModified") is not None
    ]
    if not times:
        return None
    latest = max(times)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest.astimezone(timezone.utc)


def database_backup_due(
    config: Config,
    *,
    now: Optional[datetime] = None,
    backend: Optional[B2ArchiveStorage] = None,
) -> bool:
    settings = config.data.get("database_backup") or {}
    if not settings.get("enabled", True):
        return False

    interval_hours = settings.get("interval_hours", 24)
    try:
        interval_seconds = max(0.0, float(interval_hours) * 3600)
    except (TypeError, ValueError):
        interval_seconds = 24 * 3600

    selected = backend or get_archive_storage(config)
    if not isinstance(selected, B2ArchiveStorage):
        return False

    prefix = str(settings.get("prefix") or "database-backups/").strip("/")
    prefix += "/"
    latest = _latest_backup_time(selected, prefix)
    if latest is None or interval_seconds <= 0:
        return True

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (now - latest).total_seconds() >= interval_seconds


def _native_upload_backup(
    backend: B2ArchiveStorage,
    compressed_path: Path,
    key: str,
    sqlite_sha256: str,
) -> None:
    """Fall back to the documented B2 Native API when S3 TLS fails.

    The native upload URL points to a different B2 storage pod. Every failed
    request is reconciled with a Class C listing before trying a new pod.
    TLS verification stays on; a bucket-scoped key is required.
    """
    key_id = (os.getenv("B2_KEY_ID") or "").strip()
    application_key = (os.getenv("B2_APPLICATION_KEY") or "").strip()
    if not key_id or not application_key:
        raise ArchiveStorageError("B2 credentials required for native upload fallback")

    authorization = requests.get(
        "https://api.backblazeb2.com/b2api/v4/b2_authorize_account",
        auth=(key_id, application_key), timeout=30,
    )
    authorization.raise_for_status()
    payload = authorization.json()
    token = str(payload.get("authorizationToken") or "")
    storage = ((payload.get("apiInfo") or {}).get("storageApi") or {})
    api_url = str(storage.get("apiUrl") or "").rstrip("/")
    allowed = storage.get("allowed") or {}
    buckets = allowed.get("buckets") or []
    bucket = next(
        (item for item in buckets if item.get("name") == backend.bucket), None
    )
    bucket_id = str((bucket or {}).get("id") or "")
    if not token or not api_url.startswith("https://") or not bucket_id:
        raise ArchiveStorageError(
            "Native B2 credentials do not authorize the configured bucket"
        )
    if "writeFiles" not in (allowed.get("capabilities") or []):
        raise ArchiveStorageError("B2 native fallback key lacks writeFiles")

    expected_size = compressed_path.stat().st_size
    compressed_sha1 = hashlib.sha1()
    with compressed_path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            compressed_sha1.update(chunk)
    sha1 = compressed_sha1.hexdigest()

    for attempt in range(1, 6):
        try:
            upload_data = requests.get(
                f"{api_url}/b2api/v4/b2_get_upload_url",
                headers={"Authorization": token},
                params={"bucketId": bucket_id},
                timeout=30,
            )
            upload_data.raise_for_status()
            destination = upload_data.json()
            upload_url = str(destination.get("uploadUrl") or "")
            upload_token = str(destination.get("authorizationToken") or "")
            if not upload_url.startswith("https://") or not upload_token:
                raise ArchiveStorageError("Native B2 upload URL is unavailable")

            headers = {
                "Authorization": upload_token,
                "X-Bz-File-Name": quote(key, safe=""),
                "Content-Type": "application/gzip",
                "Content-Length": str(expected_size),
                "X-Bz-Content-Sha1": sha1,
                "X-Bz-Info-sqlite-sha256": sqlite_sha256,
                "X-Bz-Info-format": "sqlite3-gzip",
            }
            with compressed_path.open("rb") as file:
                reply = requests.post(
                    upload_url, headers=headers, data=file, timeout=(30, 180),
                )
            reply.raise_for_status()
            receipt = reply.json()
            if (
                receipt.get("fileName") != key
                or int(receipt.get("contentLength", -1)) != expected_size
                or receipt.get("contentSha1") != sha1
            ):
                raise ArchiveStorageError(
                    "Native B2 upload receipt did not match checkpoint"
                )
            return
        except requests.RequestException as exc:
            # Do not retry blindly: B2 could have committed a file whose
            # response was lost. An unknown manifest state requires operator
            # reconciliation rather than risking duplicate billable versions.
            try:
                present = backend.current_object_manifest(prefix=key).get(key)
            except Exception as list_exc:
                raise ArchiveStorageError(
                    "Native B2 upload status unknown; Class C listing failed"
                ) from list_exc
            if present is not None:
                if present != expected_size:
                    raise ArchiveStorageError(
                        "Native B2 checkpoint key has unexpected size"
                    ) from exc
                logger.warning(
                    "Native B2 upload response was lost; listed checkpoint %s",
                    key,
                )
                return
            if attempt == 5 or (
                getattr(exc, "response", None) is not None
                and exc.response.status_code in (401, 403)
            ):
                raise ArchiveStorageError(
                    "Native B2 upload failed after bounded retries: "
                    + type(exc).__name__
                ) from exc
            logger.warning(
                "Native B2 upload failed, trying a fresh upload URL (%s/5): %s",
                attempt, type(exc).__name__,
            )
            time.sleep(min(2 ** attempt, 8))


def _upload_backup(
    backend: B2ArchiveStorage,
    compressed_path: Path,
    key: str,
    sqlite_sha256: str,
) -> None:
    """Upload a checkpoint without a per-object Class B HEAD request.

    A transient TLS failure can occur *after* B2 accepts an upload. Before
    retrying, use an exact-key Class C listing so a lost response does not
    create a second billable object version in a versioned bucket.
    """
    expected_size = compressed_path.stat().st_size
    transfer = TransferConfig(
        multipart_threshold=256 * 1024 * 1024,
        multipart_chunksize=32 * 1024 * 1024,
        max_concurrency=2,
        use_threads=False,
    )
    for attempt in range(1, 4):
        try:
            backend.client.upload_file(
                str(compressed_path),
                backend.bucket,
                key,
                ExtraArgs={
                    "ContentType": "application/gzip",
                    "Metadata": {
                        "sqlite-sha256": sqlite_sha256,
                        "format": "sqlite3-gzip",
                    },
                },
                Config=transfer,
            )
            return
        except Exception as exc:
            # A failed client call does not necessarily mean the object was
            # never committed. If listing fails, stop rather than risk a
            # duplicate object version through a blind retry.
            try:
                existing_size = backend.current_object_manifest(prefix=key).get(key)
            except Exception as reconciliation_error:
                raise ArchiveStorageError(
                    "Checkpoint upload status is unknown after an error; "
                    "B2 reconciliation failed. Retain the local database "
                    "and reconcile B2 before retrying."
                ) from reconciliation_error

            if existing_size is not None:
                if existing_size != expected_size:
                    raise ArchiveStorageError(
                        "Checkpoint key already exists in B2 with a different "
                        "size; refusing to overwrite a possible recovery point."
                    ) from exc
                logger.warning(
                    "Checkpoint upload response failed, but B2 Class C listing "
                    "confirms the expected object size for %s", key
                )
                return

            if attempt == 3:
                if isinstance(exc, BotocoreSSLError) and (
                    os.getenv("B2_KEY_ID") and os.getenv("B2_APPLICATION_KEY")
                ):
                    logger.warning(
                        "S3 B2 upload encountered repeated TLS failures; "
                        "trying a verified B2 Native API upload for %s", key,
                    )
                    try:
                        _native_upload_backup(
                            backend, compressed_path, key, sqlite_sha256
                        )
                        return
                    except Exception as native_error:
                        raise ArchiveStorageError(
                            "S3 and B2 Native checkpoint uploads both failed: "
                            + type(native_error).__name__
                        ) from native_error
                raise ArchiveStorageError(
                    f"Database backup upload failed after {attempt} attempts "
                    f"for {key}: {exc}"
                ) from exc
            logger.warning(
                "Checkpoint upload attempt %s failed for %s; no B2 object "
                "was committed. Retrying after a bounded delay: %s",
                attempt, key, type(exc).__name__,
            )
            time.sleep(2 ** attempt)


def _list_exact_object_versions(
    backend: B2ArchiveStorage,
    key: str,
) -> list[dict]:
    """Return every stored version/delete marker for one exact B2 object key."""
    items: list[dict] = []
    key_marker = None
    version_id_marker = None

    while True:
        kwargs = {
            "Bucket": backend.bucket,
            "Prefix": key,
        }
        if key_marker:
            kwargs["KeyMarker"] = key_marker
        if version_id_marker:
            kwargs["VersionIdMarker"] = version_id_marker

        response = backend.client.list_object_versions(**kwargs)
        for collection_name in ("Versions", "DeleteMarkers"):
            for item in response.get(collection_name) or []:
                if item.get("Key") != key:
                    continue
                version_id = item.get("VersionId")
                if version_id:
                    items.append(
                        {
                            "Key": key,
                            "VersionId": str(version_id),
                            "IsDeleteMarker": collection_name == "DeleteMarkers",
                        }
                    )

        if not response.get("IsTruncated"):
            break
        key_marker = response.get("NextKeyMarker")
        version_id_marker = response.get("NextVersionIdMarker")
        if not key_marker and not version_id_marker:
            break

    return items


def _permanently_delete_object(
    backend: B2ArchiveStorage,
    key: str,
) -> None:
    """Permanently remove all versions of one B2 object key.

    Backblaze B2 buckets are versioned by default. A normal S3 delete without a
    VersionId creates only a delete marker and leaves older bytes billable.
    Backup retention therefore deletes every concrete version explicitly.
    """
    versions = _list_exact_object_versions(backend, key)
    for item in versions:
        backend.client.delete_object(
            Bucket=backend.bucket,
            Key=key,
            VersionId=item["VersionId"],
        )


def _prune_old_backups(
    backend: B2ArchiveStorage,
    prefix: str,
    retain_count: int,
) -> None:
    retain_count = max(1, int(retain_count))
    objects = _list_backup_objects(backend, prefix)
    objects.sort(
        key=lambda item: item.get("LastModified")
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for item in objects[retain_count:]:
        key = item.get("Key")
        if not key:
            continue
        _permanently_delete_object(backend, str(key))


def backup_database_to_b2(
    config: Config,
    *,
    force: bool = False,
    now: Optional[datetime] = None,
) -> Optional[DatabaseBackupResult]:
    """Create, verify, upload, and rotate a compressed SQLite snapshot.

    Returns None when backups are disabled, local storage is selected, or the
    configured interval has not elapsed.
    """
    settings = config.data.get("database_backup") or {}
    if not settings.get("enabled", True):
        return None

    backend = get_archive_storage(config)
    if not isinstance(backend, B2ArchiveStorage):
        return None

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not force and not database_backup_due(config, now=now, backend=backend):
        return None

    db_path = Path(config.storage.get("db_path", "data/foia_archive.db"))
    if not db_path.is_file():
        raise ArchiveStorageError(f"SQLite database does not exist: {db_path}")

    prefix = str(settings.get("prefix") or "database-backups/").strip("/")
    prefix += "/"
    retain_count_raw = settings.get("retain_count", 3)
    try:
        retain_count = max(1, int(retain_count_raw))
    except (TypeError, ValueError):
        retain_count = 3

    staging_root = Path(
        settings.get("staging_dir")
        or config.storage.get("files_dir", "data/files")
    )
    staging_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="foia-db-backup-",
        dir=str(staging_root),
    ) as tempdir:
        temp = Path(tempdir)
        snapshot = temp / "foia_archive.sqlite"
        compressed = temp / "foia_archive.sqlite.gz"

        _snapshot_sqlite(db_path, snapshot)
        sqlite_sha256 = _gzip_file(snapshot, compressed)
        key = (
            f"{prefix}{_utc_timestamp(now)}-"
            f"{sqlite_sha256[:12]}.sqlite.gz"
        )

        _upload_backup(
            backend,
            compressed,
            key,
            sqlite_sha256,
        )
        compressed_bytes = compressed.stat().st_size

    try:
        _prune_old_backups(backend, prefix, retain_count)
    except Exception as exc:
        logger.warning(
            "Database backup uploaded but old-backup pruning failed: %s",
            exc,
        )

    logger.info(
        "Uploaded SQLite backup to %s (%s bytes)",
        key,
        compressed_bytes,
    )
    return DatabaseBackupResult(
        key=key,
        compressed_bytes=compressed_bytes,
        sha256=sqlite_sha256,
        created_at=now,
    )

def list_database_backups(config: Config) -> list[dict]:
    """Return B2 database backups newest first."""
    backend = get_archive_storage(config)
    if not isinstance(backend, B2ArchiveStorage):
        return []
    settings = config.data.get("database_backup") or {}
    prefix = str(settings.get("prefix") or "database-backups/").strip("/") + "/"
    objects = _list_backup_objects(backend, prefix)
    objects.sort(
        key=lambda item: item.get("LastModified")
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return objects


def bootstrap_database_from_b2(
    config: Config,
    *,
    destination_path: Optional[Path | str] = None,
) -> Optional[Path]:
    """Restore the newest verified B2 snapshot when the live DB is absent.

    Existing non-empty databases are never overwritten automatically.
    """
    destination = Path(
        destination_path
        or config.storage.get("db_path", "data/foia_archive.db")
    )
    if destination.exists() and destination.stat().st_size > 0:
        return None

    if destination.exists():
        destination.unlink()

    backups = list_database_backups(config)
    if not backups:
        return None

    newest_key = str(backups[0].get("Key") or "")
    if not newest_key:
        return None

    return restore_database_from_b2(
        config,
        key=newest_key,
        destination_path=destination,
        overwrite=True,
    )


def restore_database_from_b2(
    config: Config,
    *,
    key: Optional[str] = None,
    destination_path: Optional[Path | str] = None,
    overwrite: bool = False,
) -> Path:
    """Download and verify a B2 SQLite backup into a recovery file.

    The default destination is separate from the live database so a restore
    never overwrites production state accidentally.
    """
    backend = get_archive_storage(config)
    if not isinstance(backend, B2ArchiveStorage):
        raise ArchiveStorageError("Database restore requires B2 storage")

    backups = list_database_backups(config)
    if key is None:
        if not backups:
            raise ArchiveStorageError("No database backups are available")
        key = str(backups[0].get("Key") or "")
    if not key:
        raise ArchiveStorageError("Database backup key is empty")

    live_db = Path(config.storage.get("db_path", "data/foia_archive.db"))
    destination = Path(
        destination_path
        or live_db.with_name(f"{live_db.stem}.restored{live_db.suffix}")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise ArchiveStorageError(
            f"Restore destination already exists: {destination}. "
            "Choose another path or explicitly allow overwrite."
        )

    with tempfile.TemporaryDirectory(
        prefix="foia-db-restore-",
        dir=str(destination.parent),
    ) as tempdir:
        temp = Path(tempdir)
        compressed = temp / "backup.sqlite.gz"
        restored = temp / "restored.sqlite"

        try:
            backend.client.download_file(
                backend.bucket,
                key,
                str(compressed),
            )
        except Exception as exc:
            raise ArchiveStorageError(
                f"Database backup download failed for {key}: {exc}"
            ) from exc

        digest = hashlib.sha256()
        try:
            with gzip.open(compressed, "rb") as source, restored.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target.write(chunk)
        except (OSError, EOFError) as exc:
            raise ArchiveStorageError(
                f"Database backup is not a valid gzip stream: {key}"
            ) from exc

        actual_sha = digest.hexdigest()
        match = re.search(r"-([0-9a-f]{12})\.sqlite\.gz$", key)
        if match and not actual_sha.startswith(match.group(1)):
            raise ArchiveStorageError(
                f"Database backup SHA-256 mismatch for {key}"
            )

        check = sqlite3.connect(str(restored))
        try:
            row = check.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise ArchiveStorageError(
                    f"Restored SQLite integrity check failed: "
                    f"{row[0] if row else 'no result'}"
                )
        finally:
            check.close()

        if destination.exists():
            destination.unlink()
        restored.replace(destination)

    logger.info("Restored verified SQLite backup %s to %s", key, destination)
    return destination

