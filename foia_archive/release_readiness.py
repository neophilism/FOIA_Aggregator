"""Fail-closed evidence gate for public release; never triggers deployment."""
import ipaddress
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED = ("api_health", "database_integrity", "source_provenance", "storage_readback",
            "privacy_review", "accessibility")

def _review_link(value):
    if type(value) is not str or len(value) > 2048 or "@" in value:
        raise ValueError("Untrusted evidence link")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Untrusted evidence link")
        if parsed.port not in (None, 443) or not parsed.path.strip("/"):
            raise ValueError("Untrusted evidence link")
        hostname = parsed.hostname.lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("Localhost is not provider evidence")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not literal.is_global:
                raise ValueError("Private address is not provider evidence")
    except ValueError as exc:
        raise ValueError("Untrusted evidence link") from exc

def evaluate_release(evidence, *, now=None, maximum_age_seconds=3600):
    if type(evidence) is not dict or set(evidence) != set(REQUIRED):
        raise ValueError("Missing or unknown release checks")
    if type(maximum_age_seconds) is not int or not 30 <= maximum_age_seconds <= 86400:
        raise ValueError("Invalid freshness policy")
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("Current time must be timezone-qualified")
    details = {}
    for name, record in evidence.items():
        if type(record) is not dict or set(record) != {"head_sha", "passed", "observed_at", "evidence_url"}:
            raise ValueError("Invalid evidence record")
        sha = record["head_sha"]
        if type(sha) is not str or not SHA.fullmatch(sha) or type(record["passed"]) is not bool:
            raise ValueError("Unverified release check")
        raw_date = record["observed_at"]
        if type(raw_date) is not str:
            raise ValueError("Missing timestamp")
        try:
            observed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("Timestamp missing timezone")
            age = (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Invalid observation timestamp") from exc
        if age < -60 or age > maximum_age_seconds:
            raise ValueError("Stale or future release check")
        _review_link(record["evidence_url"])
        details[name] = record["passed"]
    heads = {record["head_sha"] for record in evidence.values()}
    accepted = all(details.values()) and len(heads) == 1
    return {"ready_for_review": accepted, "production_deployed": False,
            "head_sha": next(iter(heads)) if len(heads) == 1 else None,
            "failed_checks": sorted(name for name, passed in details.items() if not passed)}
