"""Archive storage backends for FOIA documents."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import boto3
import requests

from .utils import Config


class ArchiveStorageError(RuntimeError):
    """Base error for archive backend failures."""


class StorageQuotaReached(ArchiveStorageError):
    """Raised when an application-level remote storage ceiling is reached."""


@dataclass(frozen=True)
class ArchiveLocation:
    backend: str
    key: str
    local_path: Optional[str] = None


@dataclass(frozen=True)
class B2Usage:
    stored_bytes: int
    version_count: int
    current_object_count: int
    document_bytes: int
    backup_bytes: int
    other_bytes: int


class ArchiveStorage:
    """Minimal interface shared by local and remote archive backends."""

    name = "unknown"

    def store_file(
        self,
        source_path: Path,
        *,
        sha256: Optional[str],
        filename: str,
        mime_type: Optional[str] = None,
        current_usage_bytes: int = 0,
        known_present: bool = False,
    ) -> ArchiveLocation:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def presigned_url(self, key: str, expires_seconds: int = 3600) -> str:
        raise ArchiveStorageError(f"{self.name} does not provide presigned URLs")


class LocalArchiveStorage(ArchiveStorage):
    name = "local"

    def __init__(self, files_dir: Path | str):
        self.files_dir = Path(files_dir).resolve()
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_key(self, key: str) -> Path:
        candidate = (self.files_dir / key).resolve()
        try:
            candidate.relative_to(self.files_dir)
        except ValueError as exc:
            raise ArchiveStorageError("Local archive key escapes files_dir") from exc
        return candidate

    def store_file(
        self,
        source_path: Path,
        *,
        sha256: Optional[str],
        filename: str,
        mime_type: Optional[str] = None,
        current_usage_bytes: int = 0,
        known_present: bool = False,
    ) -> ArchiveLocation:
        source = Path(source_path).resolve()
        try:
            relative = source.relative_to(self.files_dir)
        except ValueError as exc:
            raise ArchiveStorageError(
                "Local downloads must be staged inside storage.files_dir"
            ) from exc
        return ArchiveLocation(
            backend=self.name,
            key=relative.as_posix(),
            local_path=relative.as_posix(),
        )

    def exists(self, key: str) -> bool:
        return self._resolve_key(key).is_file()

    def path_for_key(self, key: str) -> Path:
        return self._resolve_key(key)


class B2ArchiveStorage(ArchiveStorage):
    """Backblaze B2 storage through its S3-compatible API."""

    name = "b2"

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str,
        key_id: str,
        application_key: str,
        max_archive_bytes: int = 9_000_000_000,
        client=None,
    ):
        missing = [
            name
            for name, value in (
                ("B2_BUCKET", bucket),
                ("B2_REGION", region),
                ("B2_ENDPOINT_URL", endpoint_url),
                ("B2_KEY_ID", key_id),
                ("B2_APPLICATION_KEY", application_key),
            )
            if not value
        ]
        if missing:
            raise ArchiveStorageError(
                "Missing Backblaze B2 configuration: " + ", ".join(missing)
            )

        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.max_archive_bytes = max(0, int(max_archive_bytes))
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=key_id,
            aws_secret_access_key=application_key,
        )

    @staticmethod
    def key_for(sha256: str, filename: str) -> str:
        if not sha256 or len(sha256) < 2:
            raise ArchiveStorageError("SHA-256 is required for B2 archive storage")
        suffix = Path(filename or "").suffix.lower()
        if len(suffix) > 12 or any(ch not in ".abcdefghijklmnopqrstuvwxyz0123456789" for ch in suffix):
            suffix = ""
        return f"documents/{sha256[:2]}/{sha256}{suffix}"

    def current_object_manifest(self, prefix: str = "") -> dict[str, int]:
        """Return current object keys and sizes using Class C list operations.

        Backblaze bills S3 ListObjectsV2 as Class C. This is intentionally used
        for reconciliation instead of issuing one Class B HeadObject request per
        archived object.
        """
        objects: dict[str, int] = {}
        token = None
        while True:
            kwargs = {"Bucket": self.bucket}
            if prefix:
                kwargs["Prefix"] = prefix
            if token:
                kwargs["ContinuationToken"] = token
            try:
                response = self.client.list_objects_v2(**kwargs)
            except Exception as exc:
                raise ArchiveStorageError(
                    f"Could not list current B2 objects for prefix {prefix!r}: {exc}"
                ) from exc

            for item in response.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key:
                    objects[key] = max(0, int(item.get("Size") or 0))

            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
        return objects

    def exists(self, key: str) -> bool:
        """Check one key through the Class C object manifest, never HeadObject."""
        return key in self.current_object_manifest(prefix=key)

    def store_file(
        self,
        source_path: Path,
        *,
        sha256: Optional[str],
        filename: str,
        mime_type: Optional[str] = None,
        current_usage_bytes: int = 0,
        known_present: bool = False,
    ) -> ArchiveLocation:
        source = Path(source_path)
        if not source.is_file():
            raise ArchiveStorageError(f"Staged download does not exist: {source}")
        key = self.key_for(sha256 or "", filename)
        size = source.stat().st_size

        already_present = bool(known_present)
        if (
            not already_present
            and self.max_archive_bytes
            and current_usage_bytes + size > self.max_archive_bytes
        ):
            raise StorageQuotaReached(
                f"B2 document archive safety cap would be exceeded "
                f"({current_usage_bytes + size} > {self.max_archive_bytes} bytes)"
            )

        try:
            if not already_present:
                extra_args = {"ContentType": mime_type} if mime_type else None
                kwargs = {"ExtraArgs": extra_args} if extra_args else {}
                # A successful PutObject/upload_file response is authoritative
                # for normal ingestion. Per-object HeadObject verification is a
                # Class B transaction and previously exhausted the daily free
                # Class B allowance during large corpus waves.
                self.client.upload_file(str(source), self.bucket, key, **kwargs)
        except StorageQuotaReached:
            raise
        except ArchiveStorageError:
            raise
        except Exception as exc:
            raise ArchiveStorageError(f"B2 upload failed for {key}: {exc}") from exc

        try:
            source.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ArchiveStorageError(
                f"B2 upload succeeded but staged file cleanup failed: {exc}"
            ) from exc

        return ArchiveLocation(backend=self.name, key=key, local_path=None)

    def usage_summary(self) -> B2Usage:
        """Return billable stored bytes across all B2 object versions.

        B2 buckets are versioned. Counting only current object names can
        understate storage, so capacity decisions sum every concrete version.
        Delete markers carry no object bytes and are not counted.
        """
        stored_bytes = 0
        version_count = 0
        current_object_count = 0
        document_bytes = 0
        backup_bytes = 0
        other_bytes = 0
        key_marker = None
        version_id_marker = None

        while True:
            kwargs = {"Bucket": self.bucket}
            if key_marker:
                kwargs["KeyMarker"] = key_marker
            if version_id_marker:
                kwargs["VersionIdMarker"] = version_id_marker

            try:
                response = self.client.list_object_versions(**kwargs)
            except Exception as exc:
                raise ArchiveStorageError(
                    f"Could not measure B2 bucket usage: {exc}"
                ) from exc

            for item in response.get("Versions") or []:
                size = max(0, int(item.get("Size") or 0))
                key = str(item.get("Key") or "")
                stored_bytes += size
                version_count += 1
                if item.get("IsLatest"):
                    current_object_count += 1
                if key.startswith("documents/"):
                    document_bytes += size
                elif key.startswith("database-backups/"):
                    backup_bytes += size
                else:
                    other_bytes += size

            if not response.get("IsTruncated"):
                break
            key_marker = response.get("NextKeyMarker")
            version_id_marker = response.get("NextVersionIdMarker")
            if not key_marker and not version_id_marker:
                break

        return B2Usage(
            stored_bytes=stored_bytes,
            version_count=version_count,
            current_object_count=current_object_count,
            document_bytes=document_bytes,
            backup_bytes=backup_bytes,
            other_bytes=other_bytes,
        )

    def presigned_url(self, key: str, expires_seconds: int = 3600) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=max(60, min(int(expires_seconds), 86400)),
            )
        except Exception as exc:
            raise ArchiveStorageError(
                f"Could not create B2 download URL for {key}: {exc}"
            ) from exc


B2_AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"


def _region_from_b2_endpoint(endpoint_url: str) -> str:
    match = re.match(
        r"^https?://s3\.([a-z0-9-]+)\.backblazeb2\.com/?$",
        (endpoint_url or "").strip(),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


@lru_cache(maxsize=4)
def _authorize_b2_scope(
    key_id: str,
    application_key: str,
) -> dict:
    """Resolve the storage scope for a Backblaze application key."""
    try:
        response = requests.get(
            B2_AUTHORIZE_URL,
            auth=(key_id, application_key),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ArchiveStorageError(
            f"Could not authorize Backblaze B2 application key: {exc}"
        ) from exc

    if response.status_code == 401:
        raise ArchiveStorageError(
            "Backblaze rejected B2_KEY_ID/B2_APPLICATION_KEY"
        )

    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ArchiveStorageError(
            f"Backblaze authorization failed: {exc}"
        ) from exc

    storage = ((payload.get("apiInfo") or {}).get("storageApi") or {})
    if not storage:
        raise ArchiveStorageError(
            "Backblaze authorization returned no storage API configuration"
        )
    return storage


def _resolve_b2_configuration(
    config: Config,
    *,
    key_id: str,
    application_key: str,
) -> tuple[str, str, str]:
    b2_config = config.storage.get("b2") or {}
    bucket = (os.getenv("B2_BUCKET") or b2_config.get("bucket") or "").strip()
    region = (os.getenv("B2_REGION") or b2_config.get("region") or "").strip()
    endpoint_url = (
        os.getenv("B2_ENDPOINT_URL")
        or b2_config.get("endpoint_url")
        or ""
    ).strip()

    if endpoint_url and not region:
        region = _region_from_b2_endpoint(endpoint_url)
    if region and not endpoint_url:
        endpoint_url = f"https://s3.{region}.backblazeb2.com"

    if bucket and region and endpoint_url:
        return bucket, region, endpoint_url

    if not key_id or not application_key:
        missing = []
        if not bucket:
            missing.append("B2_BUCKET")
        if not region:
            missing.append("B2_REGION")
        if not endpoint_url:
            missing.append("B2_ENDPOINT_URL")
        raise ArchiveStorageError(
            "Missing Backblaze B2 configuration: " + ", ".join(missing)
        )

    storage = _authorize_b2_scope(key_id, application_key)

    if not endpoint_url:
        endpoint_url = str(storage.get("s3ApiUrl") or "").strip()
    if endpoint_url and not endpoint_url.startswith("http"):
        endpoint_url = "https://" + endpoint_url.lstrip("/")
    endpoint_url = endpoint_url.rstrip("/")

    if not region:
        region = _region_from_b2_endpoint(endpoint_url)

    allowed = storage.get("allowed") or {}
    allowed_buckets = allowed.get("buckets") or []
    if not bucket:
        if len(allowed_buckets) == 1:
            bucket = str(allowed_buckets[0].get("name") or "").strip()
        elif len(allowed_buckets) > 1:
            raise ArchiveStorageError(
                "Backblaze key can access multiple buckets; set B2_BUCKET"
            )
        else:
            raise ArchiveStorageError(
                "Could not discover a unique Backblaze bucket; set B2_BUCKET"
            )
    elif allowed_buckets and not any(
        str(item.get("name") or "") == bucket
        for item in allowed_buckets
    ):
        raise ArchiveStorageError(
            "Configured B2_BUCKET is not allowed by this application key"
        )

    missing = [
        name
        for name, value in (
            ("B2_BUCKET", bucket),
            ("B2_REGION", region),
            ("B2_ENDPOINT_URL", endpoint_url),
        )
        if not value
    ]
    if missing:
        raise ArchiveStorageError(
            "Could not resolve Backblaze B2 configuration: "
            + ", ".join(missing)
        )

    return bucket, region, endpoint_url


def _storage_backend_name(config: Config, backend_name: Optional[str] = None) -> str:
    return (
        backend_name
        or os.getenv("FOIA_STORAGE_BACKEND")
        or config.storage.get("backend")
        or "local"
    ).strip().lower()


def get_archive_storage(
    config: Config,
    backend_name: Optional[str] = None,
) -> ArchiveStorage:
    backend = _storage_backend_name(config, backend_name)

    if backend == "local":
        return LocalArchiveStorage(config.storage.get("files_dir", "data/files"))

    if backend != "b2":
        raise ArchiveStorageError(f"Unsupported archive storage backend: {backend}")

    b2_config = config.storage.get("b2") or {}
    max_archive_bytes_raw = (
        os.getenv("B2_MAX_ARCHIVE_BYTES")
        or b2_config.get("max_archive_bytes")
        or 9_000_000_000
    )
    try:
        max_archive_bytes = int(max_archive_bytes_raw)
    except (TypeError, ValueError) as exc:
        raise ArchiveStorageError("B2_MAX_ARCHIVE_BYTES must be an integer") from exc

    key_id = (os.getenv("B2_KEY_ID") or "").strip()
    application_key = (os.getenv("B2_APPLICATION_KEY") or "").strip()
    bucket, region, endpoint_url = _resolve_b2_configuration(
        config,
        key_id=key_id,
        application_key=application_key,
    )

    return B2ArchiveStorage(
        bucket=bucket,
        region=region,
        endpoint_url=endpoint_url,
        key_id=key_id,
        application_key=application_key,
        max_archive_bytes=max_archive_bytes,
    )
