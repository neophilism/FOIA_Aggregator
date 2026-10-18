"""High level orchestration for discovery and scraping."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Optional

from .database_backup import backup_database_to_b2
from .discovery import refresh_metadata
from .ingestion_journal import RunJournal, exclusive_writer, policy_fingerprint
from .scraper_core import (
    HostRateLimiter,
    crawl_reading_room,
    get_reading_rooms_to_crawl,
)
from .storage import (
    get_connection,
    init_db,
    record_reading_room_crawl_failure,
)
from .utils import load_config, logger


def _record_unexpected_source_failure(
    cfg,
    reading_room_id: int,
    exc: Exception,
) -> None:
    """Best-effort persistence for unexpected per-source failures."""
    conn = None
    try:
        conn = get_connection(cfg.storage.get("db_path"))
        record_reading_room_crawl_failure(
            conn,
            reading_room_id,
            datetime.now(timezone.utc).isoformat(),
            f"{type(exc).__name__}: {exc}"[:2000],
        )
    except Exception:
        logger.exception(
            "Could not persist unexpected failure state for reading room %s",
            reading_room_id,
        )
    finally:
        if conn is not None:
            conn.close()


def run_once(
    config_path: str = "config/settings.yaml",
    dry_run: Optional[bool] = None,
    max_docs_per_source: Optional[int] = None,
    refresh_metadata_enabled: bool = True,
    backup_enabled: bool = True,
) -> bool:
    """Run one crawl cycle.

    Returns True when metadata refresh succeeded or was intentionally skipped.
    Returns False when metadata refresh failed after retries; crawling still
    continues using the last known active source set.
    """
    cfg = load_config(
        config_path,
        overrides={
            "crawler": {
                "dry_run": dry_run,
                "max_docs_per_source": max_docs_per_source,
            }
        },
    )

    # Lock the resolved database path before schema writes or external work.
    with exclusive_writer(cfg.storage.get("db_path"), allow_nested=True):
        init_db(cfg.storage.get("db_path"), cfg.storage.get("files_dir"))
        return _run_cycle(cfg, refresh_metadata_enabled, backup_enabled)


def _run_cycle(cfg, refresh_metadata_enabled, backup_enabled):

    metadata_ok = True
    if refresh_metadata_enabled:
        logger.info("Refreshing metadata from FOIA Hub")
        try:
            refresh_metadata(cfg)
        except Exception as exc:
            metadata_ok = False
            logger.warning(
                "FOIA.gov metadata refresh failed after retries; continuing "
                "with the last known active source set: %s: %s",
                type(exc).__name__,
                exc,
            )
    else:
        logger.info(
            "Skipping FOIA.gov metadata refresh for this crawl cycle "
            "(refresh cadence not yet due)"
        )

    source_limit_raw = cfg.crawler.get("source_limit")
    source_limit = None
    if source_limit_raw is not None:
        try:
            parsed_limit = int(source_limit_raw)
            source_limit = parsed_limit if parsed_limit > 0 else None
        except (TypeError, ValueError):
            logger.warning(
                "Invalid crawler.source_limit; crawling all eligible sources"
            )

    rooms = get_reading_rooms_to_crawl(cfg, limit=source_limit)
    logger.info(
        "Crawling %s reading rooms%s",
        len(rooms),
        f" (source limit {source_limit})" if source_limit else "",
    )

    dry_run_flag = cfg.crawler.get("dry_run", True)
    max_docs = cfg.crawler.get("max_docs_per_source")
    rate_limiter = HostRateLimiter(
        float(cfg.crawler.get("per_host_delay_seconds", 0))
    )
    journal = RunJournal(cfg.storage.get("db_path"), policy_fingerprint(cfg), [rr["id"] for rr in rooms])
    completed = journal.completed_sources()
    source_failures = False
    cap_reached = False
    # Resume the original bounded plan, rather than silently shifting its denominator.
    room_by_id = {rr["id"]: rr for rr in rooms}
    try:
        for source_id in journal.source_ids:
            if source_id in completed:
                continue
            journal.source_started(source_id)
            if source_id not in room_by_id:
                journal.source_finished(source_id, "SOURCE_NOT_ELIGIBLE")
                source_failures = True
                continue
            try:
                crawl_reading_room(
                    source_id, cfg, dry_run=dry_run_flag,
                    max_docs=max_docs, rate_limiter=rate_limiter,
                )
                failed = journal.source_failed(source_id)
                journal.source_finished(source_id, "SOURCE_CRAWL_FAILED" if failed else None)
                source_failures = source_failures or failed
                if cfg.data.get("_storage_quota_reached"):
                    cap_reached = True
                    break
            except sqlite3.Error:
                raise
            except Exception as exc:
                _record_unexpected_source_failure(cfg, source_id, exc)
                journal.source_finished(source_id, type(exc).__name__)
                source_failures = True
        journal.checkpoint("PENDING" if backup_enabled else "EXTERNAL_CHECKPOINT_REQUIRED")
        journal.finish(metadata_ok=metadata_ok,
                       state="DEGRADED" if source_failures or not metadata_ok else "COMPLETED",
                       reason="storage_cap_reached" if cap_reached else "source_or_metadata_failure" if source_failures or not metadata_ok else "bounded_wave_finished")
    except BaseException as exc:
        journal.finish(metadata_ok=metadata_ok,
                       state="FAILED" if isinstance(exc, Exception) else "INTERRUPTED",
                       reason=type(exc).__name__)
        raise

    try:
        if backup_enabled:
            checkpoint = backup_database_to_b2(cfg)
            journal.checkpoint("VERIFIED" if checkpoint is not None else "DEFERRED", checkpoint.key if checkpoint is not None else None)
    except Exception as exc:
        journal.checkpoint("FAILED")
        journal.finish(metadata_ok=metadata_ok, state="DEGRADED", reason="checkpoint_failure")
        logger.warning("SQLite checkpoint failed for run %s; next cycle must retry (%s)", journal.id, type(exc).__name__)

    logger.info("Ingestion run %s finished; durable receipt retained", journal.id)
    return metadata_ok

