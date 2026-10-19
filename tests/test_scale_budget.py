import unittest
from foia_archive.scale_budget import ScaleBudget

class ScaleBudgetTests(unittest.TestCase):
    def setUp(self):
        self.budget = ScaleBudget(50, 200, 10_000_000, 600)
        self.candidate = dict(sources_started=49, is_new_source=True,
                              documents_for_source=199, next_document_bytes=1024,
                              elapsed_seconds=599)
    def test_bounded_conservative_projection(self):
        result = self.budget.project()
        self.assertFalse(result["crawl_authorized"])
        self.assertFalse(result["live_spend_authorized"])
    def test_invalid_and_unsafe_budgets_rejected(self):
        for args in ((0,1,1024,60),(1,1,1024,10),(1,1,1024,True),
                     (1,1,2**50,60),(1,10001,1024,60)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ScaleBudget(*args)
    def test_next_candidate_within_limits_is_not_crawl_permission(self):
        decision = self.budget.assess_candidate(**self.candidate)
        self.assertTrue(decision["within_plan"])
        self.assertFalse(decision["crawl_authorized"])
        self.assertFalse(decision["live_spend_authorized"])
    def test_each_hard_limit_denies_candidate(self):
        modifications = ({"sources_started":50}, {"documents_for_source":200},
                         {"next_document_bytes":10_000_001}, {"elapsed_seconds":600},
                         {"next_document_bytes":0})
        for patch in modifications:
            with self.subTest(patch=patch):
                self.assertFalse(self.budget.assess_candidate(**{**self.candidate, **patch})["within_plan"])
    def test_malformed_counter_types_fail_closed(self):
        for patch in ({"is_new_source":1}, {"sources_started":True},
                      {"documents_for_source":-1}, {"elapsed_seconds":float("nan")},
                      {"next_document_bytes":1.5}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.budget.assess_candidate(**{**self.candidate, **patch})
