"""Source-bound non-delivering alert decisions; no outbound notification or secrets."""
from datetime import datetime, timezone
from math import isfinite

KINDS = frozenset(("crawl_failure", "source_stale", "backup_failure", "quota_warning"))
LEVELS = frozenset(("warning", "critical"))

def evaluate_alert(*, kind, value, warning_threshold, critical_threshold,
                   observed_at, source_sha, now=None, maximum_age_seconds=3600,
                   evidence_verified=False):
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(source_sha, str) or len(source_sha) != 40 or not all(ch in "0123456789abcdef" for ch in source_sha):
        raise ValueError("Invalid alert source")
    if any(type(x) not in (int, float) or not isfinite(x) for x in (value, warning_threshold, critical_threshold)):
        raise ValueError("Nonfinite alert input")
    if not 0 <= warning_threshold < critical_threshold or value < 0:
        raise ValueError("Invalid alert thresholds")
    if type(maximum_age_seconds) is not int or not 30 <= maximum_age_seconds <= 86400:
        raise ValueError("Invalid alert freshness policy")
    if type(evidence_verified) is not bool:
        raise ValueError("Verified provider evidence is required")
    if not isinstance(observed_at, str):
        raise ValueError("Timestamp required")
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        current = datetime.now(timezone.utc) if now is None else now
        if not isinstance(current, datetime) or dt.tzinfo is None or current.tzinfo is None:
            raise ValueError("Time zone required")
        age = (current.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid alert timestamp") from exc
    if age < -60:
        raise ValueError("Future alert observation")
    freshness = "stale" if age > maximum_age_seconds else "current"
    trusted = evidence_verified and freshness == "current"
    severity = ("critical" if value >= critical_threshold else
                "warning" if value >= warning_threshold else None) if trusted else None
    return {"kind": kind, "severity": severity, "source_sha": source_sha,
            "observed_at": dt.astimezone(timezone.utc).isoformat(),
            "freshness": freshness, "evidence_verified": evidence_verified,
            "notification_sent": False, "evidence_required": True}
