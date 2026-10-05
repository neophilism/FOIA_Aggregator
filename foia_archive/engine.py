"""High level orchestration for discovery and scraping."""
from __future__ import annotations

from typing import Optional

from .discovery import refresh_metadata
from .scraper_core import HostRateLimiter, crawl_reading_room, get_reading_rooms_to_crawl
from .storage import init_db
from .utils import load_config, logger


def run_once(
    config_path: str = "config/settings.yaml",
    dry_run: Optional[bool] = None,
    max_docs_per_source: Optional[int] = None,
) -> None:
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

    logger.info("Refreshing metadata from FOIA Hub")
    try:
        refresh_metadata(cfg)
    except Exception as exc:  # metadata outage must not suppress known sources
        logger.warning(
            "FOIA.gov metadata refresh failed after retries; continuing with the last known active source set: %s: %s",
            type(exc).__name__,
            exc,
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
        except Exception as exc:
            logger.exception(
                "Unexpected crawl failure for reading room %s; continuing with remaining sources: %s",
                rr["id"],
                exc,
            )
