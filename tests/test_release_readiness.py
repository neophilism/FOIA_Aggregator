import unittest
from datetime import datetime, timezone, timedelta
from foia_archive.release_readiness import evaluate_release, REQUIRED

class ReleaseReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,10,18,tzinfo=timezone.utc)
    def record(self, **updates):
        x = dict(head_sha="a"*40, passed=True, observed_at=self.now.isoformat(),
                 evidence_url="https://github.com/example/repo/actions/runs/1")
        return {**x, **updates}
    def evaluate(self, evidence):
        return evaluate_release(evidence, now=self.now)
    def test_complete_evidence_is_only_ready_for_review(self):
        result = self.evaluate({name:self.record() for name in REQUIRED})
        self.assertTrue(result["ready_for_review"])
        self.assertFalse(result["production_deployed"])
    def test_missing_failing_and_mismatched_sha_fail_closed(self):
        good = {name:self.record() for name in REQUIRED}
        with self.assertRaises(ValueError): self.evaluate(dict(list(good.items())[1:]))
        self.assertFalse(self.evaluate({**good, "api_health":self.record(passed=False)})["ready_for_review"])
        self.assertFalse(self.evaluate({**good, "database_integrity":self.record(head_sha="b"*40)})["ready_for_review"])
    def test_invalid_evidence_rejected(self):
        good = {name:self.record() for name in REQUIRED}
        for url in ("http://untrusted/", "https://", "https://localhost/health",
                    "https://127.0.0.1/data", "https://user:pass@example.org/run",
                    "https://example.org:8080/results", "https://example.org/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.evaluate({**good, "api_health":self.record(evidence_url=url)})
    def test_stale_future_or_timezone_naive_observations_denied(self):
        good = {name:self.record() for name in REQUIRED}
        for observed in ((self.now-timedelta(days=2)).isoformat(),
                         (self.now+timedelta(hours=2)).isoformat(), "2026-10-18",
                         "not-a-timestamp"):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                self.evaluate({**good, "api_health":self.record(observed_at=observed)})
