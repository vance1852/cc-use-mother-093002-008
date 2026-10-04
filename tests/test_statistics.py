import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from digital_trade_statistics.service import StatisticsService


CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))


class StatisticsTestBase(unittest.TestCase):
    def database_path(self):
        return ":memory:"

    def setUp(self):
        self.database = Database(self.database_path())
        self.domain = DomainService(self.database, CLOCK)
        self.stats = StatisticsService(self.database, CLOCK)
        self.seq = 0
        self.domain.register_organization(request_id="org-1", actor_id="bootstrap",
                                          organization_id="org-secretariat", name="数据秘书处")
        self.domain.register_organization(request_id="org-2", actor_id="bootstrap",
                                          organization_id="org-other", name="另一报送机构")
        self.domain.register_actor(request_id="act-admin", actor_id="bootstrap",
                                   new_actor_id="admin", display_name="管理员", role="admin",
                                   organization_id="org-secretariat")
        for actor_id, name, role, org in (
                ("submitter", "报送员", "submitter", "org-secretariat"),
                ("submitter-2", "另一机构报送员", "submitter", "org-other"),
                ("analyst", "分析员", "analyst", "org-secretariat"),
                ("reviewer", "复核员", "reviewer", "org-secretariat"),
                ("waiver", "豁免批准人", "exemption_approver", "org-secretariat"),
                ("signer-1", "会签人一", "release_approver", "org-secretariat"),
                ("signer-2", "会签人二", "release_approver", "org-secretariat"),
                ("publisher", "发布人", "release_approver", "org-secretariat"),
                ("auditor", "审计员", "auditor", "org-secretariat")):
            self.domain.register_actor(request_id=f"act-{actor_id}", actor_id="admin",
                                       new_actor_id=actor_id, display_name=name, role=role,
                                       organization_id=org)
        for code, name, group in (("CHN", "中国", "ASIA"), ("USA", "美国", "AMER"),
                                  ("DEU", "德国", "EURO")):
            self.stats.register_economy(request_id=f"eco-{code}", actor_id="admin",
                                        economy_code=code, name=name, group_code=group)
        self.stats.register_rule_version(
            request_id="rule-1", actor_id="admin", rule_version="2026.1",
            description="全球数字贸易统计规则",
            allowed_methodologies=["digital_ordered", "digitally_deliverable"],
            expected_economies=["CHN", "USA", "DEU"], base_currency="USD",
            coverage_threshold="0.6", consistency_tolerance="0.2", required_cosignatures=2)
        self.stats.register_fx_rate(request_id="fx-eur", actor_id="admin",
                                    rule_version="2026.1", currency="EUR",
                                    effective_from="2024", effective_to="2026",
                                    rate_to_base="1.1")
        self.stats.register_fx_rate(request_id="fx-cny", actor_id="admin",
                                    rule_version="2026.1", currency="CNY",
                                    effective_from="2024", effective_to="2026",
                                    rate_to_base="0.14")
        self.stats.register_class_mapping(request_id="map-1", actor_id="admin",
                                          rule_version="2026.1", source_category="DO_GOODS",
                                          target_category="IND_ORDERED",
                                          effective_from="2024", effective_to="2026")
        self.stats.register_class_mapping(request_id="map-2", actor_id="admin",
                                          rule_version="2026.1", source_category="DD_SERV",
                                          target_category="IND_DELIVERED",
                                          effective_from="2024", effective_to="2026")

    def tearDown(self):
        self.database.close()

    def req(self):
        self.seq += 1
        return f"req-{self.seq}"

    def submit(self, economy="CHN", period="2025", currency="CNY", ordered="500",
               delivered="300", actor="submitter", org="org-secretariat",
               methodology="digital_ordered", confidentiality="public", **extra):
        return self.stats.submit_dataset(
            request_id=self.req(), actor_id=actor, organization_id=org,
            economy_code=economy, period=period, methodology=methodology,
            currency=currency, confidentiality=confidentiality,
            source_evidence=f"{economy} 官方统计函 {period}", rule_version="2026.1",
            observations=[{"category": "DO_GOODS", "value": ordered},
                          {"category": "DD_SERV", "value": delivered}],
            **extra)

    def submit_and_process(self, **kwargs):
        receipt = self.submit(**kwargs)
        self.stats.process_validation_queue(actor_id="admin")
        return receipt.resource_id

    def findings_for(self, dataset_id):
        runs = self.stats.list_validation_runs(actor_id="auditor", dataset_id=dataset_id)
        items = []
        for run in runs:
            items.extend(self.stats.list_findings(actor_id="auditor", run_id=run["run_id"]))
        return items

    def make_candidate(self, period="2025", creator="analyst"):
        receipt = self.stats.create_candidate(request_id=self.req(), actor_id=creator,
                                              period=period, rule_version="2026.1",
                                              label="候选版本")
        return receipt.resource_id

    def publish(self, candidate_id, publisher="publisher"):
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-1",
                                    candidate_id=candidate_id)
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-2",
                                    candidate_id=candidate_id)
        self.stats.acquire_release_lock(actor_id=publisher, candidate_id=candidate_id)
        return self.stats.publish_release(request_id=self.req(), actor_id=publisher,
                                          candidate_id=candidate_id)


