import unittest
from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from digital_trade_statistics.api import route
from digital_trade_statistics.service import StatisticsService


CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
ADMIN = {"X-Actor-Id": "admin"}
SUBMITTER = {"X-Actor-Id": "submitter"}
AUDITOR = {"X-Actor-Id": "auditor"}


class StatisticsApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain = DomainService(self.database, CLOCK)
        self.stats = StatisticsService(self.database, CLOCK)
        self.seq = 0
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="org-1", name="数据秘书处")
        self.domain.register_actor(request_id="act-admin", actor_id="bootstrap",
                                   new_actor_id="admin", display_name="管理员", role="admin",
                                   organization_id="org-1")
        self.domain.register_actor(request_id="act-sub", actor_id="admin",
                                   new_actor_id="submitter", display_name="报送员",
                                   role="submitter", organization_id="org-1")
        self.domain.register_actor(request_id="act-aud", actor_id="admin",
                                   new_actor_id="auditor", display_name="审计员",
                                   role="auditor", organization_id="org-1")
        self.stats.register_economy(request_id="eco-chn", actor_id="admin",
                                    economy_code="CHN", name="中国", group_code="ASIA")
        self.stats.register_rule_version(
            request_id="rule", actor_id="admin", rule_version="2026.1",
            description="规则", allowed_methodologies=["digital_ordered"],
            expected_economies=["CHN"], base_currency="USD",
            coverage_threshold="0.5", consistency_tolerance="0.2", required_cosignatures=1)
        self.stats.register_class_mapping(request_id="map", actor_id="admin",
                                          rule_version="2026.1", source_category="DO_GOODS",
                                          target_category="IND_ORDERED",
                                          effective_from="2024", effective_to="2026")

    def tearDown(self):
        self.database.close()

    def req(self):
        self.seq += 1
        return f"api-{self.seq}"

    def call(self, method, path, body=None, headers=None):
        return route(self.domain, self.stats, method, path, body, headers or {})

    def test_health_still_available(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_submit_dataset_over_http_and_query(self):
        status, payload = self.call("POST", "/statistics/datasets", {
            "request_id": self.req(), "organization_id": "org-1", "economy_code": "CHN",
            "period": "2025", "methodology": "digital_ordered", "currency": "USD",
            "confidentiality": "public", "source_evidence": "证明",
            "rule_version": "2026.1",
            "observations": [{"category": "DO_GOODS", "value": "10"}]}, SUBMITTER)
        self.assertEqual(201, status)
        self.assertEqual("dataset", payload["resource_type"])
        status, payload = self.call("GET", "/statistics/datasets?economy_code=CHN",
                                    headers=AUDITOR)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        status, payload = self.call("POST", "/statistics/validation/process", {}, ADMIN)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["processed"]))

    def test_submitter_cannot_register_rule_version(self):
        status, payload = self.call("POST", "/statistics/rule-versions", {
            "request_id": self.req(), "rule_version": "2099.1", "description": "越权",
            "allowed_methodologies": ["digital_ordered"], "expected_economies": ["CHN"],
            "base_currency": "USD", "coverage_threshold": "0.5",
            "consistency_tolerance": "0.2", "required_cosignatures": 1}, SUBMITTER)
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_public_endpoints_need_no_actor(self):
        status, payload = self.call("GET", "/public/releases")
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])
        status, payload = self.call("GET", "/public/indicators")
        self.assertEqual(404, status)

    def test_internal_metrics_require_actor(self):
        status, payload = self.call("GET", "/statistics/releases")
        self.assertEqual(404, status)  # 匿名操作者不存在
        self.assertEqual("not_found", payload["error"])

    def test_unknown_statistics_route_falls_through_to_404(self):
        status, payload = self.call("GET", "/statistics/unknown", headers=ADMIN)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = self.call("POST", "/statistics/datasets",
                                    {"request_id": "x"}, SUBMITTER)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


if __name__ == "__main__":
    unittest.main()
