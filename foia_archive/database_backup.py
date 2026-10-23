"""Consistent SQLite backups to Backblaze B2."""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import sqlite3
import tempfile
import time
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests
from botocore.exceptions import ClientError, SSLError as BotocoreSSLError
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
    source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
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
    seen = set()
    while True:
        kwargs = {"Bucket": backend.bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = backend.client.list_objects_v2(**kwargs)
        objects.extend(response.get("Contents") or [])
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not token or token in seen:
            raise ArchiveStorageError("Incomplete or cyclic B2 backup listing")
        seen.add(token)
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


def _native_url(value: str, *, api: bool = False) -> str:
    """Never send a B2 token to an arbitrary HTTPS host or a redirect."""
    try:
        if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\\\]", value):
            raise ValueError()
        url = urlsplit(value)
        host = url.hostname or ""
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443) or url.fragment:
            raise ValueError()
        if api:
            valid = re.fullmatch(r"api\d{3}\.backblazeb2\.com", host) and url.path in ("", "/") and not url.query
        else:
            path = "/b2api/v4/b2_upload_file"
            valid = re.fullmatch(r"pod[-a-z0-9]*\.(?:backblaze\.com|backblazeb2\.com)", host) and (url.path == path or url.path.startswith(path + "/"))
        if not valid:
            raise ValueError()
    except (ValueError, TypeError):
        raise ArchiveStorageError("Untrusted B2 native endpoint") from None
    return value.rstrip("/") if api else value


def _native_json(response) -> dict:
    # raise_for_status alone accepts redirects. Redirects must never receive tokens.
    if response.status_code != 200:
        if response.status_code == 429 or response.status_code >= 500:
            raise requests.HTTPError("Transient B2 native response", response=response)
        raise ArchiveStorageError("B2 native request rejected")
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, TypeError):
        raise ArchiveStorageError("Invalid B2 native response") from None


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

    try:
        payload = _native_json(requests.get(
            "https://api.backblazeb2.com/b2api/v4/b2_authorize_account",
            auth=(key_id, application_key), timeout=30, allow_redirects=False, verify=True,
        ))
    except requests.RequestException:
        raise ArchiveStorageError("B2 native authorization unavailable") from None
    token = str(payload.get("authorizationToken") or "")
    storage = ((payload.get("apiInfo") or {}).get("storageApi") or {})
    api_url = _native_url(storage.get("apiUrl") or "", api=True)
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
            destination = _native_json(requests.get(
                f"{api_url}/b2api/v4/b2_get_upload_url",
                headers={"Authorization": token},
                params={"bucketId": bucket_id},
                timeout=30, allow_redirects=False, verify=True,
            ))
            upload_url = _native_url(destination.get("uploadUrl") or "")
            upload_token = str(destination.get("authorizationToken") or "")
            if not upload_token or destination.get("bucketId") != bucket_id:
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
                    allow_redirects=False, verify=True,
                )
            receipt = _native_json(reply)
            if (
                receipt.get("fileName") != key
                or receipt.get("bucketId") != bucket_id
                or receipt.get("contentLength") != expected_size
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
                ) from None
            if present is not None:
                if present != expected_size:
                    raise ArchiveStorageError(
                        "Native B2 checkpoint key has unexpected size"
                    ) from None
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
                ) from None
            logger.warning(
                "Native B2 upload failed, trying a fresh upload URL (%s/5): %s",
                attempt, type(exc).__name__,
            )
            time.sleep(min(2 ** attempt, 8))


