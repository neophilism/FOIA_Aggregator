"""High level orchestration for discovery and scraping."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import time
import math
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


def _bounded_crawl_count(value, *, fallback: int, label: str) -> int:
    """Fail closed instead of silently crawling unlimited sources/documents."""
    if value is None:
        return fallback
    if type(value) is int:
        parsed = value
    elif type(value) is str and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError(f"Invalid {label} crawl limit")
    if not 1 <= parsed <= 10000:
        raise ValueError(f"Unbounded {label} crawl limit")
    return parsed


def _validated_dry_run(value) -> bool:
    """Never turn a malformed dry-run setting into permission to write."""
    if type(value) is bool:
        return value
    if type(value) is str:
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    raise ValueError("Invalid crawler.dry_run; explicit boolean required")


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

    # Validate resource limits before any database or external network action.
    source_limit = _bounded_crawl_count(
        cfg.crawler.get("source_limit"), fallback=10000, label="source"
    )
    max_docs = _bounded_crawl_count(
        cfg.crawler.get("max_docs_per_source"),
        fallback=10, label="per-source document",
    )

    dry_run_flag = _validated_dry_run(cfg.crawler.get("dry_run", True))
    cycle_limit_seconds = _bounded_crawl_count(
        cfg.crawler.get("maximum_cycle_seconds"),
        fallback=3600, label="cycle seconds",
    )
    delay_raw = cfg.crawler.get("per_host_delay_seconds", 0.5)
    try:
        if type(delay_raw) is bool:
            raise ValueError("Boolean delay invalid")
        per_host_delay = float(delay_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid per-host request delay") from exc
    if not math.isfinite(per_host_delay) or not 0 <= per_host_delay <= 60:
        raise ValueError("Unbounded per-host request delay")

    cycle_start = time.monotonic()

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

    rooms = get_reading_rooms_to_crawl(cfg, limit=source_limit)
    logger.info(
        "Crawling %s reading rooms%s",
        len(rooms),
        f" (source limit {source_limit})" if source_limit else "",
    )

    rate_limiter = HostRateLimiter(per_host_delay)
    for rr in rooms:
        if time.monotonic() - cycle_start >= cycle_limit_seconds:
            logger.warning(
                "Crawler cycle time budget exhausted; remaining sources deferred"
            )
            break
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
