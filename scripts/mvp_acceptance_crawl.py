"""Live MVP acceptance crawl against a small representative FOIA source set."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from foia_archive import scraper_core
from foia_archive.scraper_core import HostRateLimiter, crawl_reading_room
from foia_archive.storage import (
    get_connection,
    get_schema_version,
    init_db,
    query_documents,
    upsert_agency,
    upsert_office,
    upsert_reading_room,
)
from foia_archive.utils import Config


SOURCES = [
    {
        "key": "cia",
        "agency_slug": "cia",
        "agency_name": "Central Intelligence Agency",
        "office_slug": "cia-foia",
        "office_name": "CIA FOIA",
        "label": "CIA FOIA Electronic Reading Room",
        "url": "https://www.cia.gov/readingroom/",
    },
    {
        "key": "fbi",
        "agency_slug": "doj",
        "agency_name": "Department of Justice",
        "office_slug": "fbi",
        "office_name": "Federal Bureau of Investigation",
        "label": "FBI Vault",
        "url": "https://vault.fbi.gov/",
    },
    {
        "key": "doj-olc",
        "agency_slug": "doj",
        "agency_name": "Department of Justice",
        "office_slug": "olc",
        "office_name": "Office of Legal Counsel",
        "label": "OLC FOIA Electronic Reading Room",
        "url": "https://www.justice.gov/olc/olc-foia-electronic-reading-room",
    },
    {
        "key": "sec",
        "agency_slug": "sec",
        "agency_name": "Securities and Exchange Commission",
        "office_slug": "sec-foia",
        "office_name": "SEC FOIA Services",
        "label": "SEC Frequently Requested Documents",
        "url": "https://www.sec.gov/foia/frequently-requested-documents",
    },
    {
        "key": "dia",
        "agency_slug": "dod",
        "agency_name": "Department of Defense",
        "office_slug": "dia",
        "office_name": "Defense Intelligence Agency",
        "label": "DIA FOIA Electronic Reading Room",
        "url": "https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/",
    },
    {
        "key": "navair",
        "agency_slug": "dod",
        "agency_name": "Department of Defense",
        "office_slug": "navair",
        "office_name": "Naval Air Systems Command",
        "label": "NAVAIR FOIA Document Library",
        "url": "https://www.navair.navy.mil/foia/documents",
    },
    {
        "key": "dhs-oig",
        "agency_slug": "dhs",
        "agency_name": "Department of Homeland Security",
        "office_slug": "dhs-oig",
        "office_name": "DHS Office of Inspector General",
        "label": "DHS OIG PAL Reading Room",
        "url": "https://foia.oig.dhs.gov/app/ReadingRoom.aspx",
        "adapter_gap_expected": True,
    },
]


def make_config(root: Path) -> Config:
    return Config(
        {
            "crawler": {
                "dry_run": True,
                "max_docs_per_source": 3,
                "interval_hours": 6,
                "max_pages_per_source": 5,
                "max_depth": 2,
                "max_discovered_docs_per_source": 3,
                "page_timeout_seconds": 20,
                "page_max_size_mb": 4,
                "per_host_delay_seconds": 0.2,
                "user_agent": (
                    "FOIAArchiveAcceptance/1.0 "
                    "(+https://github.com/neophilism/FOIA_Aggregator)"
                ),
            },
            "downloader": {
                "timeout_seconds": 30,
                "max_file_size_mb": 20,
                "max_redirects": 5,
                "max_retries": 1,
                "retry_backoff_seconds": 1,
            },
            "storage": {
                "db_path": str(root / "acceptance.db"),
                "files_dir": str(root / "files"),
            },
        }
    )


def rows_for_source(conn, rr_id: int):
    return conn.execute(
        """
        SELECT d.*, ds.first_seen_at, ds.last_seen_at
        FROM documents d
        JOIN document_sources ds ON ds.document_id = d.id
        WHERE ds.reading_room_id = ?
        ORDER BY d.id
        """,
        (rr_id,),
    ).fetchall()


def status_counts(rows) -> Dict[str, int]:
    result: Dict[str, int] = defaultdict(int)
    for row in rows:
        result[row["download_status"] or "unknown"] += 1
    return dict(sorted(result.items()))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="foia-mvp-acceptance-"))
    report_dir = Path("acceptance-results")
    report_dir.mkdir(exist_ok=True)
    config = make_config(root)
    db_path = Path(config.storage["db_path"])
    files_dir = Path(config.storage["files_dir"])
    init_db(db_path, files_dir)

    report: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(root),
        "sources": {},
        "checks": {},
        "warnings": [],
        "errors": [],
    }

    source_ids: Dict[str, int] = {}
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        agency_ids: Dict[str, int] = {}
        for source in SOURCES:
            agency_id = agency_ids.get(source["agency_slug"])
            if agency_id is None:
                agency_id = upsert_agency(
                    conn,
                    source["agency_slug"],
                    source["agency_name"],
                    {"acceptance_seed": True},
                )
                agency_ids[source["agency_slug"]] = agency_id
            office_id = upsert_office(
                conn,
                source["office_slug"],
                source["office_name"],
                agency_id,
                {"acceptance_seed": True},
            )
            rr_id = upsert_reading_room(
                conn,
                source["url"],
                source["label"],
                "office",
                agency_id,
                office_id,
                source_type="reading_room",
                seen_at=now,
            )
            source_ids[source["key"]] = rr_id
            report["sources"][source["key"]] = {
                "url": source["url"],
                "reading_room_id": rr_id,
                "adapter_gap_expected": bool(
                    source.get("adapter_gap_expected", False)
                ),
                "page_requests": [],
                "page_final_urls": [],
                "downloads": [],
                "uncaught_errors": [],
            }
    finally:
        conn.close()

    current_source = {"key": None}
    original_fetch = scraper_core._fetch_crawl_resource
    original_download = scraper_core.download_document

    def recorded_fetch(url, cfg, limiter):
        key = current_source["key"]
        if key:
            report["sources"][key]["page_requests"].append(url)
        response, final_url = original_fetch(url, cfg, limiter)
        if key:
            report["sources"][key]["page_final_urls"].append(final_url)
        return response, final_url

    def recorded_download(url, filename_hint, cfg, rate_limiter=None):
        result = original_download(
            url,
            filename_hint,
            cfg,
            rate_limiter=rate_limiter,
        )
        key = current_source["key"]
        if key:
            report["sources"][key]["downloads"].append(
                {
                    "url": url,
                    "status": getattr(result, "status", "legacy"),
                    "file_size": getattr(result, "file_size", None),
                    "mime_type": getattr(result, "mime_type", None),
                    "error": getattr(result, "error", None),
                }
            )
        return result

    scraper_core._fetch_crawl_resource = recorded_fetch
    scraper_core.download_document = recorded_download

    limiter = HostRateLimiter(config.crawler["per_host_delay_seconds"])

    # Phase 1: discovery-only crawl.
    for source in SOURCES:
        key = source["key"]
        current_source["key"] = key
        try:
            crawl_reading_room(
                source_ids[key],
                config,
                dry_run=True,
                max_docs=3,
                rate_limiter=limiter,
            )
        except Exception as exc:  # acceptance harness must retain all failures
            report["sources"][key]["uncaught_errors"].append(
                f"dry-run: {type(exc).__name__}: {exc}"
            )
            report["errors"].append(
                {"source": key, "phase": "dry-run", "error": traceback.format_exc()}
            )

    conn = get_connection(db_path)
    try:
        for source in SOURCES:
            key = source["key"]
            rr_id = source_ids[key]
            rows = rows_for_source(conn, rr_id)
            health = conn.execute(
                """
                SELECT last_crawled_at, last_successful_crawl_at,
                       last_error, last_error_at
                FROM reading_rooms
                WHERE id = ?
                """,
                (rr_id,),
            ).fetchone()
            report["sources"][key]["after_dry_run"] = {
                "documents": len(rows),
                "statuses": status_counts(rows),
                "last_successful_crawl_at": health["last_successful_crawl_at"],
                "last_error": health["last_error"],
            }
    finally:
        conn.close()

    # Phase 2: live crawl and downloads.
    for source in SOURCES:
        key = source["key"]
        current_source["key"] = key
        try:
            crawl_reading_room(
                source_ids[key],
                config,
                dry_run=False,
                max_docs=None,
                rate_limiter=limiter,
            )
        except Exception as exc:
            report["sources"][key]["uncaught_errors"].append(
                f"live: {type(exc).__name__}: {exc}"
            )
            report["errors"].append(
                {"source": key, "phase": "live", "error": traceback.format_exc()}
            )

    conn = get_connection(db_path)
    try:
        total_documents = conn.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]
        total_relationships = conn.execute(
            "SELECT COUNT(*) FROM document_sources"
        ).fetchone()[0]
        downloaded_rows = conn.execute(
            """
            SELECT id, url, local_path, file_size, sha256, reading_room_id
            FROM documents
            WHERE download_status = 'downloaded'
            ORDER BY id
            """
        ).fetchall()

        for source in SOURCES:
            key = source["key"]
            rr_id = source_ids[key]
            rows = rows_for_source(conn, rr_id)
            health = conn.execute(
                """
                SELECT last_crawled_at, last_successful_crawl_at,
                       last_error, last_error_at
                FROM reading_rooms
                WHERE id = ?
                """,
                (rr_id,),
            ).fetchone()
            report["sources"][key]["after_live_run"] = {
                "documents": len(rows),
                "statuses": status_counts(rows),
                "last_successful_crawl_at": health["last_successful_crawl_at"],
                "last_error": health["last_error"],
            }

        report["live_totals"] = {
            "documents": total_documents,
            "source_relationships": total_relationships,
            "downloaded": len(downloaded_rows),
        }
    finally:
        conn.close()

    # Integrity/path checks for every successfully downloaded artifact.
    integrity_failures = []
    downloaded_source_ids = set()
    conn = get_connection(db_path)
    try:
        downloaded_rows = conn.execute(
            """
            SELECT d.id, d.url, d.local_path, d.file_size, d.sha256,
                   ds.reading_room_id
            FROM documents d
            JOIN document_sources ds ON ds.document_id = d.id
            WHERE d.download_status = 'downloaded'
            ORDER BY d.id, ds.reading_room_id
            """
        ).fetchall()
        verified_doc_ids = set()
        for row in downloaded_rows:
            downloaded_source_ids.add(row["reading_room_id"])
            if row["id"] in verified_doc_ids:
                continue
            verified_doc_ids.add(row["id"])
            local_path = Path(row["local_path"] or "")
            full_path = (files_dir / local_path).resolve()
            try:
                full_path.relative_to(files_dir.resolve())
            except ValueError:
                integrity_failures.append(
                    {"id": row["id"], "reason": "path escapes files_dir"}
                )
                continue
            if local_path.is_absolute() or not full_path.is_file():
                integrity_failures.append(
                    {"id": row["id"], "reason": "archived file missing/absolute"}
                )
                continue
            actual_size = full_path.stat().st_size
            actual_sha = hash_file(full_path)
            if actual_size <= 0 or row["file_size"] != actual_size:
                integrity_failures.append(
                    {
                        "id": row["id"],
                        "reason": "file size mismatch",
                        "db": row["file_size"],
                        "actual": actual_size,
                    }
                )
            if not row["sha256"] or row["sha256"] != actual_sha:
                integrity_failures.append(
                    {"id": row["id"], "reason": "sha256 mismatch"}
                )
    finally:
        conn.close()

    report["checks"]["download_integrity"] = {
        "passed": not integrity_failures,
        "failures": integrity_failures,
        "sources_with_downloads": len(downloaded_source_ids),
    }

    # Verify queued page targets remain within the final root crawl scope.
    scope_failures = []
    pagination_sources = []
    for source in SOURCES:
        key = source["key"]
        metrics = report["sources"][key]
        if len(metrics["page_requests"]) > 1:
            pagination_sources.append(key)
        if not metrics["page_final_urls"]:
            continue
        scope = scraper_core._crawl_scope(metrics["page_final_urls"][0])
        for target_url in metrics["page_requests"][1:]:
            if not scraper_core._url_in_scope(target_url, scope):
                scope_failures.append(
                    {"source": key, "url": target_url, "scope": scope.path_prefix}
                )
    report["checks"]["bounded_frontier"] = {
        "passed": not scope_failures,
        "failures": scope_failures,
        "sources_fetching_multiple_pages": pagination_sources,
    }

    # Simulate process restart / migration rerun.
    before_restart = report["live_totals"].copy()
    init_db(db_path, files_dir)
    conn = get_connection(db_path)
    try:
        after_restart = {
            "documents": conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0],
            "source_relationships": conn.execute(
                "SELECT COUNT(*) FROM document_sources"
            ).fetchone()[0],
            "schema_version": get_schema_version(conn),
        }
    finally:
        conn.close()

    report["checks"]["restart_persistence"] = {
        "passed": (
            after_restart["documents"] == before_restart["documents"]
            and after_restart["source_relationships"]
            == before_restart["source_relationships"]
            and after_restart["schema_version"] == 3
        ),
        "before": before_restart,
        "after": after_restart,
    }

    # Re-crawl all sources after restart; uniqueness constraints must remain clean.
    limiter = HostRateLimiter(config.crawler["per_host_delay_seconds"])
    for source in SOURCES:
        key = source["key"]
        current_source["key"] = key
        try:
            crawl_reading_room(
                source_ids[key],
                config,
                dry_run=False,
                max_docs=None,
                rate_limiter=limiter,
            )
        except Exception as exc:
            report["sources"][key]["uncaught_errors"].append(
                f"recrawl: {type(exc).__name__}: {exc}"
            )
            report["errors"].append(
                {"source": key, "phase": "recrawl", "error": traceback.format_exc()}
            )

    conn = get_connection(db_path)
    try:
        duplicate_urls = conn.execute(
            """
            SELECT url, COUNT(*) AS c
            FROM documents
            GROUP BY url
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        duplicate_relationships = conn.execute(
            """
            SELECT document_id, reading_room_id, COUNT(*) AS c
            FROM document_sources
            GROUP BY document_id, reading_room_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        report["checks"]["recrawl_deduplication"] = {
            "passed": not duplicate_urls and not duplicate_relationships,
            "duplicate_urls": [dict(row) for row in duplicate_urls],
            "duplicate_relationships": [
                dict(row) for row in duplicate_relationships
            ],
            "documents_after_recrawl": conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0],
        }
    finally:
        conn.close()

    # Delete one real archived file and require the source crawl to restore it.
    retry_check = {
        "passed": False,
        "exercised": False,
        "reason": "No successful download was available",
    }
    conn = get_connection(db_path)
    try:
        candidate = conn.execute(
            """
            SELECT d.id, d.url, d.local_path, d.sha256, ds.reading_room_id
            FROM documents d
            JOIN document_sources ds ON ds.document_id = d.id
            WHERE d.download_status = 'downloaded'
              AND d.local_path IS NOT NULL
            ORDER BY d.id
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    if candidate:
        retry_check = {
            "passed": False,
            "exercised": True,
            "document_id": candidate["id"],
            "url": candidate["url"],
        }
        target = files_dir / candidate["local_path"]
        original_sha = candidate["sha256"]
        target.unlink(missing_ok=True)
        rr_id = candidate["reading_room_id"]
        key = next(
            (
                source["key"]
                for source in SOURCES
                if source_ids[source["key"]] == rr_id
            ),
            "retry-source",
        )
        current_source["key"] = key
        try:
            crawl_reading_room(
                rr_id,
                config,
                dry_run=False,
                max_docs=None,
                rate_limiter=HostRateLimiter(
                    config.crawler["per_host_delay_seconds"]
                ),
            )
        except Exception as exc:
            retry_check["reason"] = f"retry crawl raised {type(exc).__name__}: {exc}"
        else:
            conn = get_connection(db_path)
            try:
                refreshed = conn.execute(
                    """
                    SELECT local_path, sha256, download_status
                    FROM documents WHERE id = ?
                    """,
                    (candidate["id"],),
                ).fetchone()
            finally:
                conn.close()
            restored = (
                refreshed
                and refreshed["download_status"] == "downloaded"
                and refreshed["local_path"]
                and (files_dir / refreshed["local_path"]).is_file()
            )
            retry_check["passed"] = bool(
                restored and refreshed["sha256"] == original_sha
            )
            retry_check["reason"] = (
                "file restored with matching SHA-256"
                if retry_check["passed"]
                else "file was not restored with matching metadata"
            )
    report["checks"]["missing_file_retry"] = retry_check

    # Exercise publication-date filters when live metadata supplied a date.
    conn = get_connection(db_path)
    try:
        dated = conn.execute(
            """
            SELECT id, published_date
            FROM documents
            WHERE published_date IS NOT NULL AND published_date != ''
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if dated:
            rows = query_documents(
                conn,
                start_date=dated["published_date"],
                end_date=dated["published_date"],
            )
            report["checks"]["published_date_filter"] = {
                "passed": any(row["id"] == dated["id"] for row in rows),
                "exercised": True,
                "date": dated["published_date"],
                "document_id": dated["id"],
            }
        else:
            report["checks"]["published_date_filter"] = {
                "passed": True,
                "exercised": False,
                "reason": "live sample supplied no explicit publication metadata",
            }
    finally:
        conn.close()

    # Basic URL safety invariant for every persisted document.
    conn = get_connection(db_path)
    try:
        unsafe_rows = [
            row["url"]
            for row in conn.execute("SELECT url FROM documents").fetchall()
            if not scraper_core._is_http_url(row["url"])
        ]
        root_successes = conn.execute(
            """
            SELECT COUNT(*)
            FROM reading_rooms
            WHERE last_successful_crawl_at IS NOT NULL
            """
        ).fetchone()[0]
        sources_with_docs = conn.execute(
            """
            SELECT COUNT(DISTINCT reading_room_id)
            FROM document_sources
            """
        ).fetchone()[0]
        total_downloaded = conn.execute(
            """
            SELECT COUNT(*)
            FROM documents
            WHERE download_status = 'downloaded'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    report["checks"]["persisted_url_safety"] = {
        "passed": not unsafe_rows,
        "unsafe_urls": unsafe_rows,
    }

    uncaught = sum(
        len(report["sources"][source["key"]]["uncaught_errors"])
        for source in SOURCES
    )
    report["acceptance_summary"] = {
        "root_sources_successful": root_successes,
        "total_sources": len(SOURCES),
        "sources_with_documents": sources_with_docs,
        "successful_downloads": total_downloaded,
        "uncaught_errors": uncaught,
        "pagination_observed": bool(pagination_sources),
    }

    critical_checks = [
        uncaught == 0,
        root_successes >= 4,
        sources_with_docs >= 3,
        total_downloaded >= 3,
        report["checks"]["download_integrity"]["passed"],
        report["checks"]["download_integrity"]["sources_with_downloads"] >= 2,
        report["checks"]["bounded_frontier"]["passed"],
        bool(pagination_sources),
        report["checks"]["restart_persistence"]["passed"],
        report["checks"]["recrawl_deduplication"]["passed"],
        report["checks"]["missing_file_retry"]["passed"],
        report["checks"]["published_date_filter"]["passed"],
        report["checks"]["persisted_url_safety"]["passed"],
    ]
    report["passed"] = all(critical_checks)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    json_path = report_dir / "mvp-acceptance-report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))

    md_lines = [
        "# FOIA Aggregator MVP Live Acceptance Crawl",
        "",
        f"Overall: **{'PASS' if report['passed'] else 'FAIL'}**",
        "",
        "## Summary",
        "",
        f"- Sources successfully fetched: {root_successes}/{len(SOURCES)}",
        f"- Sources with discovered documents: {sources_with_docs}",
        f"- Successfully downloaded documents: {total_downloaded}",
        f"- Uncaught crawler exceptions: {uncaught}",
        f"- Pagination/multi-page traversal observed: {'yes' if pagination_sources else 'no'}",
        f"- Schema version after restart: {after_restart['schema_version']}",
        "",
        "## Sources",
        "",
        "| Source | Pages requested | Docs after live run | Statuses | Last error |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for source in SOURCES:
        key = source["key"]
        item = report["sources"][key]
        live = item.get("after_live_run", {})
        md_lines.append(
            "| {key} | {pages} | {docs} | {statuses} | {error} |".format(
                key=key,
                pages=len(item["page_requests"]),
                docs=live.get("documents", 0),
                statuses=json.dumps(live.get("statuses", {})),
                error=(live.get("last_error") or "").replace("|", "/")[:120],
            )
        )

    md_lines.extend(
        [
            "",
            "## Checks",
            "",
        ]
    )
    for name, check in report["checks"].items():
        md_lines.append(
            f"- **{name}:** {'PASS' if check.get('passed') else 'FAIL'}"
            + (
                " (not exercised)"
                if check.get("exercised") is False
                else ""
            )
        )

    (report_dir / "mvp-acceptance-report.md").write_text(
        "\n".join(md_lines) + "\n"
    )

    print((report_dir / "mvp-acceptance-report.md").read_text())
    print(f"JSON report: {json_path}")

    # Keep no large downloads in the Actions workspace after report creation.
    shutil.rmtree(root, ignore_errors=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
