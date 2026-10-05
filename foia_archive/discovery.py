"""FOIA metadata discovery using FOIA.gov's public API."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Tuple, TypedDict
from urllib.parse import urlsplit, urlunsplit

import requests

from .source_overrides import current_source_url, curated_sources_for_component
from .storage import (
    deactivate_reading_rooms_not_seen,
    get_connection,
    upsert_agency,
    upsert_office,
    upsert_reading_room,
)
from .utils import Config, logger, slugify


RETRYABLE_METADATA_STATUS_CODES = {429, 500, 502, 503, 504}


def _metadata_retry_delay(
    response,
    attempt: int,
    base_seconds: float,
    max_delay_seconds: float,
) -> float:
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")

    delay: float | None = None
    if retry_after:
        try:
            delay = max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(
                    0.0,
                    retry_at.timestamp() - datetime.now(timezone.utc).timestamp(),
                )
            except (TypeError, ValueError, OverflowError):
                delay = None

    if delay is None:
        reset_value = (
            headers.get("X-RateLimit-Reset")
            or headers.get("x-ratelimit-reset")
            or headers.get("X-Rate-Limit-Reset")
        )
        if reset_value:
            try:
                reset_at = float(reset_value)
                delay = max(0.0, reset_at - time.time())
            except ValueError:
                delay = None

    if delay is None:
        delay = max(0.0, float(base_seconds)) * (2 ** attempt)

    return min(max(0.0, float(max_delay_seconds)), delay)


def fetch_json(
    url: str,
    timeout: int,
    headers: Dict[str, str],
    params: Dict | None = None,
    *,
    max_retries: int = 4,
    retry_backoff_seconds: float = 1.0,
    max_retry_delay_seconds: float = 60.0,
) -> Dict:
    """Fetch JSON with bounded retries for temporary FOIA.gov failures."""
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        resp = None
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers=headers,
                params=params,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= max_retries:
                raise
            delay = min(
                max(0.0, float(max_retry_delay_seconds)),
                max(0.0, float(retry_backoff_seconds)) * (2 ** attempt),
            )
            logger.warning(
                "FOIA.gov metadata transport error %s; retrying in %.1fs (%s/%s)",
                type(exc).__name__,
                delay,
                attempt + 1,
                max_retries,
            )
            if delay:
                time.sleep(delay)
            continue

        try:
            if resp.status_code in RETRYABLE_METADATA_STATUS_CODES:
                if attempt < max_retries:
                    delay = _metadata_retry_delay(
                        resp,
                        attempt,
                        retry_backoff_seconds,
                        max_retry_delay_seconds,
                    )
                    logger.warning(
                        "FOIA.gov metadata request returned HTTP %s; retrying in %.1fs (%s/%s)",
                        resp.status_code,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    if delay:
                        time.sleep(delay)
                    continue

            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise
                delay = min(
                    max(0.0, float(max_retry_delay_seconds)),
                    max(0.0, float(retry_backoff_seconds)) * (2 ** attempt),
                )
                logger.warning(
                    "FOIA.gov metadata returned invalid JSON; retrying in %.1fs (%s/%s)",
                    delay,
                    attempt + 1,
                    max_retries,
                )
                if delay:
                    time.sleep(delay)
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    if last_error is not None:
        raise RuntimeError(
            f"FOIA.gov metadata request retries exhausted for {url}: {last_error}"
        ) from last_error
    raise RuntimeError(f"FOIA.gov metadata request retries exhausted for {url}")


def _fetch_paginated(
    base_url: str,
    path: str,
    timeout: int,
    headers: Dict[str, str],
    params: Dict | None = None,
    *,
    max_retries: int = 4,
    retry_backoff_seconds: float = 1.0,
    max_retry_delay_seconds: float = 60.0,
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

        payload = fetch_json(
            next_url,
            timeout,
            headers,
            params=next_params,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            max_retry_delay_seconds=max_retry_delay_seconds,
        )
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

    return results, included


def fetch_agencies(
    base_url: str,
    timeout: int,
    headers: Dict[str, str],
    *,
    max_retries: int = 4,
    retry_backoff_seconds: float = 1.0,
    max_retry_delay_seconds: float = 60.0,
) -> List[Dict]:
    agencies, _ = _fetch_paginated(
        base_url,
        "agency",
        timeout,
        headers,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        max_retry_delay_seconds=max_retry_delay_seconds,
    )
    return agencies


def fetch_agency_components(
    base_url: str,
    timeout: int,
    headers: Dict[str, str],
    *,
    max_retries: int = 4,
    retry_backoff_seconds: float = 1.0,
    max_retry_delay_seconds: float = 60.0,
) -> Tuple[List[Dict], List[Dict]]:
    """Fetch FOIA agency components (units) from the FOIA.gov API.

    Returns a tuple of (components, included_agencies) where each list is a
    collection of JSON:API resource objects.
    """

    params = {"include": "agency"}
    return _fetch_paginated(
        base_url,
        "agency_components",
        timeout,
        headers,
        params=params,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        max_retry_delay_seconds=max_retry_delay_seconds,
    )


SOURCE_TYPE_PRIORITY = {
    "reading_room": 0,
    "foia_library": 1,
    "proactive_disclosure": 2,
    "frequently_requested_records": 3,
    "foia_website": 4,
}


class SourceCandidate(TypedDict):
    url: str
    source_type: str


def _classify_source_field(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    tokens = set(normalized.split("_"))

    if "reading" in tokens and any(token.startswith("room") for token in tokens):
        return "reading_room"
    if "foia" in tokens and any(token.startswith("librar") for token in tokens):
        return "foia_library"
    if "proactive" in tokens and (
        any(token.startswith("disclos") for token in tokens)
        or any(token.startswith("releas") for token in tokens)
    ):
        return "proactive_disclosure"
    if (
        "frequently" in tokens
        and "requested" in tokens
        and ("records" in tokens or "documents" in tokens)
    ):
        return "frequently_requested_records"
    return None


def _normalize_source_url(value: str) -> str | None:
    value = (value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _urls_in_value(value) -> List[str]:
    urls: List[str] = []
    if isinstance(value, str):
        normalized = _normalize_source_url(value)
        if normalized:
            urls.append(current_source_url(normalized))
    elif isinstance(value, dict):
        for nested in value.values():
            urls.extend(_urls_in_value(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            urls.extend(_urls_in_value(nested))
    return urls


FOIA_WEBSITE_EXCLUDED_PATH_TERMS = {
    "request",
    "submit",
    "submission",
    "status",
    "tracking",
    "track",
}


def _is_foia_website_fallback(url: str) -> bool:
    """Return True for a component website that is clearly its FOIA landing page."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    path = (parsed.path or "/").lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", path).strip("-")
    tokens = {token for token in normalized.split("-") if token}

    if tokens & FOIA_WEBSITE_EXCLUDED_PATH_TERMS:
        return False

    return (
        "foia" in tokens
        or {"freedom", "information"}.issubset(tokens)
        or "freedom-of-information" in normalized
    )


