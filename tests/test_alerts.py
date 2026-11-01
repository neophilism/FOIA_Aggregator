import unittest
from datetime import datetime, timezone, timedelta
from foia_archive.alerts import evaluate_alert

class AlertTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.base = dict(kind="crawl_failure", value=4, warning_threshold=2,
                         critical_threshold=5, observed_at=self.now.isoformat(),
                         source_sha="a"*40, now=self.now, evidence_verified=True)
    def test_warning_and_critical_cannot_send(self):
        warning = evaluate_alert(**self.base)
        critical = evaluate_alert(**{**self.base,"value":6})
        self.assertEqual(warning["severity"],"warning")
        self.assertEqual(critical["severity"],"critical")
        self.assertFalse(warning["notification_sent"])
    def test_unverified_and_stale_values_never_trigger_alert(self):
        unverified = evaluate_alert(**{**self.base, "evidence_verified":False})
        stale = evaluate_alert(**{**self.base, "observed_at":(self.now-timedelta(days=2)).isoformat()})
        self.assertIsNone(unverified["severity"])
        self.assertIsNone(stale["severity"])
        self.assertEqual(stale["freshness"], "stale")
        self.assertTrue(stale["evidence_required"])
    def test_malformed_metrics_fail_closed(self):
        for patch in ({"value":float("nan")},{"critical_threshold":1},
                      {"observed_at":"now"},{"source_sha":"x"},{"kind":"credential"},
                      {"kind":[]},{"now":"not datetime"},{"evidence_verified":1},
                      {"maximum_age_seconds":0},
                      {"observed_at":(self.now+timedelta(minutes=4)).isoformat()}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                evaluate_alert(**{**self.base,**patch})
