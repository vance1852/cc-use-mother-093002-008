import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from stat_release.service import RELEASE_LOCK, StatService

CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))

RULES = {
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
        {"source": "DD-1", "target": "DDS", "effective_from": "0000", "effective_to": "9999"},
    ],
    "required_economies": ["ECON-A", "ECON-B", "ECON-C"],
    "economy_groups": [
        {"code": "ADV", "name": "发达经济体", "members": ["ECON-A", "ECON-B"]},
        {"code": "EMG", "name": "新兴经济体", "members": ["ECON-C"]},
    ],
    "consistency_tolerance": "0.5",
    "coverage_threshold": "0.6",
    "required_countersigns": 2,
}

EVIDENCE = {"reference": "年度统计年报", "hash": "sha256:demo"}


def bootstrap(database):
    """在指定数据库上登记全套机构、操作者与规则版本。"""

    domain = DomainService(database, CLOCK)
    service = StatService(database, CLOCK)
    domain.register_organization(request_id="org-secret", actor_id="bootstrap",
                                 organization_id="org-secret", name="数据秘书处")
    domain.register_actor(request_id="actor-admin", actor_id="bootstrap",
                          new_actor_id="admin", display_name="管理员",
                          role="admin", organization_id="org-secret")
    for org_id in ["org-a", "org-b", "org-c", "org-analysis", "org-review-1",
                   "org-review-2", "org-approver", "org-publisher", "org-audit"]:
        domain.register_organization(request_id=f"org-{org_id}", actor_id="admin",
                                     organization_id=org_id, name=f"机构{org_id}")
    for actor_id, role, org_id in [
            ("sub-a", "submitter", "org-a"), ("sub-b", "submitter", "org-b"),
            ("sub-c", "submitter", "org-c"),
            ("analyst-1", "analyst", "org-analysis"),
            ("reviewer-0", "reviewer", "org-analysis"),
            ("reviewer-1", "reviewer", "org-review-1"),
            ("reviewer-2", "reviewer", "org-review-2"),
            ("reviewer-3", "reviewer", "org-review-1"),
            ("approver-1", "approver", "org-approver"),
            ("approver-a", "approver", "org-a"),
            ("publisher-1", "publisher", "org-publisher"),
            ("auditor-1", "auditor", "org-audit")]:
        domain.register_actor(request_id=f"actor-{actor_id}", actor_id="admin",
                              new_actor_id=actor_id, display_name=actor_id,
                              role=role, organization_id=org_id)
    service.register_rules_version(request_id="rules", actor_id="admin",
                                   rules_version="RV-2026", definition=RULES)
    return domain, service


class StatServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain, self.service = bootstrap(self.database)

    def tearDown(self):
        self.database.close()

    # ---- 测试辅助 ----

    def submit(self, request_id, submitter, economy, methodology, records,
               currency="USD", confidentiality="public", supersedes=None, reason=None,
               period="2025"):
        receipt = self.service.submit_dataset(
            request_id=request_id, actor_id=submitter, economy=economy, period=period,
            methodology=methodology, unit="million", currency=currency,
            confidentiality=confidentiality, source_evidence=EVIDENCE, records=records,
            supersedes=supersedes, revision_reason=reason)
        return receipt.resource_id

    def validate(self, request_id, dataset_id):
        self.service.enqueue_validation(request_id=f"{request_id}-enq", actor_id="analyst-1",
                                        dataset_id=dataset_id, rules_version="RV-2026")
        return self.service.process_next_validation(request_id=f"{request_id}-proc",
                                                    actor_id="analyst-1")

    def submit_valid(self, request_id, submitter, economy, methodology, records, **kwargs):
        dataset_id = self.submit(request_id, submitter, economy, methodology, records, **kwargs)
        self.validate(request_id, dataset_id)
        return dataset_id

    def publish_full_set(self, prefix="flow"):
        """报送三个经济体全部数据、完成校验、会签并发布，返回版本号。"""

        self.submit_valid(f"{prefix}-a-dot", "sub-a", "ECON-A", "digital_ordered",
                          [{"indicator": "DO-1", "value": "100"}])
        self.submit_valid(f"{prefix}-a-dds", "sub-a", "ECON-A", "digitally_delivered",
                          [{"indicator": "DD-1", "value": "60"}])
        self.submit_valid(f"{prefix}-b-dot", "sub-b", "ECON-B", "digital_ordered",
                          [{"indicator": "DO-1", "value": "200"}], currency="EUR")
        self.submit_valid(f"{prefix}-b-dds", "sub-b", "ECON-B", "digitally_delivered",
                          [{"indicator": "DD-1", "value": "80"}], currency="EUR")
        self.submit_valid(f"{prefix}-c-dot", "sub-c", "ECON-C", "digital_ordered",
                          [{"indicator": "DO-1", "value": "50"}], confidentiality="restricted")
        self.submit_valid(f"{prefix}-c-dds", "sub-c", "ECON-C", "digitally_delivered",
                          [{"indicator": "DD-1", "value": "30"}], confidentiality="restricted")
        candidate = self.service.assemble_candidate(
            request_id=f"{prefix}-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026")
        self.service.countersign_candidate(request_id=f"{prefix}-sign-1", actor_id="reviewer-1",
                                           candidate_id=candidate.resource_id)
        self.service.countersign_candidate(request_id=f"{prefix}-sign-2", actor_id="reviewer-2",
                                           candidate_id=candidate.resource_id)
        self.service.acquire_lock(request_id=f"{prefix}-lock", actor_id="publisher-1")
        receipt = self.service.publish_candidate(
            request_id=f"{prefix}-pub", actor_id="publisher-1",
            candidate_id=candidate.resource_id)
        return receipt.resource_id

    # ---- 报送与修订 ----

    def test_submitter_forced_to_own_organization(self):
        dataset_id = self.submit("s1", "sub-a", "ECON-A", "digital_ordered",
                                 [{"indicator": "DO-1", "value": "1"}])
        dataset = self.service.get_dataset(actor_id="admin", dataset_id=dataset_id)
        self.assertEqual("org-a", dataset["organization_id"])
        with self.assertRaises(PermissionDenied):
            self.service.submit_dataset(
                request_id="s2", actor_id="sub-a", organization_id="org-b",
                economy="ECON-A", period="2025", methodology="digital_ordered",
                unit="million", currency="USD", confidentiality="public",
                source_evidence=EVIDENCE, records=[{"indicator": "DO-1", "value": "1"}])

    def test_submitter_cannot_read_other_organization_dataset(self):
        dataset_id = self.submit("s3", "sub-b", "ECON-B", "digital_ordered",
                                 [{"indicator": "DO-1", "value": "1"}])
        with self.assertRaises(PermissionDenied):
            self.service.get_dataset(actor_id="sub-a", dataset_id=dataset_id)
        own = self.service.list_datasets(actor_id="sub-a")
        self.assertEqual([], own)

    def test_raw_submissions_preserved_and_revision_replaces(self):
        first = self.submit("s4", "sub-a", "ECON-A", "digital_ordered",
                            [{"indicator": "DO-1", "value": "1"}])
        second = self.submit("s5", "sub-a", "ECON-A", "digital_ordered",
                             [{"indicator": "DO-1", "value": "2"}],
                             supersedes=first, reason="补报更正")
        datasets = {item["dataset_id"]: item for item in self.service.list_datasets(actor_id="admin")}
        self.assertEqual("superseded", datasets[first]["status"])
        self.assertEqual("received", datasets[second]["status"])
        self.assertEqual(first, datasets[second]["supersedes"])
        self.assertEqual("补报更正", datasets[second]["revision_reason"])

    def test_revision_requires_reason_and_same_organization(self):
        first = self.submit("s6", "sub-a", "ECON-A", "digital_ordered",
                            [{"indicator": "DO-1", "value": "1"}])
        with self.assertRaises(ValidationError):
            self.submit("s7", "sub-a", "ECON-A", "digital_ordered",
                        [{"indicator": "DO-1", "value": "2"}], supersedes=first)
        with self.assertRaises(PermissionDenied):
            self.submit("s8", "sub-b", "ECON-A", "digital_ordered",
                        [{"indicator": "DO-1", "value": "2"}],
                        supersedes=first, reason="越权替代")
        self.submit("s9", "sub-a", "ECON-A", "digital_ordered",
                    [{"indicator": "DO-1", "value": "2"}], supersedes=first, reason="更正")
        with self.assertRaises(ConflictError):
            self.submit("s10", "sub-a", "ECON-A", "digital_ordered",
                        [{"indicator": "DO-1", "value": "3"}],
                        supersedes=first, reason="替代已失效报送")

    def test_submit_replay_is_idempotent(self):
        first = self.service.submit_dataset(
            request_id="s12", actor_id="sub-a", economy="ECON-A", period="2025",
            methodology="digital_ordered", unit="million", currency="USD",
            confidentiality="public", source_evidence=EVIDENCE,
            records=[{"indicator": "DO-1", "value": "1"}])
        replay = self.service.submit_dataset(
            request_id="s12", actor_id="sub-a", economy="ECON-A", period="2025",
            methodology="digital_ordered", unit="million", currency="USD",
            confidentiality="public", source_evidence=EVIDENCE,
            records=[{"indicator": "DO-1", "value": "1"}])
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)

    # ---- 校验 ----

    def test_validation_marks_structure_fx_classification_and_gaps(self):
        bad = self.submit("v1", "sub-a", "ECON-A", "digital_ordered",
                          [{"indicator": "DO-1", "value": "5"},
                           {"indicator": "XX-9", "value": "1"},
                           {"indicator": "DOT"}], currency="EUR", period="2024")
        self.validate("v1", bad)
        findings = self.service.list_findings(actor_id="auditor-1", dataset_id=bad)
        kinds = {(item["check_type"], item["severity"]) for item in findings}
        self.assertIn(("classification", "error"), kinds)
        self.assertIn(("structure", "error"), kinds)
        self.assertIn(("fx", "error"), kinds)  # EUR 汇率只对 2025 生效
        self.assertIn(("gap", "warning"), kinds)

    def test_validation_flags_cross_period_inconsistency(self):
        self.publish_full_set("base")
        revised = self.submit("v2", "sub-a", "ECON-A", "digital_ordered",
                              [{"indicator": "DO-1", "value": "400"}],
                              supersedes=None, reason=None)
        self.validate("v2", revised)
        findings = self.service.list_findings(actor_id="auditor-1", dataset_id=revised)
        consistency = [item for item in findings if item["check_type"] == "consistency"]
        self.assertEqual(1, len(consistency))
        self.assertEqual("warning", consistency[0]["severity"])
        self.assertEqual("ECON-A:DOT", consistency[0]["subject"])

    def test_queue_processes_in_submission_order(self):
        first = self.submit("v3", "sub-a", "ECON-A", "digital_ordered",
                            [{"indicator": "DO-1", "value": "1"}])
        second = self.submit("v4", "sub-b", "ECON-B", "digital_ordered",
                             [{"indicator": "DO-1", "value": "2"}])
        self.service.enqueue_validation(request_id="v3-enq", actor_id="analyst-1",
                                        dataset_id=first, rules_version="RV-2026")
        self.service.enqueue_validation(request_id="v4-enq", actor_id="analyst-1",
                                        dataset_id=second, rules_version="RV-2026")
        runs = self.service.list_validation_runs(actor_id="analyst-1")
        self.assertEqual([1, 2], [run["queue_position"] for run in runs])
        self.service.process_next_validation(request_id="v5-proc", actor_id="analyst-1")
        self.assertEqual("completed", self.service.list_validation_runs(
            actor_id="analyst-1", dataset_id=first)[0]["status"])
        self.assertEqual("queued", self.service.list_validation_runs(
            actor_id="analyst-1", dataset_id=second)[0]["status"])

    # ---- 豁免与复核 ----

    def test_exemption_requires_independent_approver(self):
        dataset_id = self.submit("e1", "sub-a", "ECON-A", "digital_ordered",
                                 [{"indicator": "XX-9", "value": "1"}])
        self.validate("e1", dataset_id)
        findings = self.service.list_findings(actor_id="auditor-1", dataset_id=dataset_id,
                                              status="open")
        finding = [item for item in findings if item["severity"] == "error"][0]
        self.assertEqual("classification", finding["check_type"])
        with self.assertRaises(PermissionDenied):
            self.service.approve_exemption(request_id="e1-x", actor_id="approver-a",
                                           finding_id=finding["finding_id"], reason="本机构豁免")
        receipt = self.service.approve_exemption(request_id="e1-y", actor_id="approver-1",
                                                 finding_id=finding["finding_id"],
                                                 reason="旧版附注项")
        self.assertFalse(receipt.replayed)
        updated = [item for item in self.service.list_findings(actor_id="auditor-1",
                                                               dataset_id=dataset_id)
                   if item["finding_id"] == finding["finding_id"]][0]
        self.assertEqual("exempted", updated["status"])
        self.assertEqual("approver-1", updated["exemption"]["approved_by"])
        with self.assertRaises(ConflictError):
            self.service.approve_exemption(request_id="e1-z", actor_id="approver-1",
                                           finding_id=finding["finding_id"], reason="重复豁免")
        # 豁免后组卷不再被未处理错误阻断
        candidate = self.service.assemble_candidate(
            request_id="e1-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026")
        self.assertFalse(candidate.replayed)

    def test_review_opinions_recorded_separately(self):
        dataset_id = self.submit("r1", "sub-a", "ECON-A", "digital_ordered",
                                 [{"indicator": "DO-1", "value": "1"}])
        self.service.record_review_opinion(request_id="r1-op", actor_id="reviewer-1",
                                           target_type="dataset", target_id=dataset_id,
                                           conclusion="approve", content="来源证明齐全")
        opinions = self.service.list_review_opinions(actor_id="auditor-1",
                                                     target_type="dataset", target_id=dataset_id)
        self.assertEqual(1, len(opinions))
        self.assertEqual("approve", opinions[0]["conclusion"])
        with self.assertRaises(PermissionDenied):
            self.service.record_review_opinion(request_id="r1-op2", actor_id="analyst-1",
                                               target_type="dataset", target_id=dataset_id,
                                               conclusion="comment", content="越权复核")

    # ---- 派生指标 ----

    def test_derived_indicator_requires_scope_grant(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_derived_indicator(
                request_id="d1", actor_id="analyst-1", code="TDT", name="数字贸易总额",
                formula={"op": "sum", "terms": ["DOT", "DDS"]})
        self.service.grant_analyst_scope(request_id="d2", actor_id="admin",
                                         organization_id="org-analysis", scope="digital_ordered")
        with self.assertRaises(PermissionDenied):
            self.service.create_derived_indicator(
                request_id="d3", actor_id="analyst-1", code="TDT", name="数字贸易总额",
                formula={"op": "sum", "terms": ["DOT", "DDS"]})
        self.service.grant_analyst_scope(request_id="d4", actor_id="admin",
                                         organization_id="org-analysis",
                                         scope="digitally_delivered")
        receipt = self.service.create_derived_indicator(
            request_id="d5", actor_id="analyst-1", code="TDT", name="数字贸易总额",
            formula={"op": "sum", "terms": ["DOT", "DDS"]})
        self.assertFalse(receipt.replayed)
        with self.assertRaises(ValidationError):
            self.service.create_derived_indicator(
                request_id="d6", actor_id="analyst-1", code="BAD", name="未知指标",
                formula={"op": "sum", "terms": ["NOPE"]})

    # ---- 组卷、会签与发布 ----

    def test_candidate_blocked_by_open_errors(self):
        self.submit_valid("c1", "sub-a", "ECON-A", "digital_ordered",
                          [{"indicator": "DO-1", "value": "100"}, {"indicator": "XX-9", "value": "1"}])
        with self.assertRaises(ConflictError):
            self.service.assemble_candidate(request_id="c1-cand", actor_id="analyst-1",
                                            report_period="2025", rules_version="RV-2026")

    def test_countersign_requires_independent_organizations(self):
        self.submit_valid("c2", "sub-a", "ECON-A", "digital_ordered",
                          [{"indicator": "DO-1", "value": "100"}])
        candidate = self.service.assemble_candidate(
            request_id="c2-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026").resource_id
        with self.assertRaises(PermissionDenied):
            # 与编制方同机构的复核员不具备独立性
            self.service.countersign_candidate(request_id="c2-s0", actor_id="reviewer-0",
                                               candidate_id=candidate)
        self.service.countersign_candidate(request_id="c2-s1", actor_id="reviewer-1",
                                           candidate_id=candidate)
        self.assertEqual("ready", self.service.get_candidate(actor_id="admin",
                                                             candidate_id=candidate)["status"])
        with self.assertRaises(ConflictError):
            self.service.countersign_candidate(request_id="c2-s2", actor_id="reviewer-1",
                                               candidate_id=candidate)
        # 同一机构的第二名复核员不能满足“两个独立机构”要求
        self.service.countersign_candidate(request_id="c2-s3", actor_id="reviewer-3",
                                           candidate_id=candidate)
        self.assertEqual("ready", self.service.get_candidate(actor_id="admin",
                                                             candidate_id=candidate)["status"])
        self.service.countersign_candidate(request_id="c2-s4", actor_id="reviewer-2",
                                           candidate_id=candidate)
        self.assertEqual("countersigned", self.service.get_candidate(
            actor_id="admin", candidate_id=candidate)["status"])

    def test_publish_requires_lock_countersign_and_coverage(self):
        self.submit_valid("p1", "sub-a", "ECON-A", "digital_ordered",
                          [{"indicator": "DO-1", "value": "100"}])
        candidate = self.service.assemble_candidate(
            request_id="p1-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026").resource_id
        self.service.acquire_lock(request_id="p1-lock", actor_id="publisher-1")
        with self.assertRaises(ConflictError):  # 未会签
            self.service.publish_candidate(request_id="p1-pub", actor_id="publisher-1",
                                           candidate_id=candidate)
        self.service.countersign_candidate(request_id="p1-s1", actor_id="reviewer-1",
                                           candidate_id=candidate)
        self.service.countersign_candidate(request_id="p1-s2", actor_id="reviewer-2",
                                           candidate_id=candidate)
        with self.assertRaises(ConflictError):  # 覆盖度 1/3 低于 0.6
            self.service.publish_candidate(request_id="p1-pub2", actor_id="publisher-1",
                                           candidate_id=candidate)

    def test_publish_requires_holding_lock(self):
        self.submit_valid("p2", "sub-a", "ECON-A", "digital_ordered",
                          [{"indicator": "DO-1", "value": "100"}])
        self.submit_valid("p3", "sub-b", "ECON-B", "digital_ordered",
                          [{"indicator": "DO-1", "value": "200"}], currency="EUR")
        candidate = self.service.assemble_candidate(
            request_id="p2-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026").resource_id
        self.service.countersign_candidate(request_id="p2-s1", actor_id="reviewer-1",
                                           candidate_id=candidate)
        self.service.countersign_candidate(request_id="p2-s2", actor_id="reviewer-2",
                                           candidate_id=candidate)
        with self.assertRaises(PermissionDenied):
            self.service.publish_candidate(request_id="p2-pub", actor_id="publisher-1",
                                           candidate_id=candidate)

    def test_approver_role_cannot_publish(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_candidate(request_id="p4-pub", actor_id="approver-1",
                                           candidate_id="cd-any")

    def test_correction_creates_new_version_with_impact(self):
        v1 = self.publish_full_set("corr")
        # 已公开的报告期不能绕过更正流程再次首发
        candidate = self.service.assemble_candidate(
            request_id="corr-cand-x", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026").resource_id
        self.service.countersign_candidate(request_id="corr-sx1", actor_id="reviewer-1",
                                           candidate_id=candidate)
        self.service.countersign_candidate(request_id="corr-sx2", actor_id="reviewer-2",
                                           candidate_id=candidate)
        with self.assertRaises(ConflictError):
            self.service.publish_candidate(request_id="corr-pub-x", actor_id="publisher-1",
                                           candidate_id=candidate)
        # 修订并发布更正版本
        old = [item for item in self.service.list_datasets(actor_id="admin")
               if item["economy"] == "ECON-B" and item["methodology"] == "digital_ordered"
               and item["status"] == "received"][0]
        revised = self.submit("corr-b3", "sub-b", "ECON-B", "digital_ordered",
                              [{"indicator": "DO-1", "value": "210"}], currency="EUR",
                              supersedes=old["dataset_id"], reason="口径对齐后调增")
        self.validate("corr-b3", revised)
        candidate = self.service.assemble_candidate(
            request_id="corr2-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026").resource_id
        self.service.countersign_candidate(request_id="corr2-s1", actor_id="reviewer-1",
                                           candidate_id=candidate)
        self.service.countersign_candidate(request_id="corr2-s2", actor_id="reviewer-2",
                                           candidate_id=candidate)
        receipt = self.service.publish_candidate(
            request_id="corr2-pub", actor_id="publisher-1", candidate_id=candidate,
            correction_of=v1, impact_note="ECON-B 修订")
        v2 = receipt.resource_id
        self.assertEqual("2025-v2", v2)
        release = self.service.get_release(actor_id="auditor-1", version_id=v2)
        self.assertEqual(v1, release["correction_of"])
        self.assertEqual(["ECON-B"], release["impact"]["affected_economies"])
        self.assertEqual(["DOT"], release["impact"]["affected_indicators"])
        self.assertEqual(["DOT"], release["impact"]["affected_charts"])
        self.assertEqual(["global:DOT"], release["impact"]["affected_conclusions"])
        releases = {row["version_id"]: row["status"]
                    for row in self.service.list_releases(actor_id="auditor-1")}
        self.assertEqual("corrected", releases[v1])
        self.assertEqual("published", releases[v2])
        self.assertEqual(v2, self.service.get_public_release("2025")["version_id"])

    # ---- 重算与公共接口 ----

    def test_recompute_matches_published_results(self):
        v1 = self.publish_full_set("rec")
        result = self.service.recompute_indicator(actor_id="auditor-1",
                                                  version_id=v1, indicator_code="DOT")
        self.assertTrue(result["match"])
        self.assertEqual("370000000.000000", result["detail"]["global_total"])
        self.assertEqual("320000000.000000", result["detail"]["groups"]["ADV"])
        self.assertEqual("50000000.000000", result["detail"]["groups"]["EMG"])
        self.assertEqual("0.594595", result["detail"]["contributions"]["economies"]["ECON-B"])
        with self.assertRaises(NotFoundError):
            self.service.recompute_indicator(actor_id="auditor-1", version_id=v1,
                                             indicator_code="NOPE")
        with self.assertRaises(PermissionDenied):
            self.service.recompute_indicator(actor_id="analyst-1", version_id=v1,
                                             indicator_code="DOT")

    def test_public_release_excludes_restricted_datasets(self):
        self.publish_full_set("pub")
        public = self.service.get_public_release("2025")
        self.assertEqual("320000000.000000",
                         public["results"]["indicators"]["DOT"]["global_total"])
        self.assertEqual(["ECON-A", "ECON-B"], public["results"]["covered_economies"])
        internal = self.service.get_release(actor_id="auditor-1", version_id="2025-v1")
        self.assertEqual("370000000.000000",
                         internal["internal_results"]["indicators"]["DOT"]["global_total"])
        limited = self.service.get_release(actor_id="analyst-1", version_id="2025-v1")
        self.assertNotIn("internal_results", limited)

    def test_derived_indicator_included_in_results(self):
        self.service.grant_analyst_scope(request_id="di-g1", actor_id="admin",
                                         organization_id="org-analysis", scope="digital_ordered")
        self.service.grant_analyst_scope(request_id="di-g2", actor_id="admin",
                                         organization_id="org-analysis",
                                         scope="digitally_delivered")
        self.service.create_derived_indicator(
            request_id="di-tdt", actor_id="analyst-1", code="TDT", name="数字贸易总额",
            formula={"op": "sum", "terms": ["DOT", "DDS"]})
        v1 = self.publish_full_set("di")
        result = self.service.recompute_indicator(actor_id="auditor-1",
                                                  version_id=v1, indicator_code="TDT")
        self.assertTrue(result["match"])
        self.assertEqual("548000000.000000", result["detail"]["global_total"])

    # ---- 重启持久性 ----

    def test_queue_and_lock_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stat.sqlite3"
            database = Database(path)
            domain, service = bootstrap(database)
            first = service.submit_dataset(
                request_id="rs-1", actor_id="sub-a", economy="ECON-A", period="2025",
                methodology="digital_ordered", unit="million", currency="USD",
                confidentiality="public", source_evidence=EVIDENCE,
                records=[{"indicator": "DO-1", "value": "1"}]).resource_id
            second = service.submit_dataset(
                request_id="rs-2", actor_id="sub-b", economy="ECON_B", period="2025",
                methodology="digital_ordered", unit="million", currency="USD",
                confidentiality="public", source_evidence=EVIDENCE,
                records=[{"indicator": "DO-1", "value": "2"}]).resource_id
            service.enqueue_validation(request_id="rs-enq-1", actor_id="analyst-1",
                                       dataset_id=first, rules_version="RV-2026")
            service.enqueue_validation(request_id="rs-enq-2", actor_id="analyst-1",
                                       dataset_id=second, rules_version="RV-2026")
            service.acquire_lock(request_id="rs-lock", actor_id="publisher-1")
            before = [run["queue_position"]
                      for run in service.list_validation_runs(actor_id="analyst-1")]
            database.close()

            reopened = Database(path)
            domain2 = DomainService(reopened, CLOCK)
            service2 = StatService(reopened, CLOCK)
            after = [run["queue_position"]
                     for run in service2.list_validation_runs(actor_id="analyst-1")]
            self.assertEqual(before, after)
            self.assertEqual([1, 2], after)
            self.assertEqual("publisher-1",
                             service2.get_lock(actor_id="admin", lock_name=RELEASE_LOCK)["holder"])
            processed = service2.process_next_validation(request_id="rs-proc",
                                                         actor_id="analyst-1")
            self.assertTrue(processed.resource_id)
            runs = service2.list_validation_runs(actor_id="analyst-1")
            self.assertEqual("completed", runs[0]["status"])
            self.assertEqual("queued", runs[1]["status"])
            valid, _ = domain2.verify_audit()
            self.assertTrue(valid)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
