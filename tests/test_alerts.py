import unittest
from foia_archive.alerts import evaluate_alert

class AlertTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(kind="crawl_failure", value=4, warning_threshold=2,
                         critical_threshold=5, observed_at="2026-10-18T12:00:00+00:00",
                         source_sha="a"*40)
    def test_warning_and_critical_cannot_send(self):
        warning = evaluate_alert(**self.base)
        critical = evaluate_alert(**{**self.base,"value":6})
        self.assertEqual(warning["severity"],"warning")
        self.assertEqual(critical["severity"],"critical")
        self.assertFalse(warning["notification_sent"])
    def test_malformed_metrics_fail_closed(self):
        for patch in ({"value":float("nan")},{"critical_threshold":1},
                      {"observed_at":"now"},{"source_sha":"x"},{"kind":"credential"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                evaluate_alert(**{**self.base,**patch})
