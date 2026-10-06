"""Expand the B2-backed FOIA corpus toward a safe storage target.

This runner restores the newest verified SQLite snapshot, performs repeated
bounded crawl waves, checkpoints the database after every wave, and stops when
one of these conditions is met:

- actual billable B2 storage reaches the configured target;
- the document archive reaches its independent safety cap;
- a crawl wave makes no archival/storage progress;
- the configured maximum wave count is reached.

Actual B2 usage is measured across every concrete object version so database
backups and versioning overhead are included in capacity decisions.
"""
from __future__ import annotations

import os
from pathlib import Path

from foia_archive.archive_storage import B2ArchiveStorage, get_archive_storage
from foia_archive.database_backup import backup_database_to_b2
from foia_archive.engine import run_once
from foia_archive.storage import (
    archived_remote_bytes,
    get_archive_stats,
    get_connection,
    init_db,
)
from foia_archive.utils import load_config

from scripts.seed_presentation_corpus import (
    _authorize,
    _required_env,
    _resolve_bucket,
    _restore_latest_if_available,
    _set_b2_environment,
)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    return max(minimum, value)


def _format_gb(value: int) -> str:
    return f"{value / 1_000_000_000:.3f} GB"


def _stats(config) -> tuple[dict, int]:
    conn = get_connection(config.storage.get("db_path"))
    try:
        return get_archive_stats(conn), archived_remote_bytes(conn, "b2")
    finally:
        conn.close()


def _write_summary(
    *,
    stats: dict,
    actual_bytes: int,
    document_bytes: int,
    backup_bytes: int,
    other_bytes: int,
    target_bytes: int,
    waves_completed: int,
    stop_reason: str,
) -> None:
    lines = [
        "## FOIA Aggregator corpus expansion",
        "",
        f"- Crawl waves completed: **{waves_completed}**",
        f"- Stop reason: **{stop_reason}**",
        f"- Actual B2 stored bytes: **{actual_bytes:,}** ({_format_gb(actual_bytes)})",
        f"- B2 document-version bytes: **{document_bytes:,}** ({_format_gb(document_bytes)})",
        f"- B2 backup-version bytes: **{backup_bytes:,}** ({_format_gb(backup_bytes)})",
        f"- B2 other-version bytes: **{other_bytes:,}** ({_format_gb(other_bytes)})",
        f"- Expansion target: **{target_bytes:,}** ({_format_gb(target_bytes)})",
        f"- Records discovered: **{stats['records']:,}**",
        f"- Files archived: **{stats['archived_records']:,}**",
        f"- Full-text searchable: **{stats['searchable_records']:,}**",
        f"- OCR-assisted searchable: **{stats['ocr_records']:,}**",
        "",
    ]
    output = "\n".join(lines)
    print(output)
    summary_path = (os.getenv("GITHUB_STEP_SUMMARY") or "").strip()
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as target:
            target.write(output)


def main() -> None:
    config_path = os.getenv("FOIA_CONFIG", "config/settings.yaml")
    key_id = _required_env("B2_KEY_ID")
    application_key = _required_env("B2_APPLICATION_KEY")
    _required_env("FOIA_API_KEY")

    target_total_bytes = _env_int(
        "FOIA_EXPANSION_TARGET_TOTAL_BYTES",
        9_000_000_000,
        minimum=1,
    )
    max_waves = _env_int("FOIA_EXPANSION_MAX_WAVES", 12, minimum=1)
    minimum_progress_bytes = _env_int(
        "FOIA_EXPANSION_MIN_PROGRESS_BYTES",
        1,
        minimum=0,
    )

    print("Authorizing Backblaze key and resolving capacity target...")
    storage_api = _authorize(key_id, application_key)
    bucket, region, endpoint = _resolve_bucket(storage_api)
    _set_b2_environment(bucket, region, endpoint)

    config = load_config(config_path)
    files_dir = Path(config.storage.get("files_dir", "data/files"))
    files_dir.mkdir(parents=True, exist_ok=True)

    skip_b2_restore = (
        (os.getenv("FOIA_SKIP_B2_RESTORE") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    restored = False
    if skip_b2_restore:
        existing_db = Path(
            config.storage.get("db_path", "data/foia_archive.db")
        )
        if existing_db.is_file() and existing_db.stat().st_size > 0:
            restored = True
            print(
                "Using pre-seeded SQLite database; skipping B2 restore "
                "to avoid Class B reads."
            )
    if not restored:
        restored = _restore_latest_if_available(config_path)
    if not restored:
        init_db(
            config.storage.get("db_path", "data/foia_archive.db"),
            files_dir,
        )

    config = load_config(config_path)
    backend = get_archive_storage(config)
    if not isinstance(backend, B2ArchiveStorage):
        raise RuntimeError("Corpus expansion requires B2 archive storage")

    waves_completed = 0
    stop_reason = "maximum waves reached"

    for wave in range(1, max_waves + 1):
        usage_before = backend.usage_summary()
        stats_before, db_document_bytes_before = _stats(config)
        print(
            f"Wave {wave}: B2 usage {_format_gb(usage_before.stored_bytes)}; "
            f"DB document archive {_format_gb(db_document_bytes_before)}; "
            f"{stats_before['archived_records']:,} files archived."
        )

        if usage_before.stored_bytes >= target_total_bytes:
            stop_reason = "B2 target reached"
            break
        if (
            backend.max_archive_bytes
            and db_document_bytes_before >= backend.max_archive_bytes
        ):
            stop_reason = "document archive safety cap reached"
            break

        run_once(
            config_path=config_path,
            dry_run=False,
            max_docs_per_source=int(
                config.crawler.get("max_docs_per_source", 20)
            ),
            refresh_metadata_enabled=(wave == 1),
        )
        waves_completed += 1

        checkpoint = backup_database_to_b2(config, force=True)
        if checkpoint is None:
            raise RuntimeError(
                "Expansion checkpoint backup was unexpectedly not created"
            )
        print(f"Checkpointed wave {wave} as {checkpoint.key}")

        usage_after = backend.usage_summary()
        stats_after, db_document_bytes_after = _stats(config)
        added_archives = (
            stats_after["archived_records"] - stats_before["archived_records"]
        )
        added_bytes = usage_after.stored_bytes - usage_before.stored_bytes
        print(
            f"Wave {wave} progress: +{added_archives:,} archived files, "
            f"+{max(0, added_bytes):,} stored bytes; "
            f"B2 now {_format_gb(usage_after.stored_bytes)}."
        )

        if usage_after.stored_bytes >= target_total_bytes:
            stop_reason = "B2 target reached"
            break
        if (
            backend.max_archive_bytes
            and db_document_bytes_after >= backend.max_archive_bytes
        ):
            stop_reason = "document archive safety cap reached"
            break
        if added_archives <= 0 and added_bytes < minimum_progress_bytes:
            stop_reason = "crawl wave made no storage progress"
            break

    final_usage = backend.usage_summary()
    final_stats, _ = _stats(config)
    _write_summary(
        stats=final_stats,
        actual_bytes=final_usage.stored_bytes,
        document_bytes=final_usage.document_bytes,
        backup_bytes=final_usage.backup_bytes,
        other_bytes=final_usage.other_bytes,
        target_bytes=target_total_bytes,
        waves_completed=waves_completed,
        stop_reason=stop_reason,
    )


if __name__ == "__main__":
    main()