class SubmissionTest(StatisticsTestBase):
    def test_same_request_replays_original_receipt(self):
        payload = dict(economy_code="CHN", period="2025", methodology="digital_ordered",
                       currency="CNY", confidentiality="public",
                       source_evidence="来源证明", rule_version="2026.1",
                       observations=[{"category": "DO_GOODS", "value": "1"}])
        first = self.stats.submit_dataset(request_id="dup", actor_id="submitter",
                                          organization_id="org-secretariat", **payload)
        second = self.stats.submit_dataset(request_id="dup", actor_id="submitter",
                                           organization_id="org-secretariat", **payload)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        with self.assertRaises(ConflictError):
            self.stats.submit_dataset(request_id="dup", actor_id="submitter",
                                      organization_id="org-secretariat",
                                      **{**payload, "source_evidence": "另一证明"})

    def test_submitter_limited_to_own_organization(self):
        with self.assertRaises(PermissionDenied):
            self.submit(actor="submitter", org="org-other")
        receipt = self.submit(actor="submitter-2", org="org-other", economy="USA",
                              currency="USD", ordered="10", delivered="5")
        self.assertFalse(receipt.replayed)
        own = self.stats.list_datasets(actor_id="submitter-2")
        self.assertEqual(["org-other"], [item["organization_id"] for item in own])

    def test_auditor_cannot_submit(self):
        with self.assertRaises(PermissionDenied):
            self.submit(actor="auditor")

    def test_revision_requires_reason_and_latest_base(self):
        base = self.submit()
        with self.assertRaises(ValidationError):
            self.submit(revision_of=base.resource_id)
        with self.assertRaises(ValidationError):
            self.submit(economy="USA", revision_of=base.resource_id,
                        revision_reason="换经济体")
        with self.assertRaises(PermissionDenied):
            self.submit(actor="submitter-2", org="org-other",
                        revision_of=base.resource_id, revision_reason="越权修订")
        revised = self.submit(ordered="600", revision_of=base.resource_id,
                              revision_reason="更正出口数值")
        self.assertNotEqual(base.resource_id, revised.resource_id)
        with self.assertRaises(ConflictError):
            self.submit(revision_of=base.resource_id, revision_reason="重复修订")
        with self.assertRaises(ConflictError):
            self.submit()
        datasets = self.stats.list_datasets(actor_id="auditor", economy_code="CHN")
        self.assertEqual(2, len(datasets))
        reasons = {item["revision_reason"] for item in datasets}
        self.assertIn("更正出口数值", reasons)

    def test_unknown_references_rejected(self):
        with self.assertRaises(NotFoundError):
            self.submit(economy="XXX")
        with self.assertRaises(ValidationError):
            self.submit(period="2025-13")


