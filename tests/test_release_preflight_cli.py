import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from foia_archive.release_readiness import REQUIRED

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_preflight.py"

class ReleasePreflightCliTests(unittest.TestCase):
    def _run(self, payload):
        return self._run_raw(json.dumps(payload))
    def _run_raw(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(payload, encoding="utf-8")
            return subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                  cwd=ROOT, capture_output=True, text=True, timeout=10)
    def _evidence(self):
        observed_at = datetime.now(timezone.utc).isoformat()
        record = dict(head_sha="a"*40, passed=True, observed_at=observed_at,
                      evidence_url="https://github.com/example/repo/actions/runs/1")
        return {name:dict(record) for name in REQUIRED}
    def test_current_complete_checks_are_review_only(self):
        result = self._run(self._evidence())
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertTrue(status["ready_for_review"])
        self.assertFalse(status["production_deployed"])
    def test_failed_check_cannot_approve(self):
        evidence = self._evidence()
        evidence["storage_readback"]["passed"] = False
        result = self._run(evidence)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ready_for_review"])
    def test_malformed_evidence_does_not_echo_sensitive_content(self):
        result = self._run({"secret":"do-not-print"})
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("do-not-print", result.stderr)
    def test_duplicate_or_nonfinite_evidence_is_rejected_without_echo(self):
        valid = json.dumps(self._evidence())
        for raw in (valid[:-1] + ',"api_health":{"secret":"do-not-print"}}',
                    valid.replace('"passed": true', '"passed": true, "passed": false', 1),
                    valid.replace('"passed": true', '"passed": NaN', 1)):
            with self.subTest(raw=raw[:60]):
                result = self._run_raw(raw)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("do-not-print", result.stderr)

    def test_oversized_file_is_bounded_during_read(self):
        result = self._run_raw(" " * (1024 * 1024 + 1))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_deeply_nested_evidence_fails_cleanly(self):
        result = self._run_raw("[" * 1100 + "0" + "]" * 1100)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
