"""Utility helpers for FOIA archive."""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


@dataclass
class Config:
    data: Dict[str, Any]

    @property
    def crawler(self) -> Dict[str, Any]:
        return self.data.get("crawler", {})

    @property
    def foia_hub(self) -> Dict[str, Any]:
        return self.data.get("foia_hub", {})

    @property
    def storage(self) -> Dict[str, Any]:
        return self.data.get("storage", {})

    @property
    def downloader(self) -> Dict[str, Any]:
        return self.data.get("downloader", {})


logger = logging.getLogger("foia_archive")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def load_config(path: str, overrides: Optional[Dict[str, Any]] = None) -> Config:
    config_path = Path(path)
    with config_path.open("r") as f:
        data: Dict[str, Any] = yaml.safe_load(f) or {}

    storage = data.setdefault("storage", {})
    if not isinstance(storage, dict):
        storage = {}
        data["storage"] = storage

    env_db_path = (os.getenv("FOIA_DB_PATH") or "").strip()
    env_files_dir = (os.getenv("FOIA_FILES_DIR") or "").strip()
    env_storage_backend = (os.getenv("FOIA_STORAGE_BACKEND") or "").strip()
    if env_db_path:
        storage["db_path"] = env_db_path
    if env_files_dir:
        storage["files_dir"] = env_files_dir
    if env_storage_backend:
        storage["backend"] = env_storage_backend

    crawler = data.setdefault("crawler", {})
    if not isinstance(crawler, dict):
        crawler = {}
        data["crawler"] = crawler

    env_dry_run = (os.getenv("FOIA_CRAWLER_DRY_RUN") or "").strip()
    env_max_docs = (os.getenv("FOIA_MAX_DOCS_PER_SOURCE") or "").strip()
    env_interval = (os.getenv("FOIA_CRAWLER_INTERVAL_HOURS") or "").strip()
    if env_dry_run:
        crawler["dry_run"] = parse_bool(env_dry_run)
    if env_max_docs:
        crawler["max_docs_per_source"] = max(0, int(env_max_docs))
    if env_interval:
        crawler["interval_hours"] = float(env_interval)

    overrides = overrides or {}
    for section, values in overrides.items():
        if values is None:
            continue
        if section not in data or not isinstance(data[section], dict):
            data[section] = {}
        for key, value in values.items():
            if value is not None:
                data[section][key] = value
    return Config(data)


def clean_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in (".", "_", "-")) or "document"


def parse_bool(value: Optional[str]) -> Optional[bool]:
    """Parse a truthy/falsey string into a boolean.

    Accepts common variants like "true", "false", "yes", "no", "1", and "0" (case-insensitive).
    Returns ``None`` when value is ``None`` to allow callers to fall back to defaults.
    """

    if value is None:
        return None

    normalized = value.strip().lower()
    truthy = {"1", "true", "t", "yes", "y", "on"}
    falsey = {"0", "false", "f", "no", "n", "off"}

    if normalized in truthy:
        return True
    if normalized in falsey:
        return False

    raise ValueError(f"Cannot parse boolean value from '{value}'")


def slugify(value: str) -> str:
    """Return a filesystem- and URL-friendly slug for a label.

    The helper keeps alphanumerics, converts whitespace to hyphens, strips
    punctuation, and lowercases the result. Falls back to ``"item"`` when the
    computed slug is empty.
    """

    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return normalized or "item"