class ValidationPipelineTest(StatisticsTestBase):
    def test_structure_currency_mapping_and_gap_findings(self):
        receipt = self.stats.submit_dataset(
            request_id=self.req(), actor_id="submitter", organization_id="org-secretariat",
            economy_code="CHN", period="2025", methodology="bogus_method",
            currency="JPY", confidentiality="secret", source_evidence="证明",
            rule_version="2026.1",
            observations=[{"category": "DO_GOODS", "value": "10"}])
        self.stats.process_validation_queue(actor_id="admin")
        codes = {(item["check_type"], item["code"], item["severity"])
                 for item in self.findings_for(receipt.resource_id)}
        self.assertIn(("structure", "methodology_not_allowed", "error"), codes)
        self.assertIn(("structure", "confidentiality_unknown", "error"), codes)
        self.assertIn(("currency", "fx_rate_missing", "error"), codes)
        self.assertIn(("gap", "indicator_gap", "warning"), codes)

    def test_mapping_missing_is_error(self):
        receipt = self.stats.submit_dataset(
            request_id=self.req(), actor_id="submitter", organization_id="org-secretariat",
            economy_code="CHN", period="2025", methodology="digital_ordered",
            currency="CNY", confidentiality="public", source_evidence="证明",
            rule_version="2026.1",
            observations=[{"category": "UNKNOWN_CAT", "value": "1"},
                          {"category": "DO_GOODS", "value": "10"},
                          {"category": "DD_SERV", "value": "5"}])
        self.stats.process_validation_queue(actor_id="admin")
        codes = {item["code"] for item in self.findings_for(receipt.resource_id)}
        self.assertIn("mapping_missing", codes)

    def test_currency_conversion_values(self):
        dataset_id = self.submit_and_process(economy="DEU", currency="EUR",
                                             ordered="100", delivered="50")
        rows = self.database.connection.execute(
            "SELECT indicator, value_base, gap FROM stat_normalized_observations "
            "WHERE dataset_id=? ORDER BY indicator", (dataset_id,)).fetchall()
        self.assertEqual([("IND_DELIVERED", "55.000000", 0),
                          ("IND_ORDERED", "110.000000", 0)],
                         [(row["indicator"], row["value_base"], row["gap"]) for row in rows])

    def test_cross_period_consistency_warning(self):
        self.submit_and_process(period="2024", ordered="500", delivered="300")
        dataset_id = self.submit_and_process(period="2025", ordered="800", delivered="300")
        findings = self.findings_for(dataset_id)
        breaks = [item for item in findings if item["code"] == "consistency_break"]
        self.assertEqual(1, len(breaks))
        self.assertEqual("warning", breaks[0]["severity"])
        self.assertEqual("IND_ORDERED", breaks[0]["context"]["indicator"])

    def test_queue_processes_in_position_order(self):
        first = self.submit(economy="CHN")
        second = self.submit(economy="USA", currency="USD", ordered="10", delivered="5")
        result = self.stats.process_validation_queue(actor_id="admin")
        runs = self.stats.list_validation_runs(actor_id="auditor")
        self.assertEqual([1, 2], [run["queue_position"] for run in runs])
        self.assertEqual(["completed", "completed"], [run["status"] for run in runs])
        self.assertEqual(2, len(result["processed"]))
        self.assertEqual(first.resource_id, runs[0]["dataset_id"])
        self.assertEqual(second.resource_id, runs[1]["dataset_id"])

    def test_submitter_cannot_drive_queue(self):
        with self.assertRaises(PermissionDenied):
            self.stats.process_validation_queue(actor_id="submitter")


