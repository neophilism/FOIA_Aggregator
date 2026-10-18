import unittest
from datetime import datetime, timezone, timedelta
from foia_archive.telemetry import project_status

class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.base = dict(repository="neophilism/FOIA_Aggregator", source_sha="a"*40,
                         kind="crawl", state="success", observed_at=self.now.isoformat(),
                         now=self.now)
    def test_fresh_status_carries_source(self):
        result = project_status(**self.base)
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["source_sha"], "a"*40)
        self.assertTrue(result["evidence_required"])
    def test_stale_success_becomes_unknown(self):
        result = project_status(**{**self.base, "observed_at":(self.now-timedelta(days=2)).isoformat()})
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["freshness"], "stale")
    def test_future_and_non_source_data_rejected(self):
        for patch in ({"source_sha":"not-a-commit"},{"repository":"private key"},
                      {"observed_at":"yesterday"},{"observed_at":(self.now+timedelta(minutes=10)).isoformat()},
                      {"state":"CERTIFIED"},{"maximum_age_seconds":0}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                project_status(**{**self.base, **patch})
