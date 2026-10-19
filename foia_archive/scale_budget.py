"""Bounded offline scale policy. Never grants permission to crawl or spend."""
from dataclasses import dataclass
from math import isfinite

@dataclass(frozen=True)
class ScaleBudget:
    source_limit: int
    max_documents_per_source: int
    max_document_bytes: int
    maximum_cycle_seconds: int

    def __post_init__(self):
        bounds = ((self.source_limit, 1, 10000),
                  (self.max_documents_per_source, 1, 10000),
                  (self.max_document_bytes, 1024, 1024 * 1024 * 1024),
                  (self.maximum_cycle_seconds, 60, 86400))
        for value, lower, upper in bounds:
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Unbounded scale budget")

    def project(self):
        return {"source_limit": self.source_limit,
                "max_documents_per_source": self.max_documents_per_source,
                "max_document_bytes": self.max_document_bytes,
                "maximum_cycle_seconds": self.maximum_cycle_seconds,
                "crawl_authorized": False, "live_spend_authorized": False}

    def assess_candidate(self, *, sources_started, is_new_source,
                         documents_for_source, next_document_bytes,
                         elapsed_seconds):
        """Offline policy check. Separate B2 quotas, writer lease and authority still apply."""
        counters = (sources_started, documents_for_source, next_document_bytes)
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("Invalid candidate counters")
        if type(is_new_source) is not bool:
            raise ValueError("New-source flag must be boolean")
        if (type(elapsed_seconds) not in (int, float)
                or not isfinite(elapsed_seconds) or elapsed_seconds < 0):
            raise ValueError("Invalid elapsed seconds")
        reasons = []
        if (is_new_source and sources_started >= self.source_limit
                or sources_started > self.source_limit):
            reasons.append("source_limit")
        if documents_for_source >= self.max_documents_per_source:
            reasons.append("source_document_limit")
        if not 0 < next_document_bytes <= self.max_document_bytes:
            reasons.append("document_size_limit")
        if elapsed_seconds >= self.maximum_cycle_seconds:
            reasons.append("cycle_timeout")
        return {"within_plan": not reasons, "reasons": reasons,
                "crawl_authorized": False, "live_spend_authorized": False}

    def assess_storage_headroom(self, *, archive_bytes, versioned_b2_bytes,
                                proposed_archive_bytes, proposed_versioned_bytes):
        """Budget-only forecast; trusted live B2 usage must be checked separately."""
        counters = (archive_bytes, versioned_b2_bytes,
                    proposed_archive_bytes, proposed_versioned_bytes)
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("Invalid storage usage measurement")
        archive_ceiling = 8_500_000_000
        versioned_ceiling = 9_000_000_000
        reasons = []
        if archive_bytes + proposed_archive_bytes > archive_ceiling:
            reasons.append("archive_budget")
        if versioned_b2_bytes + proposed_versioned_bytes > versioned_ceiling:
            reasons.append("versioned_storage_budget")
        return {"within_plan": not reasons, "reasons": reasons,
                "archive_ceiling_bytes": archive_ceiling,
                "versioned_ceiling_bytes": versioned_ceiling,
                "upload_authorized": False, "live_spend_authorized": False}
