import unittest
from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from stat_release.api import route
from stat_release.service import StatService
from test_stat_service import RULES, bootstrap

CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
EVIDENCE = {"reference": "年度统计年报", "hash": "sha256:demo"}


class StatApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain, self.service = bootstrap(self.database)

    def tearDown(self):
        self.database.close()

    def test_health(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/stat/missing", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_rules_version_requires_admin(self):
        status, payload = route(self.service, "POST", "/stat/rules-versions",
                                {"request_id": "rv-x", "rules_version": "RV-X",
                                 "definition": RULES}, {"X-Actor-Id": "analyst-1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_submit_and_validate_via_api(self):
        status, payload = route(self.service, "POST", "/stat/datasets", {
            "request_id": "api-1", "economy": "ECON-A", "period": "2025",
            "methodology": "digital_ordered", "unit": "million", "currency": "USD",
            "confidentiality": "public", "source_evidence": EVIDENCE,
            "records": [{"indicator": "DO-1", "value": "100"}]},
            {"X-Actor-Id": "sub-a"})
        self.assertEqual(201, status)
        dataset_id = payload["resource_id"]
        replay_status, _ = route(self.service, "POST", "/stat/datasets", {
            "request_id": "api-1", "economy": "ECON-A", "period": "2025",
            "methodology": "digital_ordered", "unit": "million", "currency": "USD",
            "confidentiality": "public", "source_evidence": EVIDENCE,
            "records": [{"indicator": "DO-1", "value": "100"}]},
            {"X-Actor-Id": "sub-a"})
        self.assertEqual(200, replay_status)
        status, payload = route(self.service, "POST", "/stat/validations",
                                {"request_id": "api-2", "dataset_id": dataset_id,
                                 "rules_version": "RV-2026"}, {"X-Actor-Id": "analyst-1"})
        self.assertEqual(201, status)
        self.assertTrue(payload["resource_id"])
        status, payload = route(self.service, "POST", "/stat/validations/process-next",
                                {"request_id": "api-3"}, {"X-Actor-Id": "analyst-1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET",
                                f"/stat/validations?dataset_id={dataset_id}", None,
                                {"X-Actor-Id": "analyst-1"})
        self.assertEqual("completed", payload["items"][0]["status"])

    def test_submitter_scope_enforced_via_api(self):
        route(self.service, "POST", "/stat/datasets", {
            "request_id": "api-4", "economy": "ECON-B", "period": "2025",
            "methodology": "digital_ordered", "unit": "million", "currency": "USD",
            "confidentiality": "public", "source_evidence": EVIDENCE,
            "records": [{"indicator": "DO-1", "value": "1"}]},
            {"X-Actor-Id": "sub-b"})
        dataset_id = self.service.list_datasets(actor_id="admin")[0]["dataset_id"]
        status, payload = route(self.service, "GET", f"/stat/datasets/{dataset_id}", None,
                                {"X-Actor-Id": "sub-a"})
        self.assertEqual(403, status)
        status, payload = route(self.service, "GET", "/stat/datasets", None,
                                {"X-Actor-Id": "sub-b"})
        self.assertEqual(1, len(payload["items"]))

    def test_public_endpoint_needs_no_actor_and_hides_restricted(self):
        # 通过服务层快速发布一个版本
        self.service.submit_dataset(
            request_id="api-5", actor_id="sub-a", economy="ECON-A", period="2025",
            methodology="digital_ordered", unit="million", currency="USD",
            confidentiality="public", source_evidence=EVIDENCE,
            records=[{"indicator": "DO-1", "value": "100"}])
        self.service.submit_dataset(
            request_id="api-6", actor_id="sub-b", economy="ECON-B", period="2025",
            methodology="digital_ordered", unit="million", currency="USD",
            confidentiality="restricted", source_evidence=EVIDENCE,
            records=[{"indicator": "DO-1", "value": "50"}])
        for index, dataset in enumerate(self.service.list_datasets(actor_id="admin")):
            self.service.enqueue_validation(request_id=f"api-enq-{index}", actor_id="analyst-1",
                                            dataset_id=dataset["dataset_id"],
                                            rules_version="RV-2026")
            self.service.process_next_validation(request_id=f"api-proc-{index}",
                                                 actor_id="analyst-1")
        candidate = self.service.assemble_candidate(
            request_id="api-cand", actor_id="analyst-1",
            report_period="2025", rules_version="RV-2026").resource_id
        self.service.countersign_candidate(request_id="api-s1", actor_id="reviewer-1",
                                           candidate_id=candidate)
        self.service.countersign_candidate(request_id="api-s2", actor_id="reviewer-2",
                                           candidate_id=candidate)
        self.service.acquire_lock(request_id="api-lock", actor_id="publisher-1")
        self.service.publish_candidate(request_id="api-pub", actor_id="publisher-1",
                                       candidate_id=candidate)
        status, payload = route(self.service, "GET", "/public/stat/releases?report_period=2025",
                                None)
        self.assertEqual(200, status)
        self.assertEqual("100000000.000000",
                         payload["results"]["indicators"]["DOT"]["global_total"])
        self.assertEqual(["ECON-A"], payload["results"]["covered_economies"])
        # 内部接口需要操作者身份
        status, payload = route(self.service, "GET",
                                "/stat/releases/2025-v1/recompute?indicator_code=DOT", None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])
        status, payload = route(
            self.service, "GET",
            "/stat/releases/2025-v1/recompute?indicator_code=DOT", None,
            {"X-Actor-Id": "auditor-1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["match"])

    def test_invalid_body_returns_400(self):
        status, payload = route(self.service, "POST", "/stat/datasets",
                                {"request_id": "api-7"}, {"X-Actor-Id": "sub-a"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


if __name__ == "__main__":
    unittest.main()
