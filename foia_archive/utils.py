"""Utility helpers for FOIA archive."""
from __future__ import annotations

import logging
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


logger = logging.getLogger("foia_archive")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def load_config(path: str, overrides: Optional[Dict[str, Any]] = None) -> Config:
    config_path = Path(path)
    with config_path.open("r") as f:
        data: Dict[str, Any] = yaml.safe_load(f) or {}

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
