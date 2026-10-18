"""Bounded offline scale policy. Never grants permission to crawl or spend."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ScaleBudget:
    source_limit: int
    max_documents_per_source: int
    max_document_bytes: int
    maximum_cycle_seconds: int
    def __post_init__(self):
        bounds = ((self.source_limit,1,10000),
                  (self.max_documents_per_source,1,10000),
                  (self.max_document_bytes,1024,1024*1024*1024),
                  (self.maximum_cycle_seconds,60,86400))
        for value,lower,upper in bounds:
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Unbounded scale budget")
    def project(self):
        return {"source_limit":self.source_limit,
                "max_documents_per_source":self.max_documents_per_source,
                "max_document_bytes":self.max_document_bytes,
                "maximum_cycle_seconds":self.maximum_cycle_seconds,
                "crawl_authorized":False,
                "live_spend_authorized":False}
