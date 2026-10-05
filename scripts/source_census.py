"""Generate a live census of FOIA.gov components and publication sources."""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foia_archive.discovery import fetch_agencies, fetch_agency_components
from foia_archive.scraper_core import HostRateLimiter
from foia_archive.source_census import (
    attach_probe_results,
    build_component_census,
    probe_source,
)
from foia_archive.utils import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit FOIA.gov components and publication-source coverage."
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to crawler configuration.",
    )
    parser.add_argument(
        "--output-dir",
        default="source-census-results",
        help="Directory for JSON/CSV/Markdown outputs.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Probe every unique recognized source root.",
    )
    parser.add_argument(
        "--probe-workers",
        type=int,
        default=12,
        help="Maximum number of host groups probed concurrently.",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=12,
        help="Seconds allowed for each source-root request.",
    )
    parser.add_argument(
        "--probe-delay",
        type=float,
        default=0.25,
        help="Minimum delay between requests to the same host.",
    )
    parser.add_argument(
        "--max-html-kb",
        type=int,
        default=256,
        help="Maximum HTML bytes read from one source root, in KiB.",
    )
    return parser.parse_args()


def _source_component_map(census: dict) -> dict[str, list[dict]]:
    mapping: dict[str, list[dict]] = {}
    for source in census["source_instances"]:
        mapping.setdefault(source["url"], []).append(source)
    return mapping


def _probe_host_group(
    urls: list[str],
    *,
    user_agent: str,
    timeout: float,
    delay: float,
    max_html_bytes: int,
) -> dict:
    limiter = HostRateLimiter(delay)
    results = {}
    for url in urls:
        results[url] = probe_source(
            url,
            user_agent=user_agent,
            timeout=timeout,
            max_html_bytes=max_html_bytes,
            rate_limiter=limiter,
        )
    return results


def probe_all_sources(
    urls: list[str],
    *,
    user_agent: str,
    workers: int,
    timeout: float,
    delay: float,
    max_html_bytes: int,
) -> dict:
    groups: dict[str, list[str]] = {}
    for url in urls:
        hostname = (urlparse(url).hostname or "").lower()
        groups.setdefault(hostname, []).append(url)

    results = {}
    max_workers = max(1, min(int(workers), len(groups) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _probe_host_group,
                sorted(group_urls),
                user_agent=user_agent,
                timeout=timeout,
                delay=delay,
                max_html_bytes=max_html_bytes,
            ): hostname
            for hostname, group_urls in groups.items()
        }
        for future in as_completed(futures):
            hostname = futures[future]
            try:
                results.update(future.result())
            except Exception as exc:
                # A host-group programming/runtime failure should not suppress
                # the rest of the census; record an explicit synthetic error.
                for url in groups[hostname]:
                    from foia_archive.source_census import ProbeResult

                    results[url] = ProbeResult(
                        url=url,
                        final_url=None,
                        category="probe_error",
                        status_code=None,
                        mime_type=None,
                        direct_document_links=0,
                        crawlable_page_links=0,
                        adapter_hints=(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
    return results


def write_component_csv(census: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "agency_name",
                "agency_id",
                "component_name",
                "component_id",
                "recognized_source_count",
                "coverage_status",
                "recognized_sources",
                "candidate_ignored_urls",
                "ignored_url_count",
                "historical_note",
            ],
        )
        writer.writeheader()
        for row in census["components"]:
            writer.writerow(
                {
                    "agency_name": row["agency_name"],
                    "agency_id": row["agency_id"],
                    "component_name": row["component_name"],
                    "component_id": row["component_id"],
                    "recognized_source_count": row["recognized_source_count"],
                    "coverage_status": row.get("coverage_status", ""),
                    "recognized_sources": " | ".join(
                        f'{item["source_type"]}:{item["url"]}'
                        for item in row["recognized_sources"]
                    ),
                    "candidate_ignored_urls": " | ".join(
                        f'{item["field_path"]}:{item["url"]}'
                        for item in row["candidate_ignored_urls"]
                    ),
                    "ignored_url_count": len(row["ignored_urls"]),
                    "historical_note": row.get("historical_note") or "",
                }
            )


def write_source_csv(census: dict, path: Path) -> None:
    component_map = _source_component_map(census)
    probes = census.get("probes") or {}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "url",
                "source_type",
                "agencies",
                "components",
                "component_count",
                "probe_category",
                "status_code",
                "final_url",
                "mime_type",
                "direct_document_links",
                "crawlable_page_links",
                "adapter_hints",
                "probe_error",
            ],
        )
        writer.writeheader()
        for url in sorted(component_map):
            instances = component_map[url]
            source_types = sorted(
                {item["source_type"] for item in instances}
            )
            agencies = sorted(
                {item["agency_name"] for item in instances}
            )
            components = sorted(
                {item["component_name"] for item in instances}
            )
            probe = probes.get(url) or {}
            writer.writerow(
                {
                    "url": url,
                    "source_type": ",".join(source_types),
                    "agencies": " | ".join(agencies),
                    "components": " | ".join(components),
                    "component_count": len(instances),
                    "probe_category": probe.get("category", ""),
                    "status_code": probe.get("status_code", ""),
                    "final_url": probe.get("final_url", ""),
                    "mime_type": probe.get("mime_type", ""),
                    "direct_document_links": probe.get(
                        "direct_document_links", ""
                    ),
                    "crawlable_page_links": probe.get(
                        "crawlable_page_links", ""
                    ),
                    "adapter_hints": ",".join(
                        probe.get("adapter_hints") or []
                    ),
                    "probe_error": probe.get("error", ""),
                }
            )


