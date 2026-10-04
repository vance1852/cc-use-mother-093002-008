"""运行统计资料接收、校验与版本发布服务的离线端到端验收。

场景：三家报送机构按不同口径与币种报送 2025 期数据，秘书处完成
校验、豁免、派生指标、组卷、独立会签与加锁发布；随后一笔修订触发
更正版本；审计员用证据快照重算指标；中途模拟服务重启，核对校验
队列位置与发布锁保持不变。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import StatService

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


def _bootstrap(domain: DomainService, service: StatService) -> None:
    """登记机构、操作者与规则版本。"""

    domain.register_organization(request_id="org-secret", actor_id="bootstrap",
                                 organization_id="org-secret", name="数据秘书处")
    domain.register_actor(request_id="actor-admin", actor_id="bootstrap",
                          new_actor_id="admin", display_name="秘书处管理员",
                          role="admin", organization_id="org-secret")
    for org_id, name in [("org-a", "报送机构A"), ("org-b", "报送机构B"), ("org-c", "报送机构C"),
                         ("org-analysis", "分析中心"), ("org-review-1", "复核机构一"),
                         ("org-review-2", "复核机构二"), ("org-approver", "豁免批准机构"),
                         ("org-publisher", "发布机构"), ("org-audit", "审计机构")]:
        domain.register_organization(request_id=f"org-{org_id}", actor_id="admin",
                                     organization_id=org_id, name=name)
    for actor_id, role, org_id, name in [
            ("sub-a", "submitter", "org-a", "报送员A"), ("sub-b", "submitter", "org-b", "报送员B"),
            ("sub-c", "submitter", "org-c", "报送员C"),
            ("analyst-1", "analyst", "org-analysis", "分析员"),
            ("reviewer-1", "reviewer", "org-review-1", "复核员一"),
            ("reviewer-2", "reviewer", "org-review-2", "复核员二"),
            ("approver-1", "approver", "org-approver", "豁免批准人"),
            ("publisher-1", "publisher", "org-publisher", "发布人"),
            ("auditor-1", "auditor", "org-audit", "审计员")]:
        domain.register_actor(request_id=f"actor-{actor_id}", actor_id="admin",
                              new_actor_id=actor_id, display_name=name,
                              role=role, organization_id=org_id)
    service.register_rules_version(request_id="rules-rv2026", actor_id="admin",
                                   rules_version="RV-2026", definition=RULES)


def _submit(service: StatService, request_id: str, submitter: str, economy: str,
            methodology: str, records: list, currency: str = "USD",
            confidentiality: str = "public", supersedes: str | None = None,
            reason: str | None = None) -> str:
    receipt = service.submit_dataset(
        request_id=request_id, actor_id=submitter, economy=economy, period="2025",
        methodology=methodology, unit="million", currency=currency,
        confidentiality=confidentiality,
        source_evidence={"reference": f"{economy}-{methodology}-年报",
                         "hash": f"sha256:{economy}:{methodology}"},
        records=records, supersedes=supersedes, revision_reason=reason)
    return receipt.resource_id


def _validate(service: StatService, request_id: str, dataset_id: str) -> None:
    service.enqueue_validation(request_id=f"{request_id}-enq", actor_id="analyst-1",
                               dataset_id=dataset_id, rules_version="RV-2026")


def run() -> dict[str, object]:
    """执行完整报送—校验—发布—更正—重算链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stat.sqlite3"
        database = Database(path)
        domain = DomainService(database, CLOCK)
        service = StatService(database, CLOCK)
        _bootstrap(domain, service)

        # 1. 三家机构按各自口径与币种报送 2025 期数据。
        ds_a_dot = _submit(service, "sub-a-dot", "sub-a", "ECON-A", "digital_ordered",
                           [{"indicator": "DO-1", "value": "100"}])
        ds_a_dds = _submit(service, "sub-a-dds", "sub-a", "ECON-A", "digitally_delivered",
                           [{"indicator": "DD-1", "value": "60"}])
        ds_b_dot = _submit(service, "sub-b-dot", "sub-b", "ECON-B", "digital_ordered",
                           [{"indicator": "DO-1", "value": "200"}], currency="EUR")
        ds_b_dds = _submit(service, "sub-b-dds", "sub-b", "ECON-B", "digitally_delivered",
                           [{"indicator": "DD-1", "value": "80"}], currency="EUR")
        ds_c_dot = _submit(service, "sub-c-dot", "sub-c", "ECON-C", "digital_ordered",
                           [{"indicator": "DO-1", "value": "50"}], confidentiality="restricted")
        ds_c_dds = _submit(service, "sub-c-dds", "sub-c", "ECON-C", "digitally_delivered",
                           [{"indicator": "DD-1", "value": "30"},
                            {"indicator": "XX-9", "value": "1"}], confidentiality="restricted")
        for index, dataset_id in enumerate(
                [ds_a_dot, ds_a_dds, ds_b_dot, ds_b_dds, ds_c_dot, ds_c_dds]):
            _validate(service, f"val-{index}", dataset_id)

        # 2. 发布人取得发布锁；随后模拟服务重启。
        service.acquire_lock(request_id="lock-1", actor_id="publisher-1")
        database.close()
        database = Database(path)
        domain = DomainService(database, CLOCK)
        service = StatService(database, CLOCK)
        runs = service.list_validation_runs(actor_id="analyst-1")
        queue_positions = [run["queue_position"] for run in runs]
        lock_holder = service.get_lock(actor_id="admin")["holder"]

        # 3. 重启后按原队列顺序完成校验。
        for index in range(6):
            service.process_next_validation(request_id=f"proc-{index}", actor_id="analyst-1")
        findings = service.list_findings(actor_id="auditor-1")
        open_errors = [item for item in findings
                       if item["severity"] == "error" and item["status"] == "open"]

        # 4. 豁免唯一的分类错误（ECON-C 的未映射代码），记录复核意见。
        exemption = service.approve_exemption(
            request_id="exempt-1", actor_id="approver-1",
            finding_id=open_errors[0]["finding_id"], reason="代码为旧版附注项，本期忽略")
        service.record_review_opinion(
            request_id="opinion-1", actor_id="reviewer-1", target_type="dataset",
            target_id=ds_c_dds, conclusion="comment", content="未映射代码已核实为旧版附注项")

        # 5. 授权分析中心两个口径，创建派生指标 TDT = DOT + DDS。
        service.grant_analyst_scope(request_id="scope-1", actor_id="admin",
                                    organization_id="org-analysis", scope="digital_ordered")
        service.grant_analyst_scope(request_id="scope-2", actor_id="admin",
                                    organization_id="org-analysis",
                                    scope="digitally_delivered")
        service.create_derived_indicator(
            request_id="derived-tdt", actor_id="analyst-1", code="TDT",
            name="数字贸易总额", formula={"op": "sum", "terms": ["DOT", "DDS"]})

        # 6. 组卷、两家独立机构会签、发布 2025-v1。
        candidate = service.assemble_candidate(request_id="cand-1", actor_id="analyst-1",
                                               report_period="2025", rules_version="RV-2026")
        candidate_id = candidate.resource_id
        service.countersign_candidate(request_id="sign-1", actor_id="reviewer-1",
                                      candidate_id=candidate_id)
        service.countersign_candidate(request_id="sign-2", actor_id="reviewer-2",
                                      candidate_id=candidate_id)
        first = service.publish_candidate(request_id="pub-1", actor_id="publisher-1",
                                          candidate_id=candidate_id)
        v1 = first.resource_id
        recompute_v1 = service.recompute_indicator(actor_id="auditor-1", version_id=v1,
                                                   indicator_code="DOT")
        public_v1 = service.get_public_release("2025")

        # 7. ECON-B 修订 DOT（200→210 百万欧元），形成更正版本 2025-v2。
        ds_b_dot_v2 = _submit(service, "sub-b-dot-v2", "sub-b", "ECON-B", "digital_ordered",
                              [{"indicator": "DO-1", "value": "210"}], currency="EUR",
                              supersedes=ds_b_dot, reason="补报更正：口径对齐后调增")
        _validate(service, "val-b-dot-v2", ds_b_dot_v2)
        service.process_next_validation(request_id="proc-b-dot-v2", actor_id="analyst-1")
        candidate2 = service.assemble_candidate(request_id="cand-2", actor_id="analyst-1",
                                                report_period="2025", rules_version="RV-2026")
        service.countersign_candidate(request_id="sign-3", actor_id="reviewer-1",
                                      candidate_id=candidate2.resource_id)
        service.countersign_candidate(request_id="sign-4", actor_id="reviewer-2",
                                      candidate_id=candidate2.resource_id)
        second = service.publish_candidate(
            request_id="pub-2", actor_id="publisher-1",
            candidate_id=candidate2.resource_id, correction_of=v1,
            impact_note="ECON-B 数字订购贸易额修订")
        v2 = second.resource_id
        recompute_v2 = service.recompute_indicator(actor_id="auditor-1", version_id=v2,
                                                   indicator_code="TDT")
        public_v2 = service.get_public_release("2025")
        releases = service.list_releases(actor_id="auditor-1", report_period="2025")
        valid, event_count = domain.verify_audit()
        result = {
            "status": "ok",
            "queue_positions_after_restart": queue_positions,
            "lock_holder_after_restart": lock_holder,
            "exemption_id": exemption.resource_id,
            "first_version": v1,
            "second_version": v2,
            "v1_dot_global": recompute_v1["detail"]["global_total"],
            "v1_recompute_match": recompute_v1["match"],
            "v1_public_dot": public_v1["results"]["indicators"]["DOT"]["global_total"],
            "v2_tdt_global": recompute_v2["detail"]["global_total"],
            "v2_recompute_match": recompute_v2["match"],
            "v2_impact": service.get_release(actor_id="auditor-1", version_id=v2)["impact"],
            "v2_public_tdt": public_v2["results"]["indicators"]["TDT"]["global_total"],
            "releases": [(row["version_id"], row["status"]) for row in releases],
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] \
        and result["v1_recompute_match"] and result["v2_recompute_match"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
