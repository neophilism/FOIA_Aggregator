"""Compare the recovered SQLite artifact with the latest B2 backup and object manifest.

Read-only: never replaces the live database or creates B2 objects.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from foia_archive.archive_storage import B2ArchiveStorage, get_archive_storage
from foia_archive.database_backup import restore_database_from_b2
from foia_archive.utils import load_config
from foia_archive.recovery_audit import compare_snapshots, describe_database


def audit(candidate: Path, *, recovered_path: Path) -> dict:
    if candidate.resolve() == recovered_path.resolve():
        raise ValueError("Recovery comparison must not overwrite its candidate")
    after = describe_database(candidate)
    config = load_config("config/settings.yaml")
    backend = get_archive_storage(config)
    if not isinstance(backend, B2ArchiveStorage):
        raise RuntimeError("Archive recovery audit requires B2")
    latest = restore_database_from_b2(
        config, destination_path=recovered_path, overwrite=False
    )
    before = describe_database(latest)
    manifest = backend.current_object_manifest(prefix="documents/")
    return compare_snapshots(before, after, manifest)


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
        raise SystemExit("Recovery candidate loses snapshot evidence or has missing/mismatched B2 objects")


if __name__ == "__main__":
    main()