def extract_reading_room_sources(attrs: Dict) -> List[SourceCandidate]:
    """Extract only URLs explicitly identified as FOIA publication sources.

    Explicit reading-room/library/disclosure fields are preferred. If none are
    present, a component website is accepted only when its URL path is clearly
    FOIA-related and not a request/status/submission endpoint. Other general
    websites, request forms, generic resources, and unrelated links are ignored.
    """
    candidates: Dict[str, str] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                source_type = _classify_source_field(key)
                if source_type:
                    for url in _urls_in_value(nested):
                        previous = candidates.get(url)
                        if previous is None or (
                            SOURCE_TYPE_PRIORITY[source_type]
                            < SOURCE_TYPE_PRIORITY[previous]
                        ):
                            candidates[url] = source_type
                else:
                    walk(nested)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                walk(nested)

    walk(attrs)

    # Some FOIA.gov components omit reading_rooms but identify their FOIA
    # landing page as the component website. Use that only as a fallback, and
    # only when the URL path itself is unambiguously FOIA-related.
    if not candidates:
        for url in _urls_in_value(attrs.get("website")):
            if _is_foia_website_fallback(url):
                candidates[url] = "foia_website"

    return [
        {"url": url, "source_type": candidates[url]}
        for url in sorted(candidates)
    ]


def _extract_urls_from_attrs(attrs: Dict) -> List[str]:
    """Backward-compatible URL-only view of conservative source extraction."""
    return [source["url"] for source in extract_reading_room_sources(attrs)]


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
    metadata_max_retries = int(config.foia_hub.get("max_retries", 4))
    metadata_backoff = float(
        config.foia_hub.get("retry_backoff_seconds", 1)
    )
    metadata_max_delay = float(
        config.foia_hub.get("max_retry_delay_seconds", 60)
    )

    agencies = fetch_agencies(
        base_url,
        timeout,
        headers,
        max_retries=metadata_max_retries,
        retry_backoff_seconds=metadata_backoff,
        max_retry_delay_seconds=metadata_max_delay,
    )
    components, included_agencies = fetch_agency_components(
        base_url,
        timeout,
        headers,
        max_retries=metadata_max_retries,
        retry_backoff_seconds=metadata_backoff,
        max_retry_delay_seconds=metadata_max_delay,
    )
    logger.info("Fetched %s agencies and %s agency components", len(agencies), len(components))

    refresh_seen_at = datetime.now(timezone.utc).isoformat()
    conn = get_connection(config.storage.get("db_path"))

    agency_cache: Dict[str, int] = {}
    agency_lookup: Dict[str, Dict] = {a.get("id"): a for a in agencies + included_agencies}
    seen_source_count = 0

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

        # Only fields explicitly describing FOIA publication sources become
        # crawl targets. Request forms, agency homepages, and generic links are
        # intentionally not guessed into the reading-room set.
        sources = extract_reading_room_sources(attrs)
        if not sources:
            sources = [
                {
                    "url": source["url"],
                    "source_type": source["source_type"],
                }
                for source in curated_sources_for_component(component.get("id"))
            ]

        for source in sources:
            upsert_reading_room(
                conn,
                source["url"],
                attrs.get("title") or office_name or "Reading Room",
                "office",
                agency_id,
                office_id,
                source_type=source["source_type"],
                seen_at=refresh_seen_at,
            )
            seen_source_count += 1

    if not components:
        logger.warning(
            "FOIA metadata refresh returned zero components; preserving the existing active source set"
        )
    elif seen_source_count == 0:
        logger.warning(
            "FOIA metadata refresh returned components but no recognized publication-source fields; preserving the existing active source set"
        )
    else:
        deactivated = deactivate_reading_rooms_not_seen(conn, refresh_seen_at)
        if deactivated:
            logger.info(
                "Marked %s reading room sources inactive because they were not present in the latest complete metadata refresh",
                deactivated,
            )
    conn.close()
