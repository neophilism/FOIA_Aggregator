"""Fail-closed evidence gate for public release; never triggers deployment."""
import re
SHA=re.compile(r"^[0-9a-f]{40}$")
REQUIRED=("api_health","database_integrity","source_provenance","storage_readback","privacy_review","accessibility")
def evaluate_release(evidence):
    if type(evidence) is not dict or set(evidence)!=set(REQUIRED):
        raise ValueError("Missing or unknown release checks")
    details={}
    for name,record in evidence.items():
        if type(record) is not dict or set(record)!={"head_sha","passed","observed_at","evidence_url"}:
            raise ValueError("Invalid evidence record")
        sha=record["head_sha"]
        if type(sha) is not str or not SHA.fullmatch(sha) or type(record["passed"]) is not bool:
            raise ValueError("Unverified release check")
        if type(record["observed_at"]) is not str or not record["observed_at"]:
            raise ValueError("Missing timestamp")
        url=record["evidence_url"]
        if type(url) is not str or not url.startswith("https://") or "@" in url:
            raise ValueError("Untrusted evidence link")
        details[name]=record["passed"]
    heads={record["head_sha"] for record in evidence.values()}
    accepted=all(details.values()) and len(heads)==1
    return {"ready_for_review":accepted,"production_deployed":False,
            "head_sha":next(iter(heads)) if len(heads)==1 else None,
            "failed_checks":sorted(name for name,passed in details.items() if not passed)}
