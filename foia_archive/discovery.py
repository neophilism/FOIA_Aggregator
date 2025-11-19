"""FOIA metadata discovery using FOIA.gov's public API."""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

import requests

from .storage import get_connection, upsert_agency, upsert_office, upsert_reading_room
from .utils import Config, logger, slugify


def fetch_json(url: str, timeout: int, headers: Dict[str, str], params: Dict | None = None) -> Dict:
    resp = requests.get(url, timeout=timeout, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_agency_components(base_url: str, timeout: int, headers: Dict[str, str]) -> Tuple[List[Dict], List[Dict]]:
    """Fetch FOIA agency components (units) from the FOIA.gov API.

    Returns a tuple of (components, included_agencies) where each list is a
    collection of JSON:API resource objects.
    """

    components: List[Dict] = []
    included_agencies: List[Dict] = []
    offset = 0
    page_limit = 100

    while True:
        params = {
            "include": "agency",
            "fields[agency_component]": "title,abbreviation,agency,request_form_url,website",
            "fields[agency]": "name,abbreviation",
            "page[limit]": page_limit,
            "page[offset]": offset,
        }
        url = f"{base_url.rstrip('/')}/agency_components"
        payload = fetch_json(url, timeout, headers, params=params)

        batch = payload.get("data") or []
        components.extend(batch)
        included = payload.get("included") or []
        included_agencies.extend([i for i in included if i.get("type") == "agency"])

        if len(batch) < page_limit:
            break
        offset += page_limit

    return components, included_agencies


def refresh_metadata(config: Config) -> None:
    """Refresh local metadata for agencies, offices, and reading rooms."""

    base_url = config.foia_hub.get("base_url", "https://api.foia.gov/api")
    timeout = int(config.foia_hub.get("timeout_seconds", 30))
    api_key = config.foia_hub.get("api_key") or os.getenv("FOIA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FOIA API key missing. Set FOIA_API_KEY environment variable or foia_hub.api_key in config."
        )

    headers = {
        "User-Agent": config.crawler.get("user_agent", "FOIAArchiveBot/0.1"),
        "X-API-Key": api_key,
    }
    conn = get_connection(config.storage.get("db_path"))

    components, included_agencies = fetch_agency_components(base_url, timeout, headers)
    logger.info("Fetched %s agency components", len(components))

    agency_cache: Dict[str, int] = {}
    agency_lookup: Dict[str, Dict] = {a.get("id"): a for a in included_agencies}

    for component in components:
        attrs = component.get("attributes", {})
        rel_agency_id = (
            component.get("relationships", {})
            .get("agency", {})
            .get("data", {})
            .get("id")
        )
        agency_attrs = (agency_lookup.get(rel_agency_id) or {}).get("attributes", {})
        agency_name = agency_attrs.get("name") or agency_attrs.get("abbreviation") or rel_agency_id or "agency"
        office_name = attrs.get("title") or attrs.get("abbreviation") or "office"

        agency_slug = slugify(agency_name or "agency")
        office_slug = component.get("id") or slugify(f"{agency_slug}-{office_name or 'office'}")

        agency_id = agency_cache.get(agency_slug)
        if agency_id is None:
            agency_id = upsert_agency(conn, agency_slug, agency_name or agency_slug, agency_attrs)
            agency_cache[agency_slug] = agency_id

        office_id = upsert_office(conn, office_slug, office_name or office_slug, agency_id, attrs)

        library_urls: List[str] = []
        for key in ("website", "request_form_url"):
            value = attrs.get(key)
            if isinstance(value, list):
                library_urls.extend([u for u in value if u])
            elif isinstance(value, str) and value:
                library_urls.append(value)

        # De-duplicate and persist any discovered reading rooms.
        for url in {u.strip() for u in library_urls if u and isinstance(u, str)}:
            if url:
                upsert_reading_room(
                    conn,
                    url,
                    attrs.get("title") or office_name or "Reading Room",
                    "office",
                    agency_id,
                    office_id,
                )

    conn.close()
