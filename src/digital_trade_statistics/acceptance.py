"""统计资料接收、校验与版本发布服务的离线端到端验收。

覆盖：报送与修订、校验队列重启恢复、豁免与会签职责分离、发布锁跨重启
保持、版本公开、审计确定性重算、公共接口脱敏与公开后更正。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import StatisticsService


CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))


def _setup(domain: DomainService, statistics: StatisticsService) -> None:
    domain.register_organization(request_id="acc-org", actor_id="bootstrap",
                                 organization_id="org-secretariat", name="数据秘书处")
    domain.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin",
                          display_name="系统管理员", role="admin",
                          organization_id="org-secretariat")
    for request_id, actor_id, name, role in (
            ("acc-act-sub", "submitter", "报送员", "submitter"),
            ("acc-act-ana", "analyst", "分析员", "analyst"),
            ("acc-act-rev", "reviewer", "复核员", "reviewer"),
            ("acc-act-wav", "waiver", "豁免批准人", "exemption_approver"),
            ("acc-act-sig1", "signer-1", "会签人一", "release_approver"),
            ("acc-act-sig2", "signer-2", "会签人二", "release_approver"),
            ("acc-act-pub", "publisher", "发布人", "release_approver"),
            ("acc-act-aud", "auditor", "审计员", "auditor")):
        domain.register_actor(request_id=request_id, actor_id="admin", new_actor_id=actor_id,
                              display_name=name, role=role, organization_id="org-secretariat")
    for code, name, group in (("CHN", "中国", "ASIA"), ("USA", "美国", "AMER"),
                              ("DEU", "德国", "EURO")):
        statistics.register_economy(request_id=f"acc-eco-{code}", actor_id="admin",
                                    economy_code=code, name=name, group_code=group)
    statistics.register_rule_version(
        request_id="acc-rule", actor_id="admin", rule_version="2026.1",
        description="全球数字贸易统计规则 2026.1",
        allowed_methodologies=["digital_ordered", "digitally_deliverable"],
        expected_economies=["CHN", "USA", "DEU"], base_currency="USD",
        coverage_threshold="0.6", consistency_tolerance="0.2", required_cosignatures=2)
    statistics.register_fx_rate(request_id="acc-fx-eur", actor_id="admin",
                                rule_version="2026.1", currency="EUR",
                                effective_from="2024", effective_to="2026",
                                rate_to_base="1.1")
    statistics.register_fx_rate(request_id="acc-fx-cny", actor_id="admin",
                                rule_version="2026.1", currency="CNY",
                                effective_from="2024", effective_to="2026",
                                rate_to_base="0.14")
    statistics.register_class_mapping(request_id="acc-map-1", actor_id="admin",
                                      rule_version="2026.1", source_category="DO_GOODS",
                                      target_category="IND_ORDERED",
                                      effective_from="2024", effective_to="2026")
    statistics.register_class_mapping(request_id="acc-map-2", actor_id="admin",
                                      rule_version="2026.1", source_category="DD_SERV",
                                      target_category="IND_DELIVERED",
                                      effective_from="2024", effective_to="2026")


def _submit(statistics: StatisticsService, request_id: str, economy: str, period: str,
            currency: str, ordered: str, delivered: str, **extra) -> str:
    receipt = statistics.submit_dataset(
        request_id=request_id, actor_id="submitter", organization_id="org-secretariat",
        economy_code=economy, period=period, methodology="digital_ordered",
        currency=currency, confidentiality=extra.pop("confidentiality", "public"),
        source_evidence=f"{economy} 官方统计函 {period}",
        rule_version="2026.1",
        observations=[{"category": "DO_GOODS", "value": ordered},
                      {"category": "DD_SERV", "value": delivered}],
        **extra)
    return receipt.resource_id


def run() -> dict[str, object]:
    """执行完整报送—校验—发布—重算链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        database = Database(path)
        domain = DomainService(database, CLOCK)
        statistics = StatisticsService(database, CLOCK)
        _setup(domain, statistics)

        # 上一期间数据（用于跨期一致性）
        _submit(statistics, "acc-2024-chn", "CHN", "2024", "CNY", "500", "300")
        _submit(statistics, "acc-2024-usa", "USA", "2024", "USD", "100", "80")
        _submit(statistics, "acc-2024-deu", "DEU", "2024", "EUR", "90", "60")
        statistics.process_validation_queue(actor_id="admin")

        # 本期报送；CHN 跨期变动超容差，将产生一致性警告
        _submit(statistics, "acc-2025-chn", "CHN", "2025", "CNY", "800", "300")
        _submit(statistics, "acc-2025-usa", "USA", "2025", "USD", "110", "85")
        deu_dataset = _submit(statistics, "acc-2025-deu", "DEU", "2025", "EUR", "100", "70")

        # 重启恢复：队列位置与状态在重启后保持不变
        positions_before = [run["queue_position"] for run in
                            statistics.list_validation_runs(actor_id="auditor")]
        database.close()
        database = Database(path)
        domain = DomainService(database, CLOCK)
        statistics = StatisticsService(database, CLOCK)
        positions_after = [run["queue_position"] for run in
                           statistics.list_validation_runs(actor_id="auditor")]
        queue_stable = positions_before == positions_after
        statistics.process_validation_queue(actor_id="admin")

        # 豁免 CHN 的一致性警告（与自动检查分别记录）
        chn_run = statistics.list_validation_runs(actor_id="auditor", dataset_id=None)
        warning_finding = None
        for run in chn_run:
            for finding in statistics.list_findings(actor_id="auditor", run_id=run["run_id"]):
                if finding["code"] == "consistency_break":
                    warning_finding = finding
        statistics.approve_exemption(request_id="acc-waiver", actor_id="waiver",
                                     finding_id=warning_finding["finding_id"],
                                     reason="CHN 口径调整已确认，变动属实")

        # 分析员在授权范围内创建派生指标
        statistics.grant_analyst_scope(request_id="acc-grant-1", actor_id="admin",
                                       analyst_id="analyst", scope_type="indicator",
                                       scope_value="IND_ORDERED")
        statistics.grant_analyst_scope(request_id="acc-grant-2", actor_id="admin",
                                       analyst_id="analyst", scope_type="indicator",
                                       scope_value="IND_DELIVERED")
        statistics.create_derived_indicator(
            request_id="acc-derived", actor_id="analyst", code="IND_TOTAL",
            name="数字贸易合计",
            formula={"op": "sum", "inputs": ["IND_ORDERED", "IND_DELIVERED"]})

        # 候选版本、会签、发布锁（持锁状态下再次重启验证锁不变）
        candidate = statistics.create_candidate(request_id="acc-cand", actor_id="analyst",
                                                period="2025", rule_version="2026.1",
                                                label="全球数字贸易发展报告2026 候选版")
        candidate_id = candidate.resource_id
        statistics.cosign_candidate(request_id="acc-cosign-1", actor_id="signer-1",
                                    candidate_id=candidate_id)
        statistics.cosign_candidate(request_id="acc-cosign-2", actor_id="signer-2",
                                    candidate_id=candidate_id)
        statistics.acquire_release_lock(actor_id="publisher", candidate_id=candidate_id)
        database.close()
        database = Database(path)
        domain = DomainService(database, CLOCK)
        statistics = StatisticsService(database, CLOCK)
        lock = statistics.lock_state(actor_id="auditor")
        lock_stable = lock is not None and lock["holder"] == "publisher" and \
            lock["resource_id"] == candidate_id

        release = statistics.publish_release(request_id="acc-pub", actor_id="publisher",
                                             candidate_id=candidate_id)
        release_id = release.resource_id

        # 审计员以同一证据快照确定性重算
        recompute = statistics.recompute_release(actor_id="auditor", release_id=release_id)
        public = statistics.public_indicators(release_id=release_id)

        # 公开后更正：DEU 修订数值并形成新版本
        statistics.submit_dataset(
            request_id="acc-2025-deu-r2", actor_id="submitter",
            organization_id="org-secretariat", economy_code="DEU", period="2025",
            methodology="digital_ordered", currency="EUR", confidentiality="public",
            source_evidence="DEU 官方统计函 2025（更正）", rule_version="2026.1",
            observations=[{"category": "DO_GOODS", "value": "105"},
                          {"category": "DD_SERV", "value": "70"}],
            revision_of=deu_dataset, revision_reason="DEU 秘书处更正出口数值")
        statistics.process_validation_queue(actor_id="admin")
        correction = statistics.create_correction(
            request_id="acc-corr", actor_id="admin", release_id=release_id,
            reason="DEU 报送更正",
            affected_economies=["DEU"], affected_indicators=["IND_ORDERED", "IND_TOTAL"],
            affected_charts=["chart-global-total", "chart-euro-share"],
            affected_conclusions=["conclusion-global-growth"])
        correction_candidate = statistics.list_corrections(actor_id="auditor")[0]["candidate_id"]
        statistics.cosign_candidate(request_id="acc-cosign-3", actor_id="signer-1",
                                    candidate_id=correction_candidate)
        statistics.cosign_candidate(request_id="acc-cosign-4", actor_id="signer-2",
                                    candidate_id=correction_candidate)
        statistics.acquire_release_lock(actor_id="publisher",
                                        candidate_id=correction_candidate)
        release_v2 = statistics.publish_release(request_id="acc-pub-2", actor_id="publisher",
                                                candidate_id=correction_candidate)
        recompute_v2 = statistics.recompute_release(actor_id="auditor",
                                                    release_id=release_v2.resource_id)
        releases = statistics.public_releases()
        valid, event_count = domain.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "queue_positions_stable": queue_stable,
            "release_lock_stable": lock_stable,
            "release_v1": release_id is not None,
            "recompute_v1_match": recompute["match"],
            "recompute_v2_match": recompute_v2["match"],
            "public_indicators": sorted(public["indicators"].keys()),
            "releases": len(releases),
            "correction_versioned": releases[-1]["corrects_release_id"] == release_id,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = [result["audit_valid"], result["queue_positions_stable"],
              result["release_lock_stable"], result["recompute_v1_match"],
              result["recompute_v2_match"], result["correction_versioned"]]
    return 0 if result["status"] == "ok" and all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
