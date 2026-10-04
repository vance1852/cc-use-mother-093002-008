import unittest
from decimal import Decimal

from stat_release.compute import compute_results, diff_results
from stat_release.rules import (RulesError, consistency_findings, coverage_ratio,
                                formula_scopes, gap_findings, normalize_records,
                                validate_definition, validate_formula)

RULES = validate_definition({
    "methodologies": ["digital_ordered", "digitally_delivered"],
    "indicators": [
        {"code": "DOT", "name": "数字订购贸易额", "methodology": "digital_ordered"},
        {"code": "DDS", "name": "可数字化交付服务额", "methodology": "digitally_delivered"},
    ],
    "fx_rates": [
        {"currency": "USD", "usd_per_unit": "1", "effective_from": "0000", "effective_to": "9999"},
        {"currency": "EUR", "usd_per_unit": "1.1", "effective_from": "2025", "effective_to": "2025"},
    ],
    "classification_map": [
        {"source": "DO-1", "target": "DOT", "effective_from": "0000", "effective_to": "9999"},
        {"source": "DD-1", "target": "DDS", "effective_from": "2025", "effective_to": "2025"},
    ],
    "required_economies": ["ECON-A", "ECON-B"],
    "economy_groups": [
        {"code": "ADV", "name": "发达经济体", "members": ["ECON-A", "ECON-B"]},
    ],
    "consistency_tolerance": "0.5",
    "coverage_threshold": "0.6",
    "required_countersigns": 2,
})


class DefinitionTest(unittest.TestCase):
    def test_rejects_duplicate_indicator(self):
        with self.assertRaises(RulesError):
            validate_definition({
                "methodologies": ["m"], "required_economies": ["E"],
                "indicators": [{"code": "A", "name": "甲", "methodology": "m"},
                               {"code": "A", "name": "乙", "methodology": "m"}],
                "fx_rates": [{"currency": "USD", "usd_per_unit": "1",
                              "effective_from": "0000", "effective_to": "9999"}]})

    def test_rejects_missing_usd_anchor(self):
        with self.assertRaises(RulesError):
            validate_definition({
                "methodologies": ["m"], "required_economies": ["E"],
                "indicators": [{"code": "A", "name": "甲", "methodology": "m"}],
                "fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1",
                              "effective_from": "0000", "effective_to": "9999"}]})

    def test_rejects_unknown_group_member(self):
        with self.assertRaises(RulesError):
            validate_definition({
                "methodologies": ["m"], "required_economies": ["E"],
                "indicators": [{"code": "A", "name": "甲", "methodology": "m"}],
                "fx_rates": [{"currency": "USD", "usd_per_unit": "1",
                              "effective_from": "0000", "effective_to": "9999"}],
                "economy_groups": [{"code": "G", "name": "组", "members": ["E", "X"]}]})

    def test_rejects_inverted_effective_period(self):
        with self.assertRaises(RulesError):
            validate_definition({
                "methodologies": ["m"], "required_economies": ["E"],
                "indicators": [{"code": "A", "name": "甲", "methodology": "m"}],
                "fx_rates": [{"currency": "USD", "usd_per_unit": "1",
                              "effective_from": "2026", "effective_to": "2025"}]})


class NormalizeTest(unittest.TestCase):
    def test_converts_currency_and_unit(self):
        normalized, findings = normalize_records(
            [{"indicator": "DO-1", "value": "200"}], RULES,
            period="2025", currency="EUR", unit="million")
        self.assertEqual([], findings)
        self.assertEqual([{"indicator": "DOT", "source_indicator": "DO-1",
                           "value": "220000000.000000"}], normalized)

    def test_mapping_respects_effective_period(self):
        normalized, findings = normalize_records(
            [{"indicator": "DD-1", "value": "1"}], RULES,
            period="2024", currency="USD", unit="million")
        self.assertEqual([], normalized)
        self.assertEqual("classification", findings[0]["check_type"])

    def test_flags_duplicate_and_bad_value(self):
        _, findings = normalize_records(
            [{"indicator": "DOT", "value": "1"}, {"indicator": "DO-1", "value": "2"},
             {"indicator": "DOT", "value": True}], RULES,
            period="2025", currency="USD", unit="million")
        kinds = {(item["check_type"], item["severity"]) for item in findings}
        self.assertIn(("structure", "error"), kinds)
        self.assertEqual(2, len(findings))

    def test_missing_fx_is_single_error(self):
        normalized, findings = normalize_records(
            [{"indicator": "DOT", "value": "1"}], RULES,
            period="2024", currency="EUR", unit="million")
        self.assertEqual([], normalized)
        self.assertEqual([("fx", "error")],
                         [(item["check_type"], item["severity"]) for item in findings])

    def test_exempted_error_is_suppressed(self):
        _, findings = normalize_records(
            [{"indicator": "XX-9", "value": "1"}], RULES,
            period="2025", currency="USD", unit="million")
        self.assertEqual(1, len(findings))
        _, suppressed = normalize_records(
            [{"indicator": "XX-9", "value": "1"}], RULES,
            period="2025", currency="USD", unit="million",
            exempted={("classification", "record[0]")})
        self.assertEqual([], suppressed)

    def test_gap_and_consistency_findings(self):
        gaps = gap_findings(RULES, economy="ECON-A", methodology="digital_ordered",
                            present_indicators=[])
        self.assertEqual(["ECON-A:DOT"], [item["subject"] for item in gaps])
        warnings = consistency_findings(
            RULES, economy="ECON-A",
            normalized=[{"indicator": "DOT", "value": "200.000000"}],
            base_values={"DOT": "100.000000"})
        self.assertEqual(1, len(warnings))
        quiet = consistency_findings(
            RULES, economy="ECON-A",
            normalized=[{"indicator": "DOT", "value": "140.000000"}],
            base_values={"DOT": "100.000000"})
        self.assertEqual([], quiet)

    def test_coverage_ratio(self):
        self.assertEqual(Decimal("0.500000"), coverage_ratio(RULES, ["ECON-A"]))
        self.assertEqual(Decimal("1.000000"), coverage_ratio(RULES, ["ECON-A", "ECON-B"]))


