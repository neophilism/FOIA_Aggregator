"""Resilient scheduler for continuous crawler operation."""
from __future__ import annotations

import time

from .engine import run_once
from .utils import load_config, logger


DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_ERROR_RETRY_SECONDS = 60.0
MINIMUM_SLEEP_SECONDS = 1.0


def _positive_seconds(value, fallback: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    if seconds <= 0:
        return fallback
    return seconds


def _scheduler_timings(config_path: str) -> tuple[float, float]:
    """Return normal-cycle and failed-cycle sleep durations."""
    cfg = load_config(config_path)
    interval_seconds = _positive_seconds(
        float(cfg.crawler.get("interval_hours", DEFAULT_INTERVAL_HOURS)) * 3600,
        DEFAULT_INTERVAL_HOURS * 3600,
    )
    error_retry_seconds = _positive_seconds(
        cfg.crawler.get(
            "daemon_error_retry_seconds",
            DEFAULT_ERROR_RETRY_SECONDS,
        ),
        DEFAULT_ERROR_RETRY_SECONDS,
    )
    return (
        max(MINIMUM_SLEEP_SECONDS, interval_seconds),
        max(MINIMUM_SLEEP_SECONDS, error_retry_seconds),
    )


def run_forever(config_path: str = "config/settings.yaml") -> None:
    """Run indefinitely without letting ordinary exceptions kill the daemon.

    KeyboardInterrupt and other BaseException subclasses intentionally propagate
    so operators and process supervisors can still stop the service.
    """
    last_good_interval = DEFAULT_INTERVAL_HOURS * 3600
    error_retry_seconds = DEFAULT_ERROR_RETRY_SECONDS

    while True:
        cycle_failed = False
        try:
            interval_seconds, error_retry_seconds = _scheduler_timings(
                config_path
            )
            last_good_interval = interval_seconds
            run_once(config_path=config_path)
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
