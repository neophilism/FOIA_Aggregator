import unittest
from foia_archive.release_readiness import evaluate_release, REQUIRED
class ReleaseReadinessTests(unittest.TestCase):
 def record(self,**updates):
  x=dict(head_sha="a"*40,passed=True,observed_at="2026-10-18T00:00:00Z",
         evidence_url="https://github.com/example/repo/actions/runs/1")
  return {**x,**updates}
 def test_complete_evidence_is_only_ready_for_review(self):
  result=evaluate_release({name:self.record() for name in REQUIRED})
  self.assertTrue(result["ready_for_review"])
  self.assertFalse(result["production_deployed"])
 def test_missing_failing_and_mismatched_sha_fail_closed(self):
  good={name:self.record() for name in REQUIRED}
  with self.assertRaises(ValueError): evaluate_release(dict(list(good.items())[1:]))
  self.assertFalse(evaluate_release({**good,"api_health":self.record(passed=False)})["ready_for_review"])
  self.assertFalse(evaluate_release({**good,"database_integrity":self.record(head_sha="b"*40)})["ready_for_review"])
 def test_invalid_evidence_rejected(self):
  good={name:self.record() for name in REQUIRED}
  with self.assertRaises(ValueError): evaluate_release({**good,"api_health":self.record(evidence_url="http://untrusted/")})
