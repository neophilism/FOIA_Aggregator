"""Simple scheduler to run the engine periodically."""
from __future__ import annotations

import time

from .engine import run_once
from .utils import load_config


def run_forever(config_path: str = "config/settings.yaml") -> None:
    cfg = load_config(config_path)
    interval_hours = cfg.crawler.get("interval_hours", 6)

    while True:
        run_once(config_path=config_path)
        time.sleep(float(interval_hours) * 3600)
