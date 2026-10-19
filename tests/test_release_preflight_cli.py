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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
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
