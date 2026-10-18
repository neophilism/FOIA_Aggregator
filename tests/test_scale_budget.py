import unittest
from foia_archive.scale_budget import ScaleBudget
class ScaleBudgetTests(unittest.TestCase):
    def test_bounded_conservative_projection(self):
        result=ScaleBudget(50,200,10_000_000,600).project()
        self.assertFalse(result["crawl_authorized"])
        self.assertFalse(result["live_spend_authorized"])
    def test_invalid_and_unsafe_budgets_rejected(self):
        for args in ((0,1,1024,60),(1,1,1024,10),(1,1,1024,True),
                     (1,1,2**50,60),(1,10001,1024,60)):
            with self.subTest(args=args),self.assertRaises(ValueError):
                ScaleBudget(*args)
