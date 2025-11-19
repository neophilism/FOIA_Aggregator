"""FOIA metadata discovery using FOIA.gov's public API."""
from __future__ import annotations

from typing import Dict, List

import requests

from .storage import get_connection, upsert_agency, upsert_office, upsert_reading_room
from .utils import Config, logger, slugify


def fetch_json(url: str, timeout: int, headers: Dict[str, str]) -> Dict:
    resp = requests.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.json()


def fetch_foia_units(base_url: str, timeout: int, headers: Dict[str, str]) -> List[Dict]:
    """Fetch FOIA units (components) from FOIA.gov.

    The FOIA.gov developer documentation exposes FOIA units via ``/foia_units``.
    Each unit corresponds to an office/component and carries agency metadata and
    any known FOIA library URLs. The response shape may evolve, so we keep
    parsing defensive and tolerant of optional fields.
    """

    url = f"{base_url.rstrip('/')}/foia_units"
    data = fetch_json(url, timeout, headers)
    # Some environments may wrap the list in a keyed object; unwrap if present.
    if isinstance(data, dict) and "foia_units" in data:
        return data.get("foia_units") or []
    if isinstance(data, list):
        return data
    logger.warning("Unexpected FOIA units payload shape: %s", type(data))
    return []


def refresh_metadata(config: Config) -> None:
    """Refresh local metadata for agencies, offices, and reading rooms."""

    base_url = config.foia_hub.get("base_url", "https://www.foia.gov/api")
    timeout = int(config.foia_hub.get("timeout_seconds", 30))
    headers = {"User-Agent": config.crawler.get("user_agent", "FOIAArchiveBot/0.1")}
    conn = get_connection(config.storage.get("db_path"))

    units = fetch_foia_units(base_url, timeout, headers)
    logger.info("Fetched %s FOIA units", len(units))

    agency_cache: Dict[str, int] = {}

    for unit in units:
        agency_name = unit.get("department") or unit.get("department_name") or unit.get("agency") or unit.get("agency_name")
        office_name = unit.get("name") or unit.get("office") or unit.get("component") or unit.get("bureau_name")

        agency_slug = slugify(agency_name or "agency")
        office_slug = unit.get("id") or slugify(f"{agency_slug}-{office_name or 'office'}")

        agency_id = agency_cache.get(agency_slug)
        if agency_id is None:
            agency_id = upsert_agency(conn, agency_slug, agency_name or agency_slug, unit)
            agency_cache[agency_slug] = agency_id

        office_id = upsert_office(conn, office_slug, office_name or office_slug, agency_id, unit)

        library_urls: List[str] = []
        for key in ("foia_library", "foia_library_url", "reading_room", "reading_room_url", "public_reading_room"):
            value = unit.get(key)
            if isinstance(value, list):
                library_urls.extend([u for u in value if u])
            elif isinstance(value, str) and value:
                library_urls.append(value)

        # De-duplicate and persist any discovered reading rooms.
        for url in {u.strip() for u in library_urls if u and isinstance(u, str)}:
            if url:
                upsert_reading_room(conn, url, unit.get("component") or unit.get("office") or office_name or "Reading Room", "office", agency_id, office_id)

    conn.close()
