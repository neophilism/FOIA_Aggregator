"""Live audit of public-source coverage for all 18 Intelligence Community elements."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foia_archive.intelligence_community_sources import normalized_ic_elements
from foia_archive.scraper_core import HostRateLimiter
from foia_archive.source_census import REACHABLE_PROBE_CATEGORIES, probe_source


def probe_group(hostname: str, urls: list[str]) -> dict:
    limiter = HostRateLimiter(0.25)
    results = {}
    for url in sorted(urls):
        results[url] = probe_source(
            url,
            user_agent=(
                "FOIAArchiveICAudit/1.0 "
                "(+https://github.com/neophilism/FOIA_Aggregator)"
            ),
            timeout=15,
            max_html_bytes=1024 * 1024,
            rate_limiter=limiter,
            max_retries=2,
            retry_backoff_seconds=0.5,
            max_retry_delay_seconds=8,
        )
    return results


def main() -> int:
    elements = normalized_ic_elements()
    urls = sorted(
        {
            source["url"]
            for element in elements
            for source in element["sources"]
        }
    )
    groups: dict[str, list[str]] = {}
    for url in urls:
        groups.setdefault((urlparse(url).hostname or "").lower(), []).append(url)

    probes = {}
    with ThreadPoolExecutor(max_workers=min(10, len(groups))) as pool:
        futures = {
            pool.submit(probe_group, host, host_urls): host
            for host, host_urls in groups.items()
        }
        for future in as_completed(futures):
            probes.update(future.result())

    rows = []
    dead_elements = []
    reachable_elements = 0
    for element in elements:
        source_rows = []
        categories = set()
        for source in element["sources"]:
            result = probes[source["url"]]
            categories.add(result.category)
            source_rows.append(
                {
                    **source,
                    "probe": {
                        "category": result.category,
                        "status_code": result.status_code,
                        "final_url": result.final_url,
                        "mime_type": result.mime_type,
                        "direct_document_links": result.direct_document_links,
                        "crawlable_page_links": result.crawlable_page_links,
                        "adapter_hints": list(result.adapter_hints),
                        "error": result.error,
                    },
                }
            )

        if categories & REACHABLE_PROBE_CATEGORIES:
            status = "reachable"
            reachable_elements += 1
        elif categories and categories <= {"dead"}:
            status = "dead"
            dead_elements.append(element["name"])
        else:
            status = "known_unreachable"

        rows.append(
            {
                "slug": element["slug"],
                "name": element["name"],
                "parent_agency": element["parent_agency"],
                "status": status,
                "probe_categories": sorted(categories),
                "sources": source_rows,
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "elements": len(elements),
            "elements_with_sources": sum(1 for element in elements if element["sources"]),
            "unique_source_urls": len(urls),
            "reachable_elements": reachable_elements,
            "known_unreachable_elements": sum(
                1 for row in rows if row["status"] == "known_unreachable"
            ),
            "dead_elements": len(dead_elements),
        },
        "dead_element_names": dead_elements,
        "elements": rows,
    }

    out = Path("ic-source-audit-results")
    out.mkdir(parents=True, exist_ok=True)
    (out / "ic-source-audit.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Intelligence Community Public-Source Audit",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"- IC elements: **{report['summary']['elements']}**",
        f"- Elements with identified sources: **{report['summary']['elements_with_sources']}**",
        f"- Unique official source URLs: **{report['summary']['unique_source_urls']}**",
        f"- Elements with ≥1 reachable source from this runner: **{report['summary']['reachable_elements']}**",
        f"- Elements with known sources but none reachable from this runner: **{report['summary']['known_unreachable_elements']}**",
        f"- Elements whose registered sources are confirmed dead: **{report['summary']['dead_elements']}**",
        "",
        "| IC element | Status | Probe categories | Official source(s) |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        sources = "; ".join(
            f"{source['mode']}: {source['url']}"
            for source in row["sources"]
        )
        lines.append(
            "| {name} | {status} | {categories} | {sources} |".format(
                name=row["name"].replace("|", "/"),
                status=row["status"],
                categories=", ".join(row["probe_categories"]),
                sources=sources.replace("|", "/"),
            )
        )

    (out / "ic-source-audit.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print((out / "ic-source-audit.md").read_text())

    # Source completeness requires all 18 elements and no source confirmed dead.
    return 0 if len(elements) == 18 and not dead_elements else 1


if __name__ == "__main__":
    raise SystemExit(main())
