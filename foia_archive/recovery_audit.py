"""Read-only, conservative recovery evidence; never promotes a database.

IDs may change between snapshots. Compare stable URLs/slugs and content
fingerprints, not row counts or surrogate IDs. Divergence requires review;
this module intentionally never resolves it by overwriting either input.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def _digest(values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def describe_database(path: Path) -> dict:
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")  # All evidence is from one consistent read transaction.
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Recovery SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Recovery SQLite contains orphaned relationships")
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if versions != set(range(1, 7)):
            raise RuntimeError("Recovery audit requires the complete supported schema version 6")
        evidence = {name: set() for name in (
            "agencies", "offices", "sources", "document_metadata", "archived_documents",
            "document_sources", "text_rows", "fts_rows",
        )}
        evidence["agencies"].update(conn.execute("SELECT slug, name FROM agencies"))
        evidence["offices"].update(conn.execute("SELECT o.slug, a.slug FROM offices o LEFT JOIN agencies a ON a.id=o.agency_id"))
        evidence["sources"].update(conn.execute("SELECT r.url, a.slug, o.slug FROM reading_rooms r LEFT JOIN agencies a ON a.id=r.agency_id LEFT JOIN offices o ON o.id=r.office_id"))
        keys, urls, sizes = set(), set(), {}
        documents = 0
        columns = ("title", "filename", "file_type", "description", "published_date", "mime_type", "sha256", "file_size", "agency", "office", "primary_source")
        for row in conn.execute("""SELECT d.url,d.download_status,d.storage_key,d.storage_backend,
            d.title,d.filename,d.file_type,d.description,d.published_date,d.mime_type,d.sha256,d.file_size,
            a.slug,o.slug,r.url FROM documents d
            LEFT JOIN agencies a ON a.id=d.agency_id LEFT JOIN offices o ON o.id=d.office_id
            LEFT JOIN reading_rooms r ON r.id=d.reading_room_id"""):
            url, status, key, backend = row[:4]
            if not url or url in urls:
                raise RuntimeError("Recovery document identity is missing or duplicated")
            documents += 1
            urls.add(url)
            # Known evidence must survive; filling a previously unknown field is allowed.
            evidence["document_metadata"].update((url, field, value) for field, value in zip(columns, row[4:]) if value is not None and value != "")
            if status == "downloaded":
                if not key or backend != "b2":
                    raise RuntimeError("Recovery cannot prove a downloaded record without a B2 key")
                evidence["archived_documents"].add((url, backend, key))
                keys.add(key)
                size = row[11]
                if size is not None:
                    if not isinstance(size, int) or size < 0 or (key in sizes and sizes[key] != size):
                        raise RuntimeError("Recovery archive size evidence is invalid or conflicting")
                    sizes[key] = size
        for url, source in conn.execute("""SELECT d.url,r.url FROM document_sources s
            LEFT JOIN documents d ON d.id=s.document_id LEFT JOIN reading_rooms r ON r.id=s.reading_room_id"""):
            if not url or not source:
                raise RuntimeError("Recovery contains an orphaned source association")
            evidence["document_sources"].add((url, source))
        for row in conn.execute("""SELECT d.url,t.body,t.extraction_status,t.extraction_method,t.character_count,t.truncated
            FROM document_text t LEFT JOIN documents d ON d.id=t.document_id"""):
            if not row[0]:
                raise RuntimeError("Recovery contains orphaned extracted text")
            evidence["text_rows"].add((row[0], _digest(row[1:])))
        for url, body in conn.execute("SELECT d.url,f.body FROM document_fts f LEFT JOIN documents d ON d.id=f.rowid"):
            if not url:
                raise RuntimeError("Recovery contains an orphaned search row")
            evidence["fts_rows"].add((url, _digest(body)))
        return {"documents": documents, "keys": keys, "urls": urls, "sizes": sizes, "evidence": evidence}


def compare_snapshots(before: dict, after: dict, manifest: dict) -> dict:
    """Return counts/booleans only: no source text, signed URLs or credentials."""
    missing = {name: len(rows - after["evidence"][name]) for name, rows in before["evidence"].items()}
    missing_keys = len(after["keys"] - set(manifest))
    size_mismatches = sum(1 for key, size in after["sizes"].items() if key in manifest and manifest[key] != size)
    preserves = not any(missing.values()) and before["urls"] <= after["urls"]
    return {
        "latest_b2_archive_rows": before["documents"],
        "candidate_archive_rows": after["documents"],
        "latest_b2_archived_objects": len(before["keys"]),
        "candidate_archived_objects": len(after["keys"]),
        "candidate_keys_missing_from_b2": missing_keys,
        "candidate_object_size_mismatches": size_mismatches,
        "candidate_keys_new_to_db": len(after["keys"] - before["keys"]),
        "b2_backup_keys_missing_from_candidate": len(before["keys"] - after["keys"]),
        "b2_backup_document_urls_missing_from_candidate": len(before["urls"] - after["urls"]),
        "b2_document_objects_not_referenced_by_candidate": len(set(manifest) - after["keys"]),
        "missing_snapshot_evidence": missing,
        "candidate_preserves_snapshot": preserves,
        # Preserve the existing promotion API but strengthen its meaning.
        "candidate_appears_safe_for_archived_objects": preserves and missing_keys == 0 and size_mismatches == 0,
    }