def _s3_authorization_rejected(error: Exception) -> bool:
    # upload_file wraps a ClientError in S3UploadFailedError. Inspect typed
    # exception lineage, never provider text or guessed error strings.
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ClientError):
            response = error.response
            status = str(response.get("ResponseMetadata", {}).get("HTTPStatusCode"))
            code = response.get("Error", {}).get("Code")
            if status in ("401", "403") or code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken"):
                return True
        error = error.__cause__ or error.__context__
    return False


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
    if expected_size > 5 * 1024 * 1024 * 1024:
        raise ArchiveStorageError("Checkpoint exceeds the single-request upload ceiling")
    transfer = TransferConfig(
        multipart_threshold=5 * 1024 * 1024 * 1024 + 1,
        multipart_chunksize=32 * 1024 * 1024,
        max_concurrency=2,
        use_threads=False,
    )
    tls_failures = 0
    for attempt in range(1, 4):
        try:
            backend.checkpoint_client.upload_file(
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
            if _s3_authorization_rejected(exc):
                raise ArchiveStorageError("B2 checkpoint authorization rejected; no retry or fallback") from None
            tls_failures += int(isinstance(exc, BotocoreSSLError))
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
                ) from None

            if existing_size is not None:
                if existing_size != expected_size:
                    raise ArchiveStorageError(
                        "Checkpoint key already exists in B2 with a different "
                        "size; refusing to overwrite a possible recovery point."
                    ) from None
                logger.warning(
                    "Checkpoint upload response failed, but B2 Class C listing "
                    "confirms the expected object size for %s", key
                )
                return

            if attempt == 3:
                if tls_failures == 3 and (
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
                        ) from None
                raise ArchiveStorageError(
                    f"Database backup upload failed after {attempt} attempts "
                    f"for {key}: {type(exc).__name__}"
                ) from None
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

    seen = set()
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
        marker = (key_marker, version_id_marker)
        if not any(marker) or marker in seen:
            raise ArchiveStorageError("Incomplete or cyclic B2 version listing")
        seen.add(marker)

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
    *, preserve_key: Optional[str] = None,
) -> None:
    retain_count = max(1, int(retain_count))
    objects = _list_backup_objects(backend, prefix)
    if preserve_key:
        if not any(item.get("Key") == preserve_key for item in objects):
            raise ArchiveStorageError("New verified checkpoint is absent from retention listing; no pruning")
        objects = [item for item in objects if item.get("Key") != preserve_key]
    objects.sort(
        key=lambda item: item.get("LastModified")
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    keep_older = retain_count - 1 if preserve_key else retain_count
    for item in objects[keep_older:]:
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
        max_compressed, max_restored = _checkpoint_limits(settings)
        if compressed.stat().st_size > max_compressed or snapshot.stat().st_size > max_restored:
            raise ArchiveStorageError("Checkpoint exceeds configured restore limits; no upload attempted")
        key = (
            f"{prefix}{_utc_timestamp(now)}-{uuid4().hex}-"
            f"{sqlite_sha256[:12]}.sqlite.gz"
        )

        _upload_backup(
            backend,
            compressed,
            key,
            sqlite_sha256,
        )
        compressed_bytes = compressed.stat().st_size
        # Do not prune a known-good recovery point until this new object has
        # actually been read back, decompressed, fully hashed and checked.
        verified_gzip, verified_db = temp / "verified.sqlite.gz", temp / "verified.sqlite"
        metadata = _download_checkpoint(backend, key, verified_gzip, compressed_bytes)
        _unpack_checkpoint(verified_gzip, verified_db, key, max_restored,
                           expected_sha256=sqlite_sha256, metadata=metadata)

    try:
        _prune_old_backups(backend, prefix, retain_count, preserve_key=key)
    except Exception as exc:
        logger.warning(
            "Database backup uploaded but old-backup pruning failed: %s",
            type(exc).__name__,
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


def _checkpoint_limits(settings: dict) -> tuple[int, int]:
    limits = []
    for name, default in (("max_compressed_bytes", 256 * 1024 * 1024),
                          ("max_restored_bytes", 2 * 1024 * 1024 * 1024)):
        value = settings.get(name, default)
        try:
            if isinstance(value, bool) or int(value) != value or int(value) <= 0:
                raise ValueError()
            limits.append(int(value))
        except (TypeError, ValueError, OverflowError):
            raise ArchiveStorageError("Invalid checkpoint byte limit") from None
    return tuple(limits)


def _download_checkpoint(backend: B2ArchiveStorage, key: str, destination: Path, limit: int) -> dict:
    """One bounded GetObject stream; no transfer-manager HeadObject request."""
    body = None
    try:
        result = backend.client.get_object(Bucket=backend.bucket, Key=key)
        body = result["Body"]
        length = result.get("ContentLength")
        if not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= limit:
            raise ArchiveStorageError("Checkpoint download exceeds its byte limit or has no valid length")
        total = 0
        with destination.open("wb") as target:
            while True:
                chunk = body.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit or total > length:
                    raise ArchiveStorageError("Checkpoint download exceeds declared size or byte limit")
                target.write(chunk)
        if total != length:
            raise ArchiveStorageError("Checkpoint download was truncated")
        return result.get("Metadata") or {}
    except ArchiveStorageError:
        raise
    except Exception:
        raise ArchiveStorageError("Checkpoint object read failed; preserve prior recovery points") from None
    finally:
        if body is not None:
            body.close()


def _unpack_checkpoint(compressed: Path, restored: Path, key: str, limit: int,
                       *, expected_sha256: Optional[str] = None, metadata: Optional[dict] = None) -> None:
    digest, total = hashlib.sha256(), 0
    try:
        with gzip.open(compressed, "rb") as source, restored.open("wb") as target:
            while True:
                chunk = source.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ArchiveStorageError("Restored checkpoint exceeds byte limit")
                digest.update(chunk)
                target.write(chunk)
    except (OSError, EOFError, zlib.error):
        raise ArchiveStorageError("Database backup is not a valid gzip stream") from None
    actual_sha = digest.hexdigest()
    stored_sha = (metadata or {}).get("sqlite-sha256")
    for expected in (expected_sha256, stored_sha):
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or actual_sha != expected):
            raise ArchiveStorageError("Database backup SHA-256 mismatch")
    match = re.search(r"-([0-9a-f]{12})\.sqlite\.gz$", key)
    if match and not actual_sha.startswith(match.group(1)):
        raise ArchiveStorageError("Database backup SHA-256 mismatch")
    if not (expected_sha256 or stored_sha or match):
        raise ArchiveStorageError("Database backup has no verifiable checksum")
    try:
        with closing(sqlite3.connect(restored.resolve().as_uri() + "?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ArchiveStorageError("Restored SQLite integrity check failed")
    except sqlite3.DatabaseError:
        raise ArchiveStorageError("Restored SQLite integrity check failed") from None

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

        limits = _checkpoint_limits(config.data.get("database_backup") or {})
        metadata = _download_checkpoint(backend, key, compressed, limits[0])
        _unpack_checkpoint(compressed, restored, key, limits[1], metadata=metadata)
        # Preserve the existing destination until the validated replacement is
        # atomically installed. Never mix it with a previous SQLite WAL.
        if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise ArchiveStorageError("Restore destination has SQLite sidecars; quiesce it before replacement")
        restored.replace(destination)

    logger.info("Restored verified SQLite backup %s to %s", key, destination)
    return destination


