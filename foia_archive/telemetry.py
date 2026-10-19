"""Bounded, source-linked progress projection. No collection or external calls."""
from datetime import datetime, timezone
import re

SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
KINDS = frozenset(("crawl", "metadata", "index", "backup"))
STATES = frozenset(("success", "failure", "running", "unknown"))

def project_status(*, repository, source_sha, kind, state, observed_at, now=None, maximum_age_seconds=3600, evidence_verified=False):
    if not isinstance(repository, str) or not REPO.fullmatch(repository):
        raise ValueError("Invalid source repository")
    if not isinstance(source_sha, str) or not SHA.fullmatch(source_sha):
        raise ValueError("Invalid source SHA")
    if not isinstance(kind, str) or not isinstance(state, str) or kind not in KINDS or state not in STATES:
        raise ValueError("Invalid status category")
    if type(maximum_age_seconds) is not int or not 30 <= maximum_age_seconds <= 86400:
        raise ValueError("Invalid freshness policy")
    if evidence_verified not in (True, False) or type(evidence_verified) is not bool:
        raise ValueError("Evidence verification must be explicit")
    if now is not None and not isinstance(now, datetime):
        raise ValueError("Invalid current timestamp")
    if not isinstance(observed_at, str):
        raise ValueError("Timestamp required")
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if observed.tzinfo is None or current.tzinfo is None:
            raise ValueError("Time zone required")
        age = (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid observation timestamp") from exc
    if age < -60:
        raise ValueError("Observation timestamp is in the future")
    freshness = "stale" if age > maximum_age_seconds else "current"
    verified_state = state if freshness == "current" and evidence_verified else "unknown"
    return {"repository": repository, "source_sha": source_sha, "kind": kind,
            "state": verified_state, "freshness": freshness,
            "observed_at": observed.isoformat(), "evidence_required": True,
            "evidence_verified": evidence_verified}