class RestartRecoveryTest(StatisticsTestBase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "restart.sqlite3"
        super().setUp()

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def database_path(self):
        return str(self.path)

    def _reopen(self):
        self.database.close()
        self.database = Database(self.path)
        self.domain = DomainService(self.database, CLOCK)
        self.stats = StatisticsService(self.database, CLOCK)

    def test_queue_positions_survive_restart(self):
        self.submit(economy="CHN")
        self.submit(economy="USA", currency="USD", ordered="10", delivered="5")
        before = [(run["queue_position"], run["status"])
                  for run in self.stats.list_validation_runs(actor_id="auditor")]
        self.database.connection.execute(
            "UPDATE stat_validation_runs SET status='running' WHERE queue_position=1")
        self._reopen()
        after = [(run["queue_position"], run["status"])
                 for run in self.stats.list_validation_runs(actor_id="auditor")]
        self.assertEqual(before, after)
        self.assertEqual([(1, "queued"), (2, "queued")], after)
        result = self.stats.process_validation_queue(actor_id="admin")
        self.assertEqual(2, len(result["processed"]))

    def test_release_lock_survives_restart(self):
        self.submit_and_process(economy="CHN")
        self.submit_and_process(economy="USA", currency="USD", ordered="10", delivered="5")
        self.submit_and_process(economy="DEU", currency="EUR", ordered="10", delivered="5")
        candidate_id = self.make_candidate()
        self.stats.acquire_release_lock(actor_id="publisher", candidate_id=candidate_id)
        self._reopen()
        lock = self.stats.lock_state(actor_id="auditor")
        self.assertEqual("publisher", lock["holder"])
        self.assertEqual(candidate_id, lock["resource_id"])
        with self.assertRaises(ConflictError):
            self.stats.acquire_release_lock(actor_id="signer-1", candidate_id=candidate_id)
        with self.assertRaises(PermissionDenied):
            self.stats.release_release_lock(actor_id="signer-1", candidate_id=candidate_id)
        self.stats.release_release_lock(actor_id="publisher", candidate_id=candidate_id)
        self.assertIsNone(self.stats.lock_state(actor_id="auditor"))


class ReleaseFlowTest(StatisticsTestBase):
    def _three_economies(self):
        self.submit_and_process(economy="CHN", ordered="500", delivered="300")
        self.submit_and_process(economy="USA", currency="USD", ordered="100", delivered="80")
        self.submit_and_process(economy="DEU", currency="EUR", ordered="100", delivered="50")

    def test_coverage_threshold_blocks_publish(self):
        self.submit_and_process(economy="CHN")
        candidate_id = self.make_candidate()
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-1",
                                    candidate_id=candidate_id)
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-2",
                                    candidate_id=candidate_id)
        self.stats.acquire_release_lock(actor_id="publisher", candidate_id=candidate_id)
        with self.assertRaises(ConflictError) as ctx:
            self.stats.publish_release(request_id=self.req(), actor_id="publisher",
                                       candidate_id=candidate_id)
        self.assertIn("覆盖度", str(ctx.exception))
        report = self.stats.candidate_report(actor_id="auditor", candidate_id=candidate_id)
        self.assertFalse(report["coverage_met"])
        self.assertEqual(4, len(report["missing_cells"]))

    def test_cosignature_rules(self):
        self._three_economies()
        candidate_id = self.make_candidate(creator="admin")
        with self.assertRaises(PermissionDenied):
            self.stats.cosign_candidate(request_id=self.req(), actor_id="admin",
                                        candidate_id=candidate_id)
        with self.assertRaises(PermissionDenied):
            self.stats.cosign_candidate(request_id=self.req(), actor_id="analyst",
                                        candidate_id=candidate_id)
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-1",
                                    candidate_id=candidate_id)
        with self.assertRaises(ConflictError):
            self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-1",
                                        candidate_id=candidate_id)

    def test_exemption_approver_cannot_publish_or_cosign(self):
        self.submit_and_process(economy="CHN")
        self.submit_and_process(economy="DEU", currency="EUR", ordered="100", delivered="50")
        receipt = self.stats.submit_dataset(
            request_id=self.req(), actor_id="submitter", organization_id="org-secretariat",
            economy_code="USA", period="2025", methodology="digital_ordered",
            currency="USD", confidentiality="public", source_evidence="证明",
            rule_version="2026.1",
            observations=[{"category": "DO_GOODS", "value": "100"},
                          {"category": "UNKNOWN_CAT", "value": "1"}])
        self.stats.process_validation_queue(actor_id="admin")
        error_finding = next(item for item in self.findings_for(receipt.resource_id)
                             if item["severity"] == "error")
        # admin 兼具发布权限，由其批准豁免后不得再会签或发布同一版本
        self.stats.approve_exemption(request_id=self.req(), actor_id="admin",
                                     finding_id=error_finding["finding_id"],
                                     reason="分类停用，数值已并入其他指标")
        candidate_id = self.make_candidate()
        report = self.stats.candidate_report(actor_id="auditor", candidate_id=candidate_id)
        self.assertEqual(["admin"], report["exemption_approvers"])
        self.assertIn(receipt.resource_id, report["eligible_datasets"])
        with self.assertRaises(PermissionDenied):
            self.stats.cosign_candidate(request_id=self.req(), actor_id="admin",
                                        candidate_id=candidate_id)
        with self.assertRaises(PermissionDenied):
            self.stats.cosign_candidate(request_id=self.req(), actor_id="waiver",
                                        candidate_id=candidate_id)
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-1",
                                    candidate_id=candidate_id)
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-2",
                                    candidate_id=candidate_id)
        self.stats.acquire_release_lock(actor_id="admin", candidate_id=candidate_id)
        with self.assertRaises(ConflictError) as ctx:
            self.stats.publish_release(request_id=self.req(), actor_id="admin",
                                       candidate_id=candidate_id)
        self.assertIn("批准豁免的人不得发布同一版本", str(ctx.exception))
        self.stats.release_release_lock(actor_id="admin", candidate_id=candidate_id)
        self.stats.acquire_release_lock(actor_id="publisher", candidate_id=candidate_id)
        receipt = self.stats.publish_release(request_id=self.req(), actor_id="publisher",
                                             candidate_id=candidate_id)
        self.assertFalse(receipt.replayed)

    def test_publisher_must_hold_lock_and_be_independent(self):
        self._three_economies()
        candidate_id = self.make_candidate()
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-1",
                                    candidate_id=candidate_id)
        self.stats.cosign_candidate(request_id=self.req(), actor_id="signer-2",
                                    candidate_id=candidate_id)
        with self.assertRaises(ConflictError) as ctx:
            self.stats.publish_release(request_id=self.req(), actor_id="publisher",
                                       candidate_id=candidate_id)
        self.assertIn("发布锁", str(ctx.exception))
        self.stats.acquire_release_lock(actor_id="signer-1", candidate_id=candidate_id)
        with self.assertRaises(ConflictError):
            self.stats.publish_release(request_id=self.req(), actor_id="signer-1",
                                       candidate_id=candidate_id)
        self.stats.release_release_lock(actor_id="signer-1", candidate_id=candidate_id)

    def test_publish_freezes_metrics_and_recompute_matches(self):
        self._three_economies()
        candidate_id = self.make_candidate()
        release = self.publish(candidate_id)
        release_id = release.resource_id
        metrics = self.stats.release_metrics(actor_id="auditor", release_id=release_id)
        ordered = metrics["indicators"]["IND_ORDERED"]
        self.assertEqual("280.000000", ordered["global"]["value"])
        self.assertEqual("70.000000", ordered["groups"]["ASIA"]["value"])
        self.assertEqual("100.000000", ordered["groups"]["AMER"]["value"])
        self.assertEqual("110.000000", ordered["groups"]["EURO"]["value"])
        self.assertEqual("110.000000", ordered["contributions"]["DEU"]["value"])
        delivered = metrics["indicators"]["IND_DELIVERED"]
        self.assertEqual("177.000000", delivered["global"]["value"])
        recompute = self.stats.recompute_release(actor_id="auditor", release_id=release_id)
        self.assertTrue(recompute["match"], recompute["differences"])
        self.assertEqual(14, recompute["checked_metrics"])
        summary = recompute["indicators"]["IND_ORDERED"]
        self.assertEqual("280.000000", summary["global"]["value"])
        shares = {item["economy_code"]: item["share"]
                  for item in summary["contributions"]}
        self.assertEqual("0.250000", shares["CHN"])
        again = self.stats.recompute_release(actor_id="auditor", release_id=release_id,
                                             indicator="IND_ORDERED")
        self.assertEqual(summary, again["indicators"]["IND_ORDERED"])

    def test_recompute_detects_tampered_payload(self):
        self._three_economies()
        release_id = self.publish(self.make_candidate()).resource_id
        self.database.connection.execute(
            "UPDATE stat_datasets SET payload_json='{\"observations\": []}' "
            "WHERE economy_code='DEU'")
        recompute = self.stats.recompute_release(actor_id="auditor", release_id=release_id)
        self.assertFalse(recompute["match"])
        issues = {item["issue"] for item in recompute["differences"]}
        self.assertIn("payload_hash_mismatch", issues)

    def test_correction_creates_new_version_with_impact_list(self):
        self._three_economies()
        release_v1 = self.publish(self.make_candidate()).resource_id
        base_dataset = self.database.connection.execute(
            "SELECT dataset_id FROM stat_datasets WHERE economy_code='DEU'").fetchone()
        self.submit(economy="DEU", currency="EUR", ordered="105", delivered="50",
                    revision_of=base_dataset["dataset_id"],
                    revision_reason="DEU 秘书处更正")
        self.stats.process_validation_queue(actor_id="admin")
        correction = self.stats.create_correction(
            request_id=self.req(), actor_id="admin", release_id=release_v1,
            reason="DEU 报送更正", affected_economies=["DEU"],
            affected_indicators=["IND_ORDERED"],
            affected_charts=["chart-global-total"],
            affected_conclusions=["conclusion-growth"])
        candidate_id = self.stats.list_corrections(actor_id="auditor")[0]["candidate_id"]
        release_v2 = self.publish(candidate_id).resource_id
        releases = {item["release_id"]: item for item in
                    self.stats.list_releases(actor_id="auditor")}
        self.assertEqual("superseded", releases[release_v1]["status"])
        self.assertEqual("published", releases[release_v2]["status"])
        self.assertEqual(release_v1, releases[release_v2]["corrects_release_id"])
        self.assertEqual(2, releases[release_v2]["version_number"])
        record = self.stats.list_corrections(actor_id="auditor")[0]
        self.assertEqual(release_v2, record["new_release_id"])
        self.assertEqual(["DEU"], record["affected_economies"])
        self.assertEqual(["IND_ORDERED"], record["affected_indicators"])
        self.assertEqual(["chart-global-total"], record["affected_charts"])
        self.assertEqual(["conclusion-growth"], record["affected_conclusions"])
        metrics = self.stats.release_metrics(actor_id="auditor", release_id=release_v2)
        self.assertEqual("285.500000",
                         metrics["indicators"]["IND_ORDERED"]["global"]["value"])
        self.assertTrue(self.stats.recompute_release(actor_id="auditor",
                                                     release_id=release_v2)["match"])

    def test_correction_requires_impact_lists(self):
        self._three_economies()
        release_id = self.publish(self.make_candidate()).resource_id
        with self.assertRaises(ValidationError):
            self.stats.create_correction(
                request_id=self.req(), actor_id="admin", release_id=release_id,
                reason="缺图表", affected_economies=["DEU"],
                affected_indicators=["IND_ORDERED"], affected_charts=[],
                affected_conclusions=["c"])


