"""Archive storage backends for FOIA documents."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError

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

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            response = getattr(exc, "response", {}) or {}
            code = str((response.get("Error") or {}).get("Code", ""))
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise ArchiveStorageError(f"B2 head_object failed for {key}: {exc}") from exc

    def store_file(
        self,
        source_path: Path,
        *,
        sha256: Optional[str],
        filename: str,
        mime_type: Optional[str] = None,
        current_usage_bytes: int = 0,
    ) -> ArchiveLocation:
        source = Path(source_path)
        if not source.is_file():
            raise ArchiveStorageError(f"Staged download does not exist: {source}")
        key = self.key_for(sha256 or "", filename)
        size = source.stat().st_size

        already_present = self.exists(key)
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
                self.client.upload_file(str(source), self.bucket, key, **kwargs)
                metadata = self.client.head_object(Bucket=self.bucket, Key=key)
                remote_size = int(metadata.get("ContentLength", -1))
                if remote_size != size:
                    raise ArchiveStorageError(
                        f"B2 upload size verification failed for {key}: "
                        f"local={size}, remote={remote_size}"
                    )
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

    return B2ArchiveStorage(
        bucket=os.getenv("B2_BUCKET") or b2_config.get("bucket") or "",
        region=os.getenv("B2_REGION") or b2_config.get("region") or "",
        endpoint_url=(
            os.getenv("B2_ENDPOINT_URL")
            or b2_config.get("endpoint_url")
            or ""
        ),
        key_id=os.getenv("B2_KEY_ID") or "",
        application_key=os.getenv("B2_APPLICATION_KEY") or "",
        max_archive_bytes=max_archive_bytes,
    )
