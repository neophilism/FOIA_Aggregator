"""Promote a proven SQLite recovery candidate by writing a new B2 checkpoint.

Does not mutate or replace the existing B2 snapshot; old checkpoints remain.
"""
from __future__ import annotations

import os
from pathlib import Path

from foia_archive.database_backup import backup_database_to_b2, restore_database_from_b2
from foia_archive.utils import load_config

from scripts.audit_recovery_database import audit, describe_database


def main() -> None:
    candidate = Path("recovery/foia_archive.db")
    if not candidate.is_file():
        raise SystemExit("Validated recovery database is unavailable")

    before = audit(candidate, recovered_path=Path("recovery/pre-promotion.sqlite"))
    safe = (
        before["candidate_appears_safe_for_archived_objects"]
        and before["b2_backup_document_urls_missing_from_candidate"] == 0
        and before["candidate_archive_rows"] >= before["latest_b2_archive_rows"]
    )
    if not safe:
        raise SystemExit("Recovery candidate failed monotonicity and object-existence gate")

    # The archived snapshot is an append-only B2 backup under a fresh key.
    # Retain older checkpoints during this migration for roll-back ability.
    os.environ["FOIA_DB_PATH"] = str(candidate)
    config = load_config("config/settings.yaml")
    config.data.setdefault("database_backup", {})["retain_count"] = 20
    result = backup_database_to_b2(config, force=True)
    if result is None:
        raise SystemExit("Recovery checkpoint was not created")

    restored = restore_database_from_b2(
        config, key=result.key,
        destination_path=Path("recovery/promoted-check.sqlite"),
        overwrite=True,
    )
    original = describe_database(candidate)
    recovered = describe_database(restored)
    if original != recovered:
        raise SystemExit(
            "Promoted B2 checkpoint did not restore identical document URL/key sets"
        )

    print("Recovery snapshot promoted under new verified B2 key:", result.key)
    print("Archived object keys:", len(original["keys"]))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(
                "## Verified new recovery checkpoint\n\n"
                f"- New B2 backup: \`{result.key}\`\n"
                f"- Archived object keys: {len(original['keys']):,}\n"
                "- Previous B2 backups retained for rollback.\n"
                "- A Render restart/redeploy is needed to load the new snapshot.\n"
            )


if __name__ == "__main__":
    main()