class DerivedIndicatorTest(StatisticsTestBase):
    def test_analyst_scope_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.stats.create_derived_indicator(
                request_id=self.req(), actor_id="analyst", code="IND_TOTAL",
                name="合计", formula={"op": "sum", "inputs": ["IND_ORDERED", "IND_DELIVERED"]})
        self.stats.grant_analyst_scope(request_id=self.req(), actor_id="admin",
                                       analyst_id="analyst", scope_type="indicator",
                                       scope_value="IND_ORDERED")
        with self.assertRaises(PermissionDenied):
            self.stats.create_derived_indicator(
                request_id=self.req(), actor_id="analyst", code="IND_TOTAL",
                name="合计", formula={"op": "sum", "inputs": ["IND_ORDERED", "IND_DELIVERED"]})
        self.stats.grant_analyst_scope(request_id=self.req(), actor_id="admin",
                                       analyst_id="analyst", scope_type="indicator",
                                       scope_value="IND_DELIVERED")
        receipt = self.stats.create_derived_indicator(
            request_id=self.req(), actor_id="analyst", code="IND_TOTAL",
            name="合计", formula={"op": "sum", "inputs": ["IND_ORDERED", "IND_DELIVERED"]})
        self.assertFalse(receipt.replayed)

    def test_derived_indicator_in_release_metrics(self):
        self.stats.grant_analyst_scope(request_id=self.req(), actor_id="admin",
                                       analyst_id="analyst", scope_type="indicator",
                                       scope_value="IND_ORDERED")
        self.stats.grant_analyst_scope(request_id=self.req(), actor_id="admin",
                                       analyst_id="analyst", scope_type="indicator",
                                       scope_value="IND_DELIVERED")
        self.stats.create_derived_indicator(
            request_id=self.req(), actor_id="analyst", code="IND_TOTAL",
            name="合计", formula={"op": "sum", "inputs": ["IND_ORDERED", "IND_DELIVERED"]})
        self.submit_and_process(economy="CHN", ordered="500", delivered="300")
        self.submit_and_process(economy="USA", currency="USD", ordered="100", delivered="80")
        self.submit_and_process(economy="DEU", currency="EUR", ordered="100", delivered="50")
        release_id = self.publish(self.make_candidate()).resource_id
        metrics = self.stats.release_metrics(actor_id="auditor", release_id=release_id)
        self.assertEqual("457.000000", metrics["indicators"]["IND_TOTAL"]["global"]["value"])
        recompute = self.stats.recompute_release(actor_id="auditor", release_id=release_id)
        self.assertTrue(recompute["match"], recompute["differences"])
        self.assertIn("IND_TOTAL", recompute["indicators"])

    def test_only_admin_grants_scope(self):
        with self.assertRaises(PermissionDenied):
            self.stats.grant_analyst_scope(request_id=self.req(), actor_id="analyst",
                                           analyst_id="analyst", scope_type="indicator",
                                           scope_value="IND_ORDERED")


