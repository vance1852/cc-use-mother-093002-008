import unittest

from stat_release.acceptance import run


class StatAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual([1, 2, 3, 4, 5, 6], result["queue_positions_after_restart"])
        self.assertEqual("publisher-1", result["lock_holder_after_restart"])
        self.assertEqual("2025-v1", result["first_version"])
        self.assertEqual("2025-v2", result["second_version"])
        self.assertTrue(result["v1_recompute_match"])
        self.assertTrue(result["v2_recompute_match"])
        self.assertEqual("370000000.000000", result["v1_dot_global"])
        self.assertEqual("320000000.000000", result["v1_public_dot"])
        self.assertEqual("559000000.000000", result["v2_tdt_global"])
        self.assertEqual("479000000.000000", result["v2_public_tdt"])
        self.assertEqual(["ECON-B"], result["v2_impact"]["affected_economies"])
        self.assertEqual([("2025-v1", "corrected"), ("2025-v2", "published")],
                         result["releases"])


if __name__ == "__main__":
    unittest.main()
