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
from .source_overrides import (
    curated_sources_for_component,
    historical_component_note,
)
from .scraper_core import (
    FileTooLarge,
    HostRateLimiter,
    UnsafeURL,
    _content_type,
    _crawl_scope,
    _read_limited_text,
    _request_with_safe_redirects,
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
            yield URLField(path=_path_text(path), url=normalized)
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
) -> ProbeResult:
    """Probe one known source root without performing an archive crawl."""
    response = None
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

        if mime_type and mime_type not in {"text/html", "application/xhtml+xml"}:
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
            final_url=None,
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
            final_url=None,
            category="probe_page_too_large",
            status_code=None,
            mime_type=None,
            direct_document_links=0,
            crawlable_page_links=0,
            adapter_hints=(),
            error=f"{type(exc).__name__}: {exc}",
        )
    except (requests.RequestException, OSError) as exc:
        return ProbeResult(
            url=url,
            final_url=None,
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
            close = getattr(response, "close", None)
            if callable(close):
                close()


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
    enriched["probes"] = probes
    enriched["probe_summary"] = {
        "sources_probed": len(probe_results),
        "category_counts": dict(sorted(category_counts.items())),
    }
    return enriched
