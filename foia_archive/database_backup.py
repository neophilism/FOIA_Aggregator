"""Consistent SQLite backups to Backblaze B2."""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

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
        destination.execute("PRAGMA quick_check")
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


def _upload_backup(
    backend: B2ArchiveStorage,
    compressed_path: Path,
    key: str,
    sqlite_sha256: str,
) -> None:
    size = compressed_path.stat().st_size
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
        )
        remote = backend.client.head_object(Bucket=backend.bucket, Key=key)
        remote_size = int(remote.get("ContentLength", -1))
        if remote_size != size:
            raise ArchiveStorageError(
                f"Database backup size verification failed for {key}: "
                f"local={size}, remote={remote_size}"
            )
    except ArchiveStorageError:
        raise
    except Exception as exc:
        raise ArchiveStorageError(
            f"Database backup upload failed for {key}: {exc}"
        ) from exc


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
        backend.client.delete_object(Bucket=backend.bucket, Key=key)


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
