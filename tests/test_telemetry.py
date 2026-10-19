import unittest
from datetime import datetime, timezone, timedelta
from foia_archive.telemetry import project_status

class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.base = dict(repository="neophilism/FOIA_Aggregator", source_sha="a"*40,
                         kind="crawl", state="success", observed_at=self.now.isoformat(),
                         now=self.now, evidence_verified=True)
    def test_fresh_status_carries_source(self):
        result = project_status(**self.base)
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["source_sha"], "a"*40)
        self.assertTrue(result["evidence_required"])
    def test_unverified_claims_fail_closed_even_when_fresh(self):
        for state in ("success", "running", "failure"):
            result = project_status(**{**self.base, "state": state, "evidence_verified": False})
            self.assertEqual(result["state"], "unknown")
            self.assertFalse(result["evidence_verified"])

    def test_stale_success_becomes_unknown(self):
        result = project_status(**{**self.base, "observed_at":(self.now-timedelta(days=2)).isoformat()})
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["freshness"], "stale")
    def test_future_and_non_source_data_rejected(self):
        for patch in ({"source_sha":"not-a-commit"},{"repository":"private key"},
                      {"observed_at":"yesterday"},{"observed_at":(self.now+timedelta(minutes=10)).isoformat()},
                      {"state":"CERTIFIED"},{"maximum_age_seconds":0},
                      {"now":"not-datetime"},{"evidence_verified":1}, {"kind":[]}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                project_status(**{**self.base, **patch})
