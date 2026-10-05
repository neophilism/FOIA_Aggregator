"""Audit FOIA.gov component metadata and discovered publication sources."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .discovery import (
    _normalize_source_url,
    extract_reading_room_sources,
)
from .intelligence_community_sources import normalized_ic_elements
from .source_overrides import (
    current_source_url,
    curated_sources_for_component,
    historical_component_note,
)
from .scraper_core import (
    FileTooLarge,
    HostRateLimiter,
    RETRYABLE_STATUS_CODES,
    UnsafeURL,
    _close_response,
    _content_type,
    _crawl_scope,
    _read_limited_text,
    _request_with_safe_redirects,
    _retry_sleep,
    extract_crawl_targets,
    extract_document_links,
)


POSITIVE_SOURCE_TERMS = {
    "foia",
    "reading",
    "library",
    "libraries",
    "record",
    "records",
    "disclosure",
    "disclosures",
    "release",
    "releases",
    "archive",
    "archives",
    "frequently",
    "requested",
}
NEGATIVE_SOURCE_TERMS = {
    "request_form",
    "requestform",
    "submit",
    "submission",
    "status",
    "tracking",
    "contact",
}
ADAPTER_HINT_TOKENS = (
    "govqa",
    "nextrequest",
    "mycusthelp",
    "requestcenter",
    "publicrecords",
    "foiaxpress",
    "readingroom.aspx",
    "__viewstate",
    "__eventtarget",
)

REACHABLE_PROBE_CATEGORIES = {
    "document_producing",
    "reachable_navigable",
    "reachable_no_links",
    "adapter_candidate",
    "reachable_non_html",
    "probe_page_too_large",
}


@dataclass(frozen=True)
class URLField:
    path: str
    url: str


@dataclass(frozen=True)
class ProbeResult:
    url: str
    final_url: Optional[str]
    category: str
    status_code: Optional[int]
    mime_type: Optional[str]
    direct_document_links: int
    crawlable_page_links: int
    adapter_hints: Tuple[str, ...]
    error: Optional[str]


def _path_text(path: Tuple[str, ...]) -> str:
    return ".".join(part for part in path if part)


def iter_url_fields(value: Any, path: Tuple[str, ...] = ()) -> Iterable[URLField]:
    """Yield normalized HTTP(S) URLs and the metadata path that contained them."""
    if isinstance(value, str):
        normalized = _normalize_source_url(value)
        if normalized:
            yield URLField(
                path=_path_text(path),
                url=current_source_url(normalized),
            )
        return

    if isinstance(value, dict):
        for key, nested in value.items():
            yield from iter_url_fields(nested, path + (str(key),))
        return

    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from iter_url_fields(nested, path)


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", (text or "").lower())
        if token
    }


def looks_like_unclassified_source(field_path: str, url: str) -> bool:
    """Flag ignored metadata URLs that still look publication-related."""
    lowered_path = (field_path or "").lower()
    lowered_url = (url or "").lower()

    if any(term in lowered_path for term in NEGATIVE_SOURCE_TERMS):
        return False

    path_tokens = _tokenize(lowered_path)
    url_tokens = _tokenize(lowered_url)
    combined = path_tokens | url_tokens

    if {"reading", "room"}.issubset(combined):
        return True
    if "foia" in combined and (
        "library" in combined
        or "records" in combined
        or "record" in combined
        or "archive" in combined
        or "disclosure" in combined
        or "disclosures" in combined
        or "release" in combined
        or "releases" in combined
    ):
        return True
    return len(combined & POSITIVE_SOURCE_TERMS) >= 3


def _agency_lookup(agencies: List[Dict], included_agencies: List[Dict]) -> Dict[str, Dict]:
    return {
        item.get("id"): item
        for item in agencies + included_agencies
        if item.get("id")
    }


def build_component_census(
    agencies: List[Dict],
    components: List[Dict],
    included_agencies: List[Dict],
) -> Dict[str, Any]:
    """Build coverage records from FOIA.gov metadata without network probes."""
    agency_lookup = _agency_lookup(agencies, included_agencies)
    component_rows: List[Dict[str, Any]] = []
    source_instances: List[Dict[str, Any]] = []
    ignored_field_counter: Counter[str] = Counter()
    candidate_field_counter: Counter[str] = Counter()

    unique_source_urls: set[str] = set()
    components_with_source = 0
    components_without_source = 0
    no_source_candidate_components = 0

    for component in components:
        attrs = component.get("attributes") or {}
        component_id = component.get("id")
        component_name = (
            attrs.get("title")
            or attrs.get("abbreviation")
            or component_id
            or "component"
        )
        agency_ref = (
            component.get("relationships", {})
            .get("agency", {})
            .get("data", {})
        )
        agency_id = agency_ref.get("id") if isinstance(agency_ref, dict) else None
        agency_attrs = (agency_lookup.get(agency_id) or {}).get("attributes") or {}
        agency_name = (
            agency_attrs.get("name")
            or agency_attrs.get("abbreviation")
            or agency_id
            or "agency"
        )

        recognized = extract_reading_room_sources(attrs)
        if not recognized:
            recognized = [
                {
                    "url": source["url"],
                    "source_type": source["source_type"],
                }
                for source in curated_sources_for_component(component_id)
            ]
        history_note = historical_component_note(component_id)
        recognized_urls = {source["url"] for source in recognized}
        all_url_fields = list(iter_url_fields(attrs))

        ignored: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        seen_ignored_pairs: set[tuple[str, str]] = set()
        for field in all_url_fields:
            if field.url in recognized_urls:
                continue
            pair = (field.path, field.url)
            if pair in seen_ignored_pairs:
                continue
            seen_ignored_pairs.add(pair)
            ignored_field_counter[field.path or "(root)"] += 1
            item = {"field_path": field.path, "url": field.url}
            ignored.append(item)
            if looks_like_unclassified_source(field.path, field.url):
                candidates.append(item)
                candidate_field_counter[field.path or "(root)"] += 1

        if recognized:
            components_with_source += 1
        else:
            components_without_source += 1
            if candidates:
                no_source_candidate_components += 1

        for source in recognized:
            unique_source_urls.add(source["url"])
            source_instances.append(
                {
                    "agency_id": agency_id,
                    "agency_name": agency_name,
                    "component_id": component_id,
                    "component_name": component_name,
                    "source_type": source["source_type"],
                    "url": source["url"],
                }
            )

        component_rows.append(
            {
                "agency_id": agency_id,
                "agency_name": agency_name,
                "component_id": component_id,
                "component_name": component_name,
                "recognized_sources": recognized,
                "recognized_source_count": len(recognized),
                "ignored_urls": ignored,
                "candidate_ignored_urls": candidates,
                "historical_note": history_note,
            }
        )

    ic_elements = normalized_ic_elements()
    for element in ic_elements:
        for source in element["sources"]:
            url = source["url"]
            if url in unique_source_urls:
                continue
            unique_source_urls.add(url)
            source_instances.append(
                {
                    "agency_id": None,
                    "agency_name": element["parent_agency"],
                    "component_id": None,
                    "component_name": element["name"],
                    "source_type": f"ic_{source['mode']}",
                    "url": url,
                }
            )

    source_type_counts = Counter(
        source["source_type"] for source in source_instances
    )
    agency_ids = {
        row["agency_id"]
        for row in component_rows
        if row["agency_id"]
    }
    agency_ids_with_source = {
        row["agency_id"]
        for row in component_rows
        if row["agency_id"] and row["recognized_source_count"] > 0
    }
    agency_rows: Dict[str, List[Dict[str, Any]]] = {}
    for row in component_rows:
        if row["agency_id"]:
            agency_rows.setdefault(row["agency_id"], []).append(row)
    historical_agency_ids = {
        agency_id
        for agency_id, rows in agency_rows.items()
        if rows and all(row["historical_note"] for row in rows)
    }
    current_agency_ids = agency_ids - historical_agency_ids
    agency_ids_without_source = agency_ids - agency_ids_with_source
    current_agency_ids_without_source = (
        current_agency_ids - agency_ids_with_source
    )
    historical_component_count = sum(
        1 for row in component_rows if row["historical_note"]
    )

    for row in component_rows:
        if row["historical_note"]:
            coverage_status = "historical"
        elif row["recognized_source_count"] > 0:
            coverage_status = "direct"
        elif row["agency_id"] in agency_ids_with_source:
            coverage_status = "covered_by_agency"
        else:
            coverage_status = "uncovered"
        row["agency_has_recognized_source"] = (
            row["agency_id"] in agency_ids_with_source
        )
        row["coverage_status"] = coverage_status

    return {
        "summary": {
            "agencies_returned": len(agencies),
            "agencies_referenced_by_components": len(agency_ids),
            "agencies_with_recognized_source": len(agency_ids_with_source),
            "agencies_without_recognized_source": len(agency_ids_without_source),
            "current_agencies": len(current_agency_ids),
            "current_agencies_without_recognized_source": len(
                current_agency_ids_without_source
            ),
            "historical_agencies": len(historical_agency_ids),
            "components": len(components),
            "historical_components": historical_component_count,
            "components_with_recognized_source": components_with_source,
            "components_without_recognized_source": components_without_source,
            "components_without_source_but_candidate_url": no_source_candidate_components,
            "recognized_source_instances": len(source_instances),
            "unique_source_urls": len(unique_source_urls),
            "source_type_counts": dict(sorted(source_type_counts.items())),
        },
        "components": component_rows,
        "source_instances": source_instances,
        "ignored_url_field_counts": dict(
            ignored_field_counter.most_common()
        ),
        "candidate_ignored_field_counts": dict(
            candidate_field_counter.most_common()
        ),
        "intelligence_community": ic_elements,
    }


def _adapter_hints(url: str, html: str) -> Tuple[str, ...]:
    haystack = f"{url}\n{html}".lower()
    return tuple(
        token for token in ADAPTER_HINT_TOKENS if token in haystack
    )


def probe_source(
    url: str,
    *,
    user_agent: str,
    timeout: float = 15,
    max_redirects: int = 5,
    max_html_bytes: int = 256 * 1024,
    rate_limiter: Optional[HostRateLimiter] = None,
    max_retries: int = 2,
    retry_backoff_seconds: float = 0.5,
    max_retry_delay_seconds: float = 10.0,
) -> ProbeResult:
    """Probe one known source root without performing an archive crawl."""
    response = None
    final_url: Optional[str] = None

    for attempt in range(max_retries + 1):
        try:
            response, final_url = _request_with_safe_redirects(
                url,
                headers={"User-Agent": user_agent},
                timeout=timeout,
                max_redirects=max_redirects,
                rate_limiter=rate_limiter,
            )
            status_code = getattr(response, "status_code", None)
            mime_type = _content_type(response) or None

            if status_code in RETRYABLE_STATUS_CODES:
                headers_map = getattr(response, "headers", {}) or {}
                retry_after = (
                    headers_map.get("Retry-After")
                    or headers_map.get("retry-after")
                )
                rate_limit_reset = (
                    headers_map.get("X-RateLimit-Reset")
                    or headers_map.get("x-ratelimit-reset")
                    or headers_map.get("X-Rate-Limit-Reset")
                )
                if attempt < max_retries:
                    _close_response(response)
                    response = None
                    _retry_sleep(
                        attempt,
                        retry_backoff_seconds,
                        retry_after=retry_after,
                        rate_limit_reset=rate_limit_reset,
                        max_delay_seconds=max_retry_delay_seconds,
                    )
                    continue

            if status_code in {401, 403}:
                return ProbeResult(
                    url=url,
                    final_url=final_url,
                    category="blocked",
                    status_code=status_code,
                    mime_type=mime_type,
                    direct_document_links=0,
                    crawlable_page_links=0,
                    adapter_hints=(),
                    error=None,
                )
            if status_code == 429:
                return ProbeResult(
                    url=url,
                    final_url=final_url,
                    category="rate_limited",
                    status_code=status_code,
                    mime_type=mime_type,
                    direct_document_links=0,
                    crawlable_page_links=0,
                    adapter_hints=(),
                    error=None,
                )
            if status_code in {404, 410}:
                return ProbeResult(
                    url=url,
                    final_url=final_url,
                    category="dead",
                    status_code=status_code,
                    mime_type=mime_type,
                    direct_document_links=0,
                    crawlable_page_links=0,
                    adapter_hints=(),
                    error=None,
                )
            if status_code is not None and status_code >= 500:
                return ProbeResult(
                    url=url,
                    final_url=final_url,
                    category="server_error",
                    status_code=status_code,
                    mime_type=mime_type,
                    direct_document_links=0,
                    crawlable_page_links=0,
                    adapter_hints=(),
                    error=None,
                )
            if status_code is not None and not 200 <= status_code < 300:
                return ProbeResult(
                    url=url,
                    final_url=final_url,
                    category="http_error",
                    status_code=status_code,
                    mime_type=mime_type,
                    direct_document_links=0,
                    crawlable_page_links=0,
                    adapter_hints=(),
                    error=None,
                )

            if mime_type and mime_type not in {
                "text/html",
                "application/xhtml+xml",
            }:
                return ProbeResult(
                    url=url,
                    final_url=final_url,
                    category="reachable_non_html",
                    status_code=status_code,
                    mime_type=mime_type,
                    direct_document_links=0,
                    crawlable_page_links=0,
                    adapter_hints=(),
                    error=None,
                )

            html = _read_limited_text(response, max_html_bytes)
            documents = extract_document_links(html, final_url)
            try:
                scope = _crawl_scope(final_url)
                crawl_targets = extract_crawl_targets(
                    html,
                    final_url,
                    scope,
                    depth=1,
                )
            except Exception:
                crawl_targets = []
            hints = _adapter_hints(final_url, html)

            if documents:
                category = "document_producing"
            elif hints:
                category = "adapter_candidate"
            elif crawl_targets:
                category = "reachable_navigable"
            else:
                category = "reachable_no_links"

            return ProbeResult(
                url=url,
                final_url=final_url,
                category=category,
                status_code=status_code,
                mime_type=mime_type,
                direct_document_links=len(documents),
                crawlable_page_links=len(crawl_targets),
                adapter_hints=hints,
                error=None,
            )
        except UnsafeURL as exc:
            return ProbeResult(
                url=url,
                final_url=final_url,
                category="blocked_by_safety",
                status_code=None,
                mime_type=None,
                direct_document_links=0,
                crawlable_page_links=0,
                adapter_hints=(),
                error=f"{type(exc).__name__}: {exc}",
            )
        except FileTooLarge as exc:
            return ProbeResult(
                url=url,
                final_url=final_url,
                category="probe_page_too_large",
                status_code=None,
                mime_type=None,
                direct_document_links=0,
                crawlable_page_links=0,
                adapter_hints=(),
                error=f"{type(exc).__name__}: {exc}",
            )
        except (requests.Timeout, requests.ConnectionError, OSError) as exc:
            if attempt < max_retries:
                _retry_sleep(
                    attempt,
                    retry_backoff_seconds,
                    max_delay_seconds=max_retry_delay_seconds,
                )
                continue
            return ProbeResult(
                url=url,
                final_url=final_url,
                category="request_error",
                status_code=None,
                mime_type=None,
                direct_document_links=0,
                crawlable_page_links=0,
                adapter_hints=(),
                error=f"{type(exc).__name__}: {exc}",
            )
        except requests.RequestException as exc:
            return ProbeResult(
                url=url,
                final_url=final_url,
                category="request_error",
                status_code=None,
                mime_type=None,
                direct_document_links=0,
                crawlable_page_links=0,
                adapter_hints=(),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if response is not None:
                _close_response(response)
                response = None

    return ProbeResult(
        url=url,
        final_url=final_url,
        category="request_error",
        status_code=None,
        mime_type=None,
        direct_document_links=0,
        crawlable_page_links=0,
        adapter_hints=(),
        error="Probe retries exhausted",
    )


def attach_probe_results(
    census: Dict[str, Any],
    probe_results: Dict[str, ProbeResult],
) -> Dict[str, Any]:
    """Return a census copy enriched with unique-source probe results."""
    enriched = dict(census)
    probes = {
        url: asdict(result)
        for url, result in sorted(probe_results.items())
    }
    category_counts = Counter(
        result.category for result in probe_results.values()
    )

    agency_sources: Dict[str, set[str]] = {}
    agency_categories: Dict[str, set[str]] = {}
    for source in census.get("source_instances", []):
        agency_name = source.get("agency_name") or source.get("agency_id") or "agency"
        url = source.get("url")
        if not url:
            continue
        agency_sources.setdefault(agency_name, set()).add(url)
        result = probe_results.get(url)
        if result is not None:
            agency_categories.setdefault(agency_name, set()).add(
                result.category
            )

    agencies_with_reachable_source = sorted(
        agency_name
        for agency_name, categories in agency_categories.items()
        if categories & REACHABLE_PROBE_CATEGORIES
    )
    agencies_without_reachable_source = sorted(
        agency_name
        for agency_name in agency_sources
        if not (
            agency_categories.get(agency_name, set())
            & REACHABLE_PROBE_CATEGORIES
        )
    )
    agency_problem_categories = {
        agency_name: sorted(agency_categories.get(agency_name, set()))
        for agency_name in agencies_without_reachable_source
    }

    ic_rows: List[Dict[str, Any]] = []
    ic_reachable = 0
    ic_known_unreachable = 0
    ic_unprobed = 0
    for element in census.get("intelligence_community", []):
        source_rows = []
        categories: set[str] = set()
        for source in element["sources"]:
            result = probe_results.get(source["url"])
            source_row = dict(source)
            if result is not None:
                source_row["probe"] = asdict(result)
                categories.add(result.category)
            else:
                source_row["probe"] = None
            source_rows.append(source_row)

        if categories & REACHABLE_PROBE_CATEGORIES:
            coverage_status = "reachable"
            ic_reachable += 1
        elif categories:
            coverage_status = "known_unreachable"
            ic_known_unreachable += 1
        else:
            coverage_status = "unprobed"
            ic_unprobed += 1

        ic_rows.append(
            {
                "slug": element["slug"],
                "name": element["name"],
                "parent_agency": element["parent_agency"],
                "sources": source_rows,
                "probe_categories": sorted(categories),
                "coverage_status": coverage_status,
            }
        )

    enriched["intelligence_community"] = ic_rows
    enriched["ic_summary"] = {
        "elements": len(ic_rows),
        "elements_with_sources": sum(
            1 for row in ic_rows if row["sources"]
        ),
        "reachable_elements": ic_reachable,
        "known_unreachable_elements": ic_known_unreachable,
        "unprobed_elements": ic_unprobed,
    }
    enriched["probes"] = probes
    enriched["probe_summary"] = {
        "sources_probed": len(probe_results),
        "category_counts": dict(sorted(category_counts.items())),
        "agencies_with_sources": len(agency_sources),
        "agencies_with_reachable_source": len(agencies_with_reachable_source),
        "agencies_without_reachable_source": len(
            agencies_without_reachable_source
        ),
        "agencies_without_reachable_source_names": (
            agencies_without_reachable_source
        ),
        "agency_problem_categories": agency_problem_categories,
    }
    return enriched