class ReviewAndPublicTest(StatisticsTestBase):
    def test_reviews_recorded_separately(self):
        dataset_id = self.submit_and_process(economy="CHN")
        receipt = self.stats.add_review(request_id=self.req(), actor_id="reviewer",
                                        target_type="dataset", target_id=dataset_id,
                                        opinion="approve", comment="来源核验通过")
        self.assertFalse(receipt.replayed)
        reviews = self.stats.list_reviews(actor_id="auditor", target_type="dataset",
                                          target_id=dataset_id)
        self.assertEqual(1, len(reviews))
        self.assertEqual("approve", reviews[0]["opinion"])
        with self.assertRaises(PermissionDenied):
            self.stats.add_review(request_id=self.req(), actor_id="submitter",
                                  target_type="dataset", target_id=dataset_id,
                                  opinion="approve", comment="越权复核")

    def test_public_interface_returns_only_published_public_results(self):
        with self.assertRaises(NotFoundError):
            self.stats.public_indicators()
        self.submit_and_process(economy="CHN", ordered="500", delivered="300")
        self.submit_and_process(economy="USA", currency="USD", ordered="100", delivered="80")
        self.submit_and_process(economy="DEU", currency="EUR", ordered="100", delivered="50",
                                confidentiality="confidential")
        release_id = self.publish(self.make_candidate()).resource_id
        public = self.stats.public_indicators()
        self.assertEqual({}, public["indicators"])
        self.assertEqual(["IND_DELIVERED", "IND_ORDERED"], public["restricted_indicators"])
        internal = self.stats.release_metrics(actor_id="auditor", release_id=release_id)
        self.assertEqual("280.000000", internal["indicators"]["IND_ORDERED"]["global"]["value"])
        releases = self.stats.public_releases()
        self.assertEqual(1, len(releases))
        self.assertEqual("published", releases[0]["status"])

    def test_public_interface_visible_when_all_public(self):
        self.submit_and_process(economy="CHN", ordered="500", delivered="300")
        self.submit_and_process(economy="USA", currency="USD", ordered="100", delivered="80")
        self.submit_and_process(economy="DEU", currency="EUR", ordered="100", delivered="50")
        self.publish(self.make_candidate())
        public = self.stats.public_indicators(indicator="IND_ORDERED")
        self.assertEqual("280.000000", public["indicators"]["IND_ORDERED"]["global"]["value"])
        contributions = {item["economy_code"] for item in
                         public["indicators"]["IND_ORDERED"]["contributions"]}
        self.assertEqual({"CHN", "USA", "DEU"}, contributions)
        self.assertEqual([], public["restricted_indicators"])


if __name__ == "__main__":
    unittest.main()
