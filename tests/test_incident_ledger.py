import sqlite3
import tempfile
import unittest
from pathlib import Path
from foia_archive.alerts import evaluate_alert
from foia_archive.incident_ledger import list_incidents, record_incident

class IncidentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "incidents.db"
        self.conn = sqlite3.connect(self.db)
        self.repo = "neophilism/FOIA_Aggregator"
        self.base = dict(kind="crawl_failure", value=6, warning_threshold=2,
                         critical_threshold=5, observed_at="2026-10-18T12:00:00+00:00",
                         source_sha="a"*40, evidence_verified=True)
        from datetime import datetime, timezone
        self.base["now"] = datetime(2026,10,18,12,tzinfo=timezone.utc)
    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()
    def test_duplicate_does_not_generate_second_incident_and_survives_reopen(self):
        decision = evaluate_alert(**self.base)
        first = record_incident(self.conn, self.repo, decision)
        self.conn.commit()
        self.assertTrue(first["newly_recorded"])
        self.assertFalse(first["notification_sent"])
        self.conn.close()
        self.conn = sqlite3.connect(self.db)
        duplicate = record_incident(self.conn, self.repo, decision)
        self.assertEqual(first["incident_id"], duplicate["incident_id"])
        self.assertFalse(duplicate["newly_recorded"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0], 1)
    def test_only_current_verified_actionable_severity_is_recorded(self):
        for decision in (
            evaluate_alert(**{**self.base, "evidence_verified":False}),
            evaluate_alert(**{**self.base, "value":0}),
            evaluate_alert(**{**self.base, "observed_at":"2026-10-01T12:00:00+00:00"}),
        ):
            with self.assertRaises(ValueError):
                record_incident(self.conn, self.repo, decision)
    def test_malformed_incident_payloads_fail_closed(self):
        original = evaluate_alert(**self.base)
        with self.assertRaises(ValueError):
            record_incident(self.conn, self.repo, {**original, "severity": []})
        with self.assertRaises(ValueError):
            record_incident(self.conn, "../invalid", original)
        for repository in ("./invalid", "owner/..", "owner/."):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                record_incident(self.conn, repository, original)

    def test_distinct_source_revision_does_not_collide(self):
        first = record_incident(self.conn, self.repo, evaluate_alert(**self.base))
        second = record_incident(self.conn, self.repo, evaluate_alert(**{**self.base, "source_sha":"b"*40}))
        self.assertNotEqual(first["incident_id"], second["incident_id"])

    def test_bounded_read_only_view_of_verified_incidents(self):
        self.assertEqual(list_incidents(self.conn), [])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'alert_incidents'"
        ).fetchone())
        decision = evaluate_alert(**self.base)
        record_incident(self.conn, self.repo, decision)
        rows = list_incidents(self.conn, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repository"], self.repo)
        self.assertEqual(rows[0]["source_sha"], "a" * 40)
        self.assertEqual(rows[0]["severity"], "critical")
        for invalid in (0, 101, True, "1"):
            with self.subTest(limit=invalid), self.assertRaises(ValueError):
                list_incidents(self.conn, limit=invalid)
