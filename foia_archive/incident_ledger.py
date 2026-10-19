"""Durable deduplication for source-verified alert decisions, without delivery."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Mapping

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SEVERITIES = frozenset(("warning", "critical"))
KINDS = frozenset(("crawl_failure", "source_stale", "backup_failure", "quota_warning"))

def ensure_incident_ledger(conn: sqlite3.Connection) -> None:
    """Initialize on the archive's existing single-writer connection."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_incidents (
            incident_id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            source_sha TEXT NOT NULL,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

def record_incident(conn: sqlite3.Connection, repository: str, decision: Mapping) -> dict:
    """Record only explicitly verified current measurements; never dispatch.

    The caller must validate the provider's authenticated evidence *before*
    calling evaluate_alert and passing its decision into this ledger.
    Transaction commit/rollback remains the caller's responsibility.
    """
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError("Invalid incident repository")
    if not isinstance(decision, Mapping):
        raise ValueError("Invalid incident decision")
    source_sha = decision.get("source_sha")
    kind = decision.get("kind")
    severity = decision.get("severity")
    observed_at = decision.get("observed_at")
    if (not isinstance(source_sha, str) or not SHA.fullmatch(source_sha)
            or not isinstance(kind, str) or kind not in KINDS
            or not isinstance(severity, str) or severity not in SEVERITIES
            or decision.get("freshness") != "current"
            or decision.get("evidence_verified") is not True
            or not isinstance(observed_at, str) or not observed_at):
        raise ValueError("Unverified or non-actionable incident")
    raw_key = "\x00".join((repository, source_sha, kind, severity))
    incident_id = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    ensure_incident_ledger(conn)
    cursor = conn.execute("""
        INSERT INTO alert_incidents
            (incident_id, repository, source_sha, kind, severity, observed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(incident_id) DO NOTHING
    """, (incident_id, repository, source_sha, kind, severity, observed_at))
    return {"incident_id": incident_id, "newly_recorded": cursor.rowcount == 1,
            "notification_sent": False}