def _markdown_table(rows: list[list[str]], headers: list[str]) -> list[str]:
    result = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        result.append(
            "| "
            + " | ".join(
                str(value).replace("|", "/").replace("\n", " ")
                for value in row
            )
            + " |"
        )
    return result


def write_markdown(census: dict, path: Path) -> None:
    summary = census["summary"]
    lines = [
        "# FOIA Source Census",
        "",
        f"Generated: {census['generated_at']}",
        "",
        "## Coverage summary",
        "",
        f"- FOIA.gov agencies returned: **{summary['agencies_returned']}**",
        f"- Agencies referenced by components: **{summary['agencies_referenced_by_components']}**",
        f"- Agencies with ≥1 recognized publication source: **{summary['agencies_with_recognized_source']}**",
        f"- Agencies with no recognized publication source (including historical): **{summary['agencies_without_recognized_source']}**",
        f"- Current agencies: **{summary['current_agencies']}**",
        f"- Current agencies with no recognized publication source: **{summary['current_agencies_without_recognized_source']}**",
        f"- Historical/defunct agencies retained by FOIA.gov: **{summary['historical_agencies']}**",
        f"- Agency components: **{summary['components']}**",
        f"- Historical/defunct components: **{summary['historical_components']}**",
        f"- Components with ≥1 recognized publication source: **{summary['components_with_recognized_source']}**",
        f"- Components with no recognized publication source: **{summary['components_without_recognized_source']}**",
        f"- No-source components with a publication-looking ignored URL: **{summary['components_without_source_but_candidate_url']}**",
        f"- Recognized source instances: **{summary['recognized_source_instances']}**",
        f"- Unique recognized source URLs: **{summary['unique_source_urls']}**",
        "",
        "### Recognized source types",
        "",
    ]
    for source_type, count in summary["source_type_counts"].items():
        lines.append(f"- {source_type}: **{count}**")

    agency_coverage: dict[str, dict] = {}
    for row in census["components"]:
        agency_key = row["agency_id"] or row["agency_name"]
        entry = agency_coverage.setdefault(
            agency_key,
            {
                "agency_name": row["agency_name"],
                "components": 0,
                "current_components": 0,
                "components_with_source": 0,
            },
        )
        entry["components"] += 1
        if not row.get("historical_note"):
            entry["current_components"] += 1
        if row["recognized_source_count"] > 0:
            entry["components_with_source"] += 1

    current_agencies_without_source = sorted(
        (
            entry
            for entry in agency_coverage.values()
            if entry["current_components"] > 0
            and entry["components_with_source"] == 0
        ),
        key=lambda item: item["agency_name"].lower(),
    )
    lines.extend(
        [
            "",
            "## Current agencies with no recognized publication source",
            "",
            f"Total: **{len(current_agencies_without_source)}**",
            "",
        ]
    )
    if current_agencies_without_source:
        lines.extend(
            _markdown_table(
                [
                    [
                        item["agency_name"],
                        str(item["current_components"]),
                    ]
                    for item in current_agencies_without_source
                ],
                ["Agency", "Current components"],
            )
        )
    else:
        lines.append("None.")

    historical_rows = [
        row for row in census["components"] if row.get("historical_note")
    ]
    lines.extend(
        [
            "",
            "## Historical/defunct agency components",
            "",
            f"Total: **{len(historical_rows)}**",
            "",
        ]
    )
    if historical_rows:
        lines.extend(
            _markdown_table(
                [
                    [
                        row["agency_name"],
                        row["component_name"],
                        row["historical_note"],
                    ]
                    for row in historical_rows
                ],
                ["Agency", "Component", "Reason"],
            )
        )
    else:
        lines.append("None.")

    probe_summary = census.get("probe_summary")
    if probe_summary:
        lines.extend(
            [
                "",
                "## Live source-root probes",
                "",
                f"- Unique source roots probed: **{probe_summary['sources_probed']}**",
                f"- Agencies represented by recognized sources: **{probe_summary['agencies_with_sources']}**",
                f"- Agencies with ≥1 reachable source from this runner: **{probe_summary['agencies_with_reachable_source']}**",
                f"- Agencies with no reachable source from this runner: **{probe_summary['agencies_without_reachable_source']}**",
            ]
        )
        for category, count in probe_summary["category_counts"].items():
            lines.append(f"- {category}: **{count}**")

        unavailable_agencies = probe_summary.get(
            "agencies_without_reachable_source_names"
        ) or []
        if unavailable_agencies:
            lines.extend(
                [
                    "",
                    "### Agencies with known sources but no reachable source from this runner",
                    "",
                ]
            )
            lines.extend(
                _markdown_table(
                    [
                        [
                            agency_name,
                            ", ".join(
                                probe_summary.get(
                                    "agency_problem_categories",
                                    {},
                                ).get(agency_name, [])
                            ),
                        ]
                        for agency_name in unavailable_agencies
                    ],
                    ["Agency", "Observed source categories"],
                )
            )

    no_source_candidates = [
        row
        for row in census["components"]
        if row["recognized_source_count"] == 0
        and row["candidate_ignored_urls"]
    ]
    lines.extend(
        [
            "",
            "## Highest-priority discovery gaps",
            "",
            "These components have no recognized publication source, but their ignored metadata still contains a URL that looks publication-related.",
            "",
        ]
    )
    if no_source_candidates:
        gap_rows = []
        for row in no_source_candidates:
            candidates = "; ".join(
                f'{item["field_path"]}: {item["url"]}'
                for item in row["candidate_ignored_urls"]
            )
            gap_rows.append(
                [
                    row["agency_name"],
                    row["component_name"],
                    candidates,
                ]
            )
        lines.extend(
            _markdown_table(
                gap_rows,
                ["Agency", "Component", "Candidate ignored URL(s)"],
            )
        )
    else:
        lines.append("None.")

    zero_source = [
        row
        for row in census["components"]
        if row["recognized_source_count"] == 0
    ]
    lines.extend(
        [
            "",
            "## All components with no recognized publication source",
            "",
            f"Total: **{len(zero_source)}**",
            "",
        ]
    )
    zero_rows = [
        [
            row["agency_name"],
            row["component_name"],
            row.get("coverage_status", "uncovered"),
            str(len(row["ignored_urls"])),
        ]
        for row in zero_source
    ]
    lines.extend(
        _markdown_table(
            zero_rows,
            ["Agency", "Component", "Status", "Ignored metadata URLs"],
        )
    )

    lines.extend(
        [
            "",
            "## Ignored URL-bearing metadata fields",
            "",
            "These field paths contain HTTP(S) URLs but are not currently classified as publication sources.",
            "",
        ]
    )
    ignored_rows = [
        [field, str(count)]
        for field, count in list(
            census["ignored_url_field_counts"].items()
        )[:50]
    ]
    lines.extend(
        _markdown_table(
            ignored_rows,
            ["Field path", "Occurrences"],
        )
    )

    if census.get("candidate_ignored_field_counts"):
        lines.extend(
            [
                "",
                "## Publication-looking ignored fields",
                "",
            ]
        )
        candidate_rows = [
            [field, str(count)]
            for field, count in census[
                "candidate_ignored_field_counts"
            ].items()
        ]
        lines.extend(
            _markdown_table(
                candidate_rows,
                ["Field path", "Occurrences"],
            )
        )

    probes = census.get("probes") or {}
    if probes:
        problem_categories = {
            "blocked",
            "rate_limited",
            "dead",
            "server_error",
            "http_error",
            "request_error",
            "blocked_by_safety",
            "probe_page_too_large",
            "probe_error",
            "adapter_candidate",
        }
        problem_rows = []
        component_map = _source_component_map(census)
        for url, probe in probes.items():
            if probe["category"] not in problem_categories:
                continue
            instances = component_map.get(url, [])
            problem_rows.append(
                [
                    probe["category"],
                    ", ".join(
                        sorted(
                            {
                                item["agency_name"]
                                for item in instances
                            }
                        )
                    ),
                    ", ".join(
                        sorted(
                            {
                                item["component_name"]
                                for item in instances
                            }
                        )
                    ),
                    url,
                    str(probe.get("status_code") or ""),
                    ", ".join(probe.get("adapter_hints") or []),
                ]
            )
        lines.extend(
            [
                "",
                "## Known-source crawlability problems",
                "",
            ]
        )
        if problem_rows:
            lines.extend(
                _markdown_table(
                    problem_rows,
                    [
                        "Category",
                        "Agency",
                        "Component",
                        "URL",
                        "HTTP",
                        "Adapter hints",
                    ],
                )
            )
        else:
            lines.append("None detected by the source-root probe.")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    base_url = config.foia_hub.get(
        "base_url",
        "https://api.foia.gov/api",
    )
    timeout = int(config.foia_hub.get("timeout_seconds", 30))
    api_key = (
        os.getenv("FOIA_API_KEY")
        or config.foia_hub.get("api_key")
    )
    if not api_key:
        raise RuntimeError(
            "FOIA API key missing. Set FOIA_API_KEY or foia_hub.api_key."
        )

    headers = {
        "User-Agent": config.crawler.get(
            "user_agent",
            "FOIAArchiveBot/0.1",
        ),
        "X-API-Key": api_key,
    }

    metadata_max_retries = int(config.foia_hub.get("max_retries", 5))
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
    census = build_component_census(
        agencies,
        components,
        included_agencies,
    )
    census["generated_at"] = datetime.now(timezone.utc).isoformat()

    if args.probe:
        urls = sorted(
            {
                item["url"]
                for item in census["source_instances"]
            }
        )
        probes = probe_all_sources(
            urls,
            user_agent=config.crawler.get(
                "user_agent",
                "FOIAArchiveBot/0.1",
            ),
            workers=args.probe_workers,
            timeout=args.probe_timeout,
            delay=args.probe_delay,
            max_html_bytes=max(1024, args.max_html_kb * 1024),
        )
        census = attach_probe_results(census, probes)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "source-census.json").write_text(
        json.dumps(census, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_component_csv(
        census,
        output_dir / "component-coverage.csv",
    )
    write_source_csv(
        census,
        output_dir / "source-probes.csv",
    )
    write_markdown(
        census,
        output_dir / "source-census.md",
    )

    print((output_dir / "source-census.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
