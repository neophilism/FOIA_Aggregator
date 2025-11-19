"""FOIA-Hub metadata discovery."""
from __future__ import annotations

import requests
from typing import Dict, List

from .storage import get_connection, upsert_agency, upsert_office, upsert_reading_room
from .utils import Config, logger


def fetch_json(url: str, timeout: int, headers: Dict[str, str]) -> Dict:
    resp = requests.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.json()


def fetch_agencies(base_url: str, timeout: int, headers: Dict[str, str]) -> List[Dict]:
    url = f"{base_url.rstrip('/')}/agency/"
    return fetch_json(url, timeout, headers)


def fetch_agency_detail(slug: str, base_url: str, timeout: int, headers: Dict[str, str]) -> Dict:
    url = f"{base_url.rstrip('/')}/agency/{slug}"
    return fetch_json(url, timeout, headers)


def fetch_offices(base_url: str, timeout: int, headers: Dict[str, str]) -> List[Dict]:
    url = f"{base_url.rstrip('/')}/office/"
    return fetch_json(url, timeout, headers)


def fetch_office_detail(slug: str, base_url: str, timeout: int, headers: Dict[str, str]) -> Dict:
    url = f"{base_url.rstrip('/')}/office/{slug}"
    return fetch_json(url, timeout, headers)


def refresh_metadata(config: Config) -> None:
    base_url = config.foia_hub.get("base_url", "https://www.foia.gov/api")
    timeout = int(config.foia_hub.get("timeout_seconds", 30))
    headers = {"User-Agent": config.crawler.get("user_agent", "FOIAArchiveBot/0.1")}
    conn = get_connection(config.storage.get("db_path"))

    agencies = fetch_agencies(base_url, timeout, headers)
    logger.info("Fetched %s agencies", len(agencies))
    agency_lookup = {}
    for agency in agencies:
        slug = agency.get("slug") or agency.get("id")
        if not slug:
            continue
        detail = fetch_agency_detail(slug, base_url, timeout, headers)
        agency_id = upsert_agency(conn, slug, detail.get("title") or detail.get("name") or slug, detail)
        agency_lookup[slug] = agency_id
        for library in detail.get("foia_libraries", []) or []:
            url = library.get("url")
            label = library.get("link_text") or library.get("title") or "Reading Room"
            if url:
                upsert_reading_room(conn, url, label, "agency", agency_id, None)

    offices = fetch_offices(base_url, timeout, headers)
    logger.info("Fetched %s offices", len(offices))
    for office in offices:
        slug = office.get("slug") or office.get("id")
        if not slug:
            continue
        detail = fetch_office_detail(slug, base_url, timeout, headers)
        agency_slug = detail.get("agency_slug") or detail.get("agency") or office.get("agency_slug")
        agency_id = agency_lookup.get(agency_slug)
        office_id = upsert_office(
            conn,
            slug,
            detail.get("title") or detail.get("name") or slug,
            agency_id,
            detail,
        )
        for library in detail.get("foia_libraries", []) or []:
            url = library.get("url")
            label = library.get("link_text") or library.get("title") or "Reading Room"
            if url:
                upsert_reading_room(conn, url, label, "office", agency_id, office_id)

    conn.close()
