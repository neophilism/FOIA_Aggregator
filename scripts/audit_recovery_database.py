"""Compare the recovered SQLite artifact with the latest B2 backup and object manifest.

Read-only: never replaces the live database or creates B2 objects.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from foia_archive.archive_storage import B2ArchiveStorage, get_archive_storage
from foia_archive.database_backup import restore_database_from_b2
from foia_archive.utils import load_config


def describe_database(path: Path) -> dict:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}")
        rows = conn.execute(
            "SELECT storage_key, url, download_status FROM documents"
        ).fetchall()
    keys = {
        str(row[0]) for row in rows
        if row[2] == "downloaded" and row[0]
    }
    urls = {str(row[1]) for row in rows if row[1]}
    return {"documents": len(rows), "keys": keys, "urls": urls}


def audit(candidate: Path, *, recovered_path: Path) -> dict:
    config = load_config("config/settings.yaml")
    backend = get_archive_storage(config)
    if not isinstance(backend, B2ArchiveStorage):
        raise RuntimeError("Archive recovery audit requires B2")
    latest = restore_database_from_b2(
        config, destination_path=recovered_path, overwrite=True
    )
    before = describe_database(latest)
    after = describe_database(candidate)
    manifest = backend.current_object_manifest(prefix="documents/")
    data = {
        "latest_b2_archive_rows": before["documents"],
        "candidate_archive_rows": after["documents"],
        "latest_b2_archived_objects": len(before["keys"]),
        "candidate_archived_objects": len(after["keys"]),
        "candidate_keys_missing_from_b2": len(after["keys"] - set(manifest)),
        "candidate_keys_new_to_db": len(after["keys"] - before["keys"]),
        "b2_backup_keys_missing_from_candidate": len(before["keys"] - after["keys"]),
        "b2_backup_document_urls_missing_from_candidate": len(before["urls"] - after["urls"]),
        "b2_document_objects_not_referenced_by_candidate": len(set(manifest) - after["keys"]),
        "candidate_appears_safe_for_archived_objects": (
            before["keys"] <= after["keys"]
            and after["keys"] <= set(manifest)
        ),
    }
    return data


def main() -> None:
    candidate = Path(sys.argv[1] if len(sys.argv) > 1 else "recovery/foia_archive.db")
    result = audit(candidate, recovered_path=Path("recovery/latest-b2.sqlite"))
    print(json.dumps(result, indent=2))
    # This job intentionally does not promote either database even on success.
    summary_path = __import__("os").environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write("## Recovery candidate vs latest B2 backup\n\n")
            for key, value in result.items():
                output.write(f"- {key}: **{value}**\n")
            output.write("\nNo production database was modified.\n")
    if not result["candidate_appears_safe_for_archived_objects"]:
        raise SystemExit("Recovery candidate has missing archived objects or excludes B2 backup keys")


if __name__ == "__main__":
    main()
