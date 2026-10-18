"""Source-bound non-delivering alert decisions; no outbound notification or secrets."""
from datetime import datetime, timezone
from math import isfinite

KINDS = frozenset(("crawl_failure", "source_stale", "backup_failure", "quota_warning"))
LEVELS = frozenset(("warning", "critical"))

def evaluate_alert(*, kind, value, warning_threshold, critical_threshold,
                   observed_at, source_sha):
    if kind not in KINDS or not isinstance(source_sha, str) or len(source_sha) != 40 or not all(ch in "0123456789abcdef" for ch in source_sha):
        raise ValueError("Invalid alert source")
    if any(type(x) not in (int, float) or not isfinite(x) for x in (value, warning_threshold, critical_threshold)):
        raise ValueError("Nonfinite alert input")
    if not 0 <= warning_threshold < critical_threshold or value < 0:
        raise ValueError("Invalid alert thresholds")
    if not isinstance(observed_at, str):
        raise ValueError("Timestamp required")
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Unqualified timestamp")
    except ValueError as exc:
        raise ValueError("Invalid alert timestamp") from exc
    severity = "critical" if value >= critical_threshold else "warning" if value >= warning_threshold else None
    return {"kind":kind,"severity":severity,"source_sha":source_sha,
            "observed_at":dt.astimezone(timezone.utc).isoformat(),
            "notification_sent":False,"evidence_required":True}
