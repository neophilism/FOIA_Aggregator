"""High level orchestration for discovery and scraping."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Optional

from .database_backup import backup_database_to_b2
from .discovery import refresh_metadata
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

    init_db(cfg.storage.get("db_path"), cfg.storage.get("files_dir"))

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

    rooms = get_reading_rooms_to_crawl(cfg)
    logger.info("Crawling %s reading rooms", len(rooms))

    dry_run_flag = cfg.crawler.get("dry_run", True)
    max_docs = cfg.crawler.get("max_docs_per_source")
    rate_limiter = HostRateLimiter(
        float(cfg.crawler.get("per_host_delay_seconds", 0))
    )
    for rr in rooms:
        try:
            crawl_reading_room(
                rr["id"],
                cfg,
                dry_run=dry_run_flag,
                max_docs=max_docs,
                rate_limiter=rate_limiter,
            )
        except sqlite3.Error as exc:
            logger.exception(
                "Database failure while crawling reading room %s; aborting "
                "this cycle for controlled daemon retry: %s",
                rr["id"],
                exc,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected crawl failure for reading room %s; continuing "
                "with remaining sources: %s",
                rr["id"],
                exc,
            )
            _record_unexpected_source_failure(cfg, rr["id"], exc)

    try:
        backup_database_to_b2(cfg)
    except Exception as exc:
        logger.warning(
            "SQLite backup failed; crawl cycle remains successful and the "
            "next eligible cycle will retry: %s: %s",
            type(exc).__name__,
            exc,
        )

    return metadata_ok
