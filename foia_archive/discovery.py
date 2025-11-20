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


def _fetch_paginated(
    base_url: str, path: str, timeout: int, headers: Dict[str, str], params: Dict | None = None
) -> Tuple[List[Dict], List[Dict]]:
    """Fetch all pages for a JSON:API endpoint following provided next links."""

    results: List[Dict] = []
    included: List[Dict] = []
    params = dict(params or {})
    params.setdefault("page[size]", 100)

    next_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    next_params = dict(params)
    seen_urls = set()

    while next_url:
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)

        payload = fetch_json(next_url, timeout, headers, params=next_params)
        batch = payload.get("data") or []
        results.extend(batch)
        included.extend(payload.get("included") or [])

        links = payload.get("links") or {}
        raw_next = links.get("next")
        if not raw_next or not batch:
            break

        next_href = raw_next.get("href") if isinstance(raw_next, dict) else raw_next
        if not next_href:
            break

        next_url = next_href
        # The next URL already encodes pagination parameters; avoid mixing param styles.
        next_params = None


def fetch_agencies(base_url: str, timeout: int, headers: Dict[str, str]) -> List[Dict]:
    agencies, _ = _fetch_paginated(base_url, "agency", timeout, headers)
    return agencies


def fetch_agency_components(base_url: str, timeout: int, headers: Dict[str, str]) -> Tuple[List[Dict], List[Dict]]:
    """Fetch FOIA agency components (units) from the FOIA.gov API.

    Returns a tuple of (components, included_agencies) where each list is a
    collection of JSON:API resource objects.
    """

    params = {"include": "agency"}
    return _fetch_paginated(base_url, "agency_components", timeout, headers, params=params)


def _extract_urls_from_attrs(attrs: Dict) -> List[str]:
    """Return all HTTP(S) URLs found within an attribute dict."""

    urls: List[str] = []

    def collect_targeted_fields():
        # Prefer likely reading-room fields before falling back to brute-force scanning.
        targeted_keys = [
            "reading_rooms",
            "reading_room",
            "foia_libraries",
            "foia_library",
            "resources",
            "links",
            "website",
            "websites",
            "request_form",
        ]
        for key in targeted_keys:
            if key in attrs:
                collect(attrs[key])

    def collect(value):
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                collect(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                collect(v)

    collect_targeted_fields()
    # Backfill with a generic crawl over the full attribute payload in case URLs live
    # in unexpected keys.
    collect(attrs)
    return urls


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

    agencies = fetch_agencies(base_url, timeout, headers)
    components, included_agencies = fetch_agency_components(base_url, timeout, headers)
    logger.info("Fetched %s agencies and %s agency components", len(agencies), len(components))

    agency_cache: Dict[str, int] = {}
    agency_lookup: Dict[str, Dict] = {a.get("id"): a for a in agencies + included_agencies}

    # Persist agencies up front so component handling can link to them reliably.
    for agency in agencies:
        agency_attrs = agency.get("attributes", {})
        agency_name = agency_attrs.get("name") or agency_attrs.get("abbreviation") or agency.get("id") or "agency"
        agency_slug = slugify(agency_name)
        agency_id = upsert_agency(conn, agency_slug, agency_name, agency_attrs)
        agency_cache[agency_slug] = agency_id

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

        # Capture any URLs from the component attributes; the FOIA API uses
        # multiple fields for forms, websites, and other publicly available
        # records links, so we scan all attribute values for HTTP(S) URLs.
        library_urls = _extract_urls_from_attrs(attrs)

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
