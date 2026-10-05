"""Resilient scheduler for continuous crawler operation."""
from __future__ import annotations

import math
import time

from .engine import run_once
from .utils import load_config, logger


DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_ERROR_RETRY_SECONDS = 60.0
DEFAULT_METADATA_REFRESH_MINUTES = 360.0
DEFAULT_METADATA_FAILURE_RETRY_MINUTES = 15.0
MINIMUM_SLEEP_SECONDS = 1.0


def _positive_seconds(value, fallback: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(seconds) or seconds <= 0:
        return fallback
    return seconds


def _scheduler_timings(
    config_path: str,
) -> tuple[float, float, float, float]:
    """Return crawl, failed-cycle, metadata, and metadata-failure delays."""
    cfg = load_config(config_path)
    interval_seconds = (
        _positive_seconds(
            cfg.crawler.get("interval_hours", DEFAULT_INTERVAL_HOURS),
            DEFAULT_INTERVAL_HOURS,
        )
        * 3600
    )
    error_retry_seconds = _positive_seconds(
        cfg.crawler.get(
            "daemon_error_retry_seconds",
            DEFAULT_ERROR_RETRY_SECONDS,
        ),
        DEFAULT_ERROR_RETRY_SECONDS,
    )
    metadata_refresh_seconds = (
        _positive_seconds(
            cfg.foia_hub.get(
                "refresh_interval_minutes",
                DEFAULT_METADATA_REFRESH_MINUTES,
            ),
            DEFAULT_METADATA_REFRESH_MINUTES,
        )
        * 60
    )
    metadata_failure_retry_seconds = (
        _positive_seconds(
            cfg.foia_hub.get(
                "failure_retry_minutes",
                DEFAULT_METADATA_FAILURE_RETRY_MINUTES,
            ),
            DEFAULT_METADATA_FAILURE_RETRY_MINUTES,
        )
        * 60
    )
    return (
        max(MINIMUM_SLEEP_SECONDS, interval_seconds),
        max(MINIMUM_SLEEP_SECONDS, error_retry_seconds),
        max(MINIMUM_SLEEP_SECONDS, metadata_refresh_seconds),
        max(MINIMUM_SLEEP_SECONDS, metadata_failure_retry_seconds),
    )


def run_forever(config_path: str = "config/settings.yaml") -> None:
    """Run indefinitely without letting ordinary exceptions kill the daemon.

    KeyboardInterrupt and other BaseException subclasses intentionally propagate
    so operators and process supervisors can still stop the service.
    """
    last_good_interval = DEFAULT_INTERVAL_HOURS * 3600
    error_retry_seconds = DEFAULT_ERROR_RETRY_SECONDS
    next_metadata_refresh = 0.0

    while True:
        cycle_failed = False
        try:
            (
                interval_seconds,
                error_retry_seconds,
                metadata_refresh_seconds,
                metadata_failure_retry_seconds,
            ) = _scheduler_timings(config_path)
            last_good_interval = interval_seconds

            refresh_due = time.monotonic() >= next_metadata_refresh
            metadata_ok = run_once(
                config_path=config_path,
                refresh_metadata_enabled=refresh_due,
            )
            if refresh_due:
                metadata_delay = (
                    metadata_failure_retry_seconds
                    if metadata_ok is False
                    else metadata_refresh_seconds
                )
                next_metadata_refresh = time.monotonic() + metadata_delay
        except Exception as exc:
            cycle_failed = True
            logger.exception(
                "Crawler cycle failed unexpectedly; daemon remains alive and "
                "will retry after %.1f seconds: %s",
                error_retry_seconds,
                exc,
            )

        sleep_seconds = (
            error_retry_seconds if cycle_failed else last_good_interval
        )
        time.sleep(max(MINIMUM_SLEEP_SECONDS, sleep_seconds))
