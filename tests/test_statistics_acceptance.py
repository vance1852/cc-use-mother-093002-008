import unittest

from digital_trade_statistics.acceptance import run


class StatisticsAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["queue_positions_stable"])
        self.assertTrue(result["release_lock_stable"])
        self.assertTrue(result["recompute_v1_match"])
        self.assertTrue(result["recompute_v2_match"])
        self.assertTrue(result["correction_versioned"])
        self.assertEqual(2, result["releases"])
        self.assertIn("IND_TOTAL", result["public_indicators"])


if __name__ == "__main__":
    unittest.main()
