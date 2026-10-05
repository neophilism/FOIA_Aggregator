"""Build or extend a bounded presentation corpus using B2-backed storage.

This script is intended for the manual GitHub Actions seed workflow. It:
1. authorizes the bucket-scoped Backblaze application key;
2. discovers the allowed bucket/region/endpoint without printing secrets;
3. restores the newest SQLite snapshot when one exists;
4. runs one bounded live crawl cycle;
5. forces a fresh verified SQLite backup to B2;
6. prints and optionally writes presentation statistics.

The original document binaries are archived through the normal crawler path.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import requests

from foia_archive.database_backup import (
    backup_database_to_b2,
    list_database_backups,
    restore_database_from_b2,
)
from foia_archive.engine import run_once
from foia_archive.storage import get_archive_stats, get_connection, init_db
from foia_archive.utils import load_config


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


def _authorize(key_id: str, application_key: str) -> dict:
    response = requests.get(
        AUTHORIZE_URL,
        auth=(key_id, application_key),
        timeout=30,
    )
    if response.status_code == 401:
        raise RuntimeError(
            "Backblaze rejected B2_KEY_ID/B2_APPLICATION_KEY (HTTP 401)."
        )
    response.raise_for_status()
    payload = response.json()
    storage = ((payload.get("apiInfo") or {}).get("storageApi") or {})
    if not storage:
        raise RuntimeError("Backblaze authorization returned no storageApi")
    return storage


def _resolve_bucket(storage: dict) -> tuple[str, str, str]:
    allowed = storage.get("allowed") or {}
    capabilities = set(allowed.get("capabilities") or [])
    missing = sorted(REQUIRED_CAPABILITIES - capabilities)
    if missing:
        raise RuntimeError(
            "Backblaze application key is missing capabilities: "
            + ", ".join(missing)
        )

    buckets = allowed.get("buckets") or []
    requested = (os.getenv("B2_BUCKET") or "").strip()

    selected: Optional[dict] = None
    if requested:
        selected = next(
            (item for item in buckets if item.get("name") == requested),
            None,
        )
        if selected is None and buckets:
            raise RuntimeError(
                "B2_BUCKET does not match a bucket allowed by this key"
            )
    elif len(buckets) == 1:
        selected = buckets[0]
    elif len(buckets) > 1:
        raise RuntimeError(
            "The application key can access multiple buckets; set B2_BUCKET."
        )

    if selected is None:
        if not requested:
            raise RuntimeError(
                "Could not discover a unique B2 bucket from the application key."
            )
        bucket = requested
    else:
        bucket = str(selected["name"])

    endpoint = str(storage.get("s3ApiUrl") or "").strip()
    if not endpoint:
        endpoint = (os.getenv("B2_ENDPOINT_URL") or "").strip()
    if not endpoint:
        raise RuntimeError("Could not determine Backblaze S3 endpoint")
    if not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint.lstrip("/")
    endpoint = endpoint.rstrip("/")

    region = (os.getenv("B2_REGION") or "").strip()
    if not region:
        match = re.search(
            r"^https://s3\.([a-z0-9-]+)\.backblazeb2\.com$",
            endpoint,
            flags=re.IGNORECASE,
        )
        if not match:
            raise RuntimeError(
                "Could not infer Backblaze region from S3 endpoint"
            )
        region = match.group(1)

    return bucket, region, endpoint


def _set_b2_environment(bucket: str, region: str, endpoint: str) -> None:
    os.environ["FOIA_STORAGE_BACKEND"] = "b2"
    os.environ["B2_BUCKET"] = bucket
    os.environ["B2_REGION"] = region
    os.environ["B2_ENDPOINT_URL"] = endpoint


def _restore_latest_if_available(config_path: str) -> bool:
    config = load_config(config_path)
    backups = list_database_backups(config)
    if not backups:
        print("No prior SQLite backup found; starting a fresh presentation database.")
        return False

    key = str(backups[0].get("Key") or "")
    if not key:
        print("Newest backup entry had no key; starting fresh.")
        return False

    destination = Path(
        config.storage.get("db_path", "data/foia_archive.db")
    )
    if destination.exists():
        # The workflow normally begins with an empty workspace data directory,
        # but avoid replacing an existing DB unless it came from this restore.
        destination.unlink()

    restore_database_from_b2(
        config,
        key=key,
        destination_path=destination,
        overwrite=True,
    )
    print("Restored newest verified SQLite backup from B2.")
    return True


def _write_summary(stats: dict) -> None:
    summary_path = (os.getenv("GITHUB_STEP_SUMMARY") or "").strip()
    lines = [
        "## FOIA Aggregator presentation corpus",
        "",
        f"- Agencies catalogued: **{stats['agencies']:,}**",
        f"- Offices/components: **{stats['offices']:,}**",
        f"- Active official sources: **{stats['active_sources']:,}**",
        f"- Records discovered: **{stats['records']:,}**",
        f"- Files archived: **{stats['archived_records']:,}**",
        f"- Full-text searchable: **{stats['searchable_records']:,}**",
        f"- OCR-assisted searchable: **{stats['ocr_records']:,}**",
        "",
    ]
    text = "\n".join(lines)
    print(text)
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as target:
            target.write(text)


def main() -> None:
    config_path = os.getenv("FOIA_CONFIG", "config/settings.yaml")
    key_id = _required_env("B2_KEY_ID")
    application_key = _required_env("B2_APPLICATION_KEY")
    _required_env("FOIA_API_KEY")

    print("Authorizing Backblaze application key and resolving bucket...")
    storage = _authorize(key_id, application_key)
    bucket, region, endpoint = _resolve_bucket(storage)
    _set_b2_environment(bucket, region, endpoint)
    print(f"Resolved B2 region {region}; bucket restriction verified.")

    config = load_config(config_path)
    Path(config.storage.get("files_dir", "data/files")).mkdir(
        parents=True,
        exist_ok=True,
    )

    restored = _restore_latest_if_available(config_path)
    if not restored:
        init_db(
            config.storage.get("db_path", "data/foia_archive.db"),
            config.storage.get("files_dir", "data/files"),
        )

    max_docs = config.crawler.get("max_docs_per_source", 1)
    print(
        "Starting bounded live crawl "
        f"(max_docs_per_source={max_docs}, "
        f"source_limit={config.crawler.get('source_limit') or 'all'}, "
        f"max_pages={config.crawler.get('max_pages_per_source')}, "
        f"max_depth={config.crawler.get('max_depth')})"
    )

    run_once(
        config_path=config_path,
        dry_run=False,
        max_docs_per_source=int(max_docs),
        refresh_metadata_enabled=True,
    )

    config = load_config(config_path)
    backup = backup_database_to_b2(config, force=True)
    if backup is None:
        raise RuntimeError("Forced SQLite backup was unexpectedly not created")

    conn = get_connection(config.storage.get("db_path"))
    try:
        stats = get_archive_stats(conn)
    finally:
        conn.close()

    _write_summary(stats)
    print(
        "PASS: bounded corpus crawl completed and verified SQLite snapshot "
        f"uploaded as {backup.key}"
    )


if __name__ == "__main__":
    main()