class FormulaTest(unittest.TestCase):
    def test_validate_and_scopes(self):
        formula = validate_formula({"op": "sum", "terms": ["DOT", "DDS"]}, RULES)
        self.assertEqual(["digital_ordered", "digitally_delivered"],
                         formula_scopes(formula, RULES))
        with self.assertRaises(RulesError):
            validate_formula({"op": "sum", "terms": ["NOPE"]}, RULES)
        with self.assertRaises(RulesError):
            validate_formula({"op": "ratio", "numerator": "DOT", "denominator": "DOT"}, RULES)


DATASETS = [
    {"dataset_id": "ds-1", "economy": "ECON-A", "currency": "USD", "unit": "million",
     "records": [{"indicator": "DO-1", "value": "100"}, {"indicator": "DD-1", "value": "60"}]},
    {"dataset_id": "ds-2", "economy": "ECON-B", "currency": "EUR", "unit": "million",
     "records": [{"indicator": "DO-1", "value": "200"}, {"indicator": "DD-1", "value": "80"}]},
]
DERIVED = [{"code": "TDT", "name": "数字贸易总额",
            "formula": {"op": "sum", "terms": ["DOT", "DDS"]}}]


class ComputeTest(unittest.TestCase):
    def test_totals_groups_contributions_and_derived(self):
        results = compute_results(period="2025", definition=RULES,
                                  datasets=DATASETS, derived=DERIVED)
        dot = results["indicators"]["DOT"]
        self.assertEqual("320000000.000000", dot["global_total"])
        self.assertEqual("320000000.000000", dot["groups"]["ADV"])
        self.assertEqual("0.687500", dot["contributions"]["economies"]["ECON-B"])
        tdt = results["indicators"]["TDT"]
        self.assertEqual("468000000.000000", tdt["global_total"])
        self.assertEqual("1.000000", results["coverage"])
        self.assertEqual([["ECON-A", "100000000.000000"], ["ECON-B", "220000000.000000"]],
                         results["charts"]["DOT"])
        keys = [item["key"] for item in results["conclusions"]]
        self.assertEqual(["global:DDS", "global:DOT", "global:TDT"], keys)

    def test_growth_uses_base_release(self):
        base_dataset = {"dataset_id": "ds-0", "economy": "ECON-A", "currency": "USD",
                        "unit": "million", "records": [{"indicator": "DO-1", "value": "100"}]}
        base = compute_results(period="2024", definition=RULES,
                               datasets=[base_dataset], derived=[])
        current = compute_results(period="2025", definition=RULES,
                                  datasets=DATASETS, derived=[], base_results=base)
        # DOT: 320M vs 100M → 2.2 倍增长
        self.assertEqual("2.200000", current["indicators"]["DOT"]["growth"])
        keys = [item["key"] for item in current["conclusions"]]
        self.assertIn("growth:DOT", keys)

    def test_deterministic_across_runs(self):
        first = compute_results(period="2025", definition=RULES,
                                datasets=DATASETS, derived=DERIVED)
        second = compute_results(period="2025", definition=RULES,
                                 datasets=list(reversed(DATASETS)), derived=DERIVED)
        self.assertEqual(first, second)

    def test_diff_lists_affected_items(self):
        old = compute_results(period="2025", definition=RULES,
                              datasets=DATASETS, derived=DERIVED)
        changed = [dict(DATASETS[0]), dict(DATASETS[1])]
        changed[1] = {**changed[1],
                      "records": [{"indicator": "DO-1", "value": "210"},
                                  {"indicator": "DD-1", "value": "80"}]}
        new = compute_results(period="2025", definition=RULES,
                              datasets=changed, derived=DERIVED)
        impact = diff_results(old, new)
        self.assertEqual(["ECON-B"], impact["affected_economies"])
        self.assertEqual(["DOT", "TDT"], impact["affected_indicators"])
        self.assertEqual(["DOT", "TDT"], impact["affected_charts"])
        self.assertEqual(["global:DOT", "global:TDT"], impact["affected_conclusions"])


if __name__ == "__main__":
    unittest.main()
