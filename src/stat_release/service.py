"""统计资料接收、校验与版本发布的领域服务。

在基础服务（操作者、幂等回执、哈希链审计、SQLite 事务）之上实现：
报送与修订、按规则版本的自动校验、人工豁免、复核意见、派生指标、
候选版本组卷、独立会签、加锁发布、更正版本与审计确定性重算。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from digital_trade_foundation.audit import append_event, canonical_json, digest
from digital_trade_foundation.clock import Clock, SystemClock
from digital_trade_foundation.errors import (ConflictError, NotFoundError,
                                             PermissionDenied, ValidationError)
from digital_trade_foundation.models import WriteReceipt
from digital_trade_foundation.storage import Database

from .compute import ComputeError, compute_results, diff_results
from .rules import (CONFIDENTIALITIES, UNIT_SCALE, RulesError, consistency_findings,
                    coverage_ratio, decimal_text, formula_scopes, gap_findings,
                    normalize_records, parse_decimal, validate_definition, validate_formula)
from .schema import ensure_schema

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
RELEASE_LOCK = "release"
INTERNAL_READERS = ("admin", "analyst", "reviewer", "approver", "publisher", "auditor")


class StatService:
    """协调报送、校验、豁免、会签、发布与审计重算规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_schema(database.connection)

    # ---- 基础工具 ----

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value).strip() if value is not None else ""
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip() if value is not None else ""
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _require_reader(self, actor) -> None:
        if actor["role"] not in INTERNAL_READERS and actor["role"] != "submitter":
            raise PermissionDenied("当前角色不能查阅统计资料")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()))
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _rules_row(self, connection, rules_version: str):
        row = connection.execute(
            "SELECT * FROM stat_rules_versions WHERE rules_version=?", (rules_version,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return row

    def _latest_release(self, connection, period: str, *, inclusive: bool):
        operator = "<=" if inclusive else "<"
        return connection.execute(
            f"SELECT * FROM stat_releases WHERE status='published' AND report_period{operator}? "
            "ORDER BY report_period DESC, sequence DESC LIMIT 1", (period,)).fetchone()

    def _run_approvers(self, connection, run_ids: list[str]) -> set[str]:
        if not run_ids:
            return set()
        marks = ",".join("?" for _ in run_ids)
        rows = connection.execute(
            f"SELECT DISTINCT e.approved_by AS approver FROM stat_exemptions e "
            f"JOIN stat_findings f ON f.finding_id=e.finding_id WHERE f.run_id IN ({marks})",
            run_ids).fetchall()
        return {row["approver"] for row in rows}

    def _run_exemption_descriptors(self, connection, run_ids: list[str]) -> list[dict[str, str]]:
        if not run_ids:
            return []
        marks = ",".join("?" for _ in run_ids)
        rows = connection.execute(
            f"SELECT e.exemption_id AS exemption_id, f.check_type AS check_type, f.subject AS subject "
            f"FROM stat_exemptions e JOIN stat_findings f ON f.finding_id=e.finding_id "
            f"WHERE f.run_id IN ({marks}) ORDER BY e.exemption_id", run_ids).fetchall()
        return [{"exemption_id": row["exemption_id"], "check_type": row["check_type"],
                 "subject": row["subject"]} for row in rows]

    def _snapshot_inputs(self, connection, snapshot: dict[str, Any], *, require_current: bool):
        """按快照重建计算输入，并核对数据集内容摘要未被改动。"""

        datasets = []
        for entry in snapshot["datasets"]:
            row = connection.execute(
                "SELECT * FROM stat_datasets WHERE dataset_id=?", (entry["dataset_id"],)).fetchone()
            if row is None:
                raise ConflictError(f"快照数据集 {entry['dataset_id']} 不存在")
            if row["records_hash"] != entry["records_hash"]:
                raise ConflictError(f"快照数据集 {entry['dataset_id']} 内容已被改动")
            if require_current and row["status"] != "received":
                raise ConflictError(f"快照数据集 {entry['dataset_id']} 已被新修订替代，请重新组卷")
            datasets.append({"dataset_id": row["dataset_id"], "economy": row["economy"],
                             "currency": row["currency"], "unit": row["unit"],
                             "confidentiality": row["confidentiality"],
                             "records": json.loads(row["records_json"])})
        derived = [{"code": item["code"], "name": item.get("name") or item["code"],
                    "formula": item["formula"]} for item in snapshot.get("derived", [])]
        exempted = {(item["check_type"], item["subject"])
                    for item in snapshot.get("exemptions", [])}
        return datasets, derived, exempted

    # ---- 规则版本 ----

    def register_rules_version(self, *, request_id: str, actor_id: str,
                               rules_version: str, definition: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rules_version": rules_version, "definition": definition}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rules_version = self._identifier(rules_version, "rules_version")
            try:
                normalized = validate_definition(definition)
            except RulesError as exc:
                raise ValidationError(str(exc)) from exc
            definition_hash = digest(normalized)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_rules_versions(rules_version,definition_json,definition_hash,"
                        "status,created_by,created_at) VALUES(?,?,?,'active',?,?)",
                        (rules_version, canonical_json(normalized), definition_hash,
                         actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("规则版本已经存在") from exc
                append_event(connection, actor_id=actor_id, action="stat.rules.registered",
                             resource_type="stat_rules", resource_id=rules_version,
                             detail={"definition_hash": definition_hash}, occurred_at=self._now())
                return "stat_rules", rules_version, {"rules_version": rules_version,
                                                     "definition_hash": definition_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.rules.register", payload=payload, create=create)

    def list_rules_versions(self, *, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        rows = connection.execute(
            "SELECT * FROM stat_rules_versions ORDER BY rules_version").fetchall()
        return [{"rules_version": row["rules_version"], "definition_hash": row["definition_hash"],
                 "status": row["status"], "definition": json.loads(row["definition_json"]),
                 "created_by": row["created_by"], "created_at": row["created_at"]} for row in rows]

    # ---- 数据集报送与修订 ----

    def submit_dataset(self, *, request_id: str, actor_id: str, economy: str, period: str,
                       methodology: str, unit: str, currency: str, confidentiality: str,
                       source_evidence: dict[str, Any], records: list[dict[str, Any]],
                       organization_id: str | None = None, dataset_id: str | None = None,
                       supersedes: str | None = None,
                       revision_reason: str | None = None) -> WriteReceipt:
        if not isinstance(records, list) or not records:
            raise ValidationError("records 必须是非空数组")
        if not all(isinstance(item, dict) for item in records):
            raise ValidationError("records 元素必须是对象")
        if not isinstance(source_evidence, dict):
            raise ValidationError("source_evidence 必须是对象")
        payload = {"actor_id": actor_id, "economy": economy, "period": period,
                   "methodology": methodology, "unit": unit, "currency": currency,
                   "confidentiality": confidentiality, "source_evidence": source_evidence,
                   "records": records, "organization_id": organization_id,
                   "dataset_id": dataset_id, "supersedes": supersedes,
                   "revision_reason": revision_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "submitter", "admin")
            if actor["role"] == "submitter":
                if organization_id is not None and organization_id != actor["organization_id"]:
                    raise PermissionDenied("报送员只能处理本机构资料")
                organization_id = actor["organization_id"]
            else:
                organization_id = self._identifier(organization_id, "organization_id")
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (organization_id,)).fetchone() is None:
                    raise NotFoundError("组织不存在")
            economy = self._identifier(economy, "economy")
            period = self._identifier(period, "period")
            methodology = self._identifier(methodology, "methodology")
            currency = self._identifier(currency, "currency").upper()
            if unit not in UNIT_SCALE:
                raise ValidationError("unit 必须是 unit、thousand、million 或 billion")
            if confidentiality not in CONFIDENTIALITIES:
                raise ValidationError("confidentiality 不在允许范围内")
            reference = self._text(source_evidence.get("reference"), "source_evidence.reference")
            evidence_hash = self._text(source_evidence.get("hash"), "source_evidence.hash")
            evidence = {"reference": reference, "hash": evidence_hash}
            if dataset_id is not None:
                dataset_id = self._identifier(dataset_id, "dataset_id")
            if supersedes is not None:
                target = connection.execute(
                    "SELECT * FROM stat_datasets WHERE dataset_id=?", (supersedes,)).fetchone()
                if target is None:
                    raise NotFoundError("被替代的报送不存在")
                if target["organization_id"] != organization_id:
                    raise PermissionDenied("只能替代本机构的报送")
                if target["economy"] != economy:
                    raise ValidationError("修订必须针对同一经济体")
                if target["status"] != "received":
                    raise ConflictError("被替代的报送已被替代")
                if revision_reason is None or not str(revision_reason).strip():
                    raise ValidationError("修订必须声明替代对象和原因")
                revision_reason = self._text(revision_reason, "revision_reason", 500)
            records_hash = digest(records)

            def create() -> tuple[str, str, dict[str, Any]]:
                new_id = dataset_id or f"ds-{uuid.uuid4().hex}"
                try:
                    connection.execute(
                        "INSERT INTO stat_datasets(dataset_id,organization_id,economy,period,methodology,"
                        "unit,currency,confidentiality,source_evidence_json,records_json,records_hash,"
                        "supersedes,revision_reason,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'received',?,?)",
                        (new_id, organization_id, economy, period, methodology, unit, currency,
                         confidentiality, canonical_json(evidence), canonical_json(records),
                         records_hash, supersedes, revision_reason, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("数据集编号已经存在") from exc
                if supersedes is not None:
                    connection.execute(
                        "UPDATE stat_datasets SET status='superseded' WHERE dataset_id=?",
                        (supersedes,))
                append_event(connection, actor_id=actor_id, action="stat.dataset.submitted",
                             resource_type="stat_dataset", resource_id=new_id,
                             detail={"organization_id": organization_id, "economy": economy,
                                     "period": period, "methodology": methodology,
                                     "confidentiality": confidentiality,
                                     "records_hash": records_hash, "supersedes": supersedes,
                                     "revision_reason": revision_reason},
                             occurred_at=self._now())
                return "stat_dataset", new_id, {"dataset_id": new_id, "records_hash": records_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.dataset.submit", payload=payload, create=create)

    def _dataset_dict(self, row) -> dict[str, Any]:
        return {"dataset_id": row["dataset_id"], "organization_id": row["organization_id"],
                "economy": row["economy"], "period": row["period"],
                "methodology": row["methodology"], "unit": row["unit"],
                "currency": row["currency"], "confidentiality": row["confidentiality"],
                "source_evidence": json.loads(row["source_evidence_json"]),
                "records": json.loads(row["records_json"]), "records_hash": row["records_hash"],
                "supersedes": row["supersedes"], "revision_reason": row["revision_reason"],
                "status": row["status"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    def get_dataset(self, *, actor_id: str, dataset_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_reader(actor)
        row = connection.execute(
            "SELECT * FROM stat_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        if row is None:
            raise NotFoundError("数据集不存在")
        if actor["role"] == "submitter" and row["organization_id"] != actor["organization_id"]:
            raise PermissionDenied("报送员只能处理本机构资料")
        return self._dataset_dict(row)

    def list_datasets(self, *, actor_id: str, organization_id: str | None = None,
                      period: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_reader(actor)
        if actor["role"] == "submitter":
            organization_id = actor["organization_id"]
        query = "SELECT * FROM stat_datasets WHERE 1=1"
        parameters: list[Any] = []
        if organization_id:
            query += " AND organization_id=?"
            parameters.append(organization_id)
        if period:
            query += " AND period=?"
            parameters.append(period)
        query += " ORDER BY dataset_id"
        return [self._dataset_dict(row)
                for row in connection.execute(query, parameters).fetchall()]

    # ---- 校验队列 ----

    def enqueue_validation(self, *, request_id: str, actor_id: str, dataset_id: str,
                           rules_version: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "rules_version": rules_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "analyst", "admin")
            dataset = connection.execute(
                "SELECT * FROM stat_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            if dataset is None:
                raise NotFoundError("数据集不存在")
            if dataset["status"] != "received":
                raise ConflictError("数据集已被修订替代，请校验最新报送")
            self._rules_row(connection, self._identifier(rules_version, "rules_version"))
            position = connection.execute(
                "SELECT COALESCE(MAX(queue_position),0)+1 AS position FROM stat_validation_runs"
            ).fetchone()["position"]

            def create() -> tuple[str, str, dict[str, Any]]:
                run_id = f"vr-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO stat_validation_runs(run_id,dataset_id,rules_version,queue_position,"
                    "status,created_by,created_at) VALUES(?,?,?,?,'queued',?,?)",
                    (run_id, dataset_id, rules_version, position, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="stat.validation.queued",
                             resource_type="stat_validation", resource_id=run_id,
                             detail={"dataset_id": dataset_id, "rules_version": rules_version,
                                     "queue_position": position}, occurred_at=self._now())
                return "stat_validation", run_id, {"run_id": run_id, "queue_position": position}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.validation.enqueue", payload=payload, create=create)

    def process_next_validation(self, *, request_id: str, actor_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "analyst", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                run = connection.execute(
                    "SELECT * FROM stat_validation_runs WHERE status='queued' "
                    "ORDER BY queue_position LIMIT 1").fetchone()
                if run is None:
                    return "stat_validation_queue", "empty", {"processed": False}
                dataset = connection.execute(
                    "SELECT * FROM stat_datasets WHERE dataset_id=?",
                    (run["dataset_id"],)).fetchone()
                rules = self._rules_row(connection, run["rules_version"])
                definition = json.loads(rules["definition_json"])
                base = self._latest_release(connection, dataset["period"], inclusive=True)
                base_values: dict[str, str] = {}
                if base is not None:
                    base_results = json.loads(base["internal_results_json"])
                    for code, item in base_results.get("indicators", {}).items():
                        value = item.get("economies", {}).get(dataset["economy"])
                        if value is not None:
                            base_values[code] = value
                findings: list[dict[str, Any]] = []
                if dataset["methodology"] not in definition["methodologies"]:
                    findings.append({"check_type": "structure", "severity": "error",
                                     "subject": "methodology",
                                     "detail": {"methodology": dataset["methodology"],
                                                "reason": "口径未在规则版本中声明"}})
                normalized, found = normalize_records(
                    json.loads(dataset["records_json"]), definition,
                    period=dataset["period"], currency=dataset["currency"], unit=dataset["unit"])
                findings.extend(found)
                findings.extend(gap_findings(
                    definition, economy=dataset["economy"], methodology=dataset["methodology"],
                    present_indicators=[item["indicator"] for item in normalized]))
                findings.extend(consistency_findings(
                    definition, economy=dataset["economy"], normalized=normalized,
                    base_values=base_values))
                findings.sort(key=lambda item: (item["check_type"], item["subject"],
                                                canonical_json(item["detail"])))
                errors = sum(1 for item in findings if item["severity"] == "error")
                warnings = len(findings) - errors
                for item in findings:
                    connection.execute(
                        "INSERT INTO stat_findings(finding_id,run_id,dataset_id,check_type,severity,"
                        "subject,detail_json,status,created_at) VALUES(?,?,?,?,?,?,?,'open',?)",
                        (f"fd-{uuid.uuid4().hex}", run["run_id"], dataset["dataset_id"],
                         item["check_type"], item["severity"], item["subject"],
                         canonical_json(item["detail"]), self._now()))
                checks: dict[str, int] = {}
                for item in findings:
                    checks[item["check_type"]] = checks.get(item["check_type"], 0) + 1
                report = {"dataset_id": dataset["dataset_id"], "rules_version": run["rules_version"],
                          "period": dataset["period"],
                          "base_release": base["version_id"] if base else None,
                          "normalized": normalized,
                          "summary": {"errors": errors, "warnings": warnings, "checks": checks}}
                connection.execute(
                    "UPDATE stat_validation_runs SET status='completed', base_release=?, "
                    "report_json=?, completed_at=? WHERE run_id=?",
                    (base["version_id"] if base else None, canonical_json(report),
                     self._now(), run["run_id"]))
                append_event(connection, actor_id=actor_id, action="stat.validation.completed",
                             resource_type="stat_validation", resource_id=run["run_id"],
                             detail={"dataset_id": dataset["dataset_id"],
                                     "rules_version": run["rules_version"],
                                     "queue_position": run["queue_position"],
                                     "errors": errors, "warnings": warnings},
                             occurred_at=self._now())
                return "stat_validation", run["run_id"], {
                    "processed": True, "run_id": run["run_id"],
                    "dataset_id": dataset["dataset_id"],
                    "queue_position": run["queue_position"],
                    "errors": errors, "warnings": warnings}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.validation.process_next",
                                    payload=payload, create=create)

    def list_validation_runs(self, *, actor_id: str,
                             dataset_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_reader(actor)
        query = ("SELECT r.* FROM stat_validation_runs r "
                 "JOIN stat_datasets d ON d.dataset_id=r.dataset_id WHERE 1=1")
        parameters: list[Any] = []
        if actor["role"] == "submitter":
            query += " AND d.organization_id=?"
            parameters.append(actor["organization_id"])
        if dataset_id:
            query += " AND r.dataset_id=?"
            parameters.append(dataset_id)
        query += " ORDER BY r.queue_position"
        items = []
        for row in connection.execute(query, parameters).fetchall():
            report = json.loads(row["report_json"]) if row["report_json"] else None
            items.append({"run_id": row["run_id"], "dataset_id": row["dataset_id"],
                          "rules_version": row["rules_version"],
                          "queue_position": row["queue_position"], "status": row["status"],
                          "base_release": row["base_release"],
                          "summary": report["summary"] if report else None,
                          "created_at": row["created_at"], "completed_at": row["completed_at"]})
        return items

    # ---- 发现项、豁免与复核意见 ----

    def list_findings(self, *, actor_id: str, dataset_id: str | None = None,
                      run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_reader(actor)
        query = ("SELECT f.*, e.exemption_id AS exemption_id, e.approved_by AS approved_by, "
                 "e.reason AS exemption_reason FROM stat_findings f "
                 "JOIN stat_datasets d ON d.dataset_id=f.dataset_id "
                 "LEFT JOIN stat_exemptions e ON e.finding_id=f.finding_id WHERE 1=1")
        parameters: list[Any] = []
        if actor["role"] == "submitter":
            query += " AND d.organization_id=?"
            parameters.append(actor["organization_id"])
        if dataset_id:
            query += " AND f.dataset_id=?"
            parameters.append(dataset_id)
        if run_id:
            query += " AND f.run_id=?"
            parameters.append(run_id)
        if status:
            query += " AND f.status=?"
            parameters.append(status)
        query += " ORDER BY f.created_at, f.finding_id"
        items = []
        for row in connection.execute(query, parameters).fetchall():
            item = {"finding_id": row["finding_id"], "run_id": row["run_id"],
                    "dataset_id": row["dataset_id"], "check_type": row["check_type"],
                    "severity": row["severity"], "subject": row["subject"],
                    "detail": json.loads(row["detail_json"]), "status": row["status"],
                    "created_at": row["created_at"]}
            if row["exemption_id"]:
                item["exemption"] = {"exemption_id": row["exemption_id"],
                                     "approved_by": row["approved_by"],
                                     "reason": row["exemption_reason"]}
            items.append(item)
        return items

    def approve_exemption(self, *, request_id: str, actor_id: str, finding_id: str,
                          reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "finding_id": finding_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "approver")
            row = connection.execute(
                "SELECT f.*, d.organization_id AS dataset_org FROM stat_findings f "
                "JOIN stat_datasets d ON d.dataset_id=f.dataset_id WHERE f.finding_id=?",
                (finding_id,)).fetchone()
            if row is None:
                raise NotFoundError("发现项不存在")
            if row["status"] != "open":
                raise ConflictError("发现项已处理")
            if row["dataset_org"] == actor["organization_id"]:
                raise PermissionDenied("不能豁免本机构报送的发现项")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                exemption_id = f"ex-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO stat_exemptions(exemption_id,finding_id,approved_by,reason,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (exemption_id, finding_id, actor_id, reason, self._now()))
                connection.execute(
                    "UPDATE stat_findings SET status='exempted' WHERE finding_id=?", (finding_id,))
                append_event(connection, actor_id=actor_id, action="stat.exemption.approved",
                             resource_type="stat_finding", resource_id=finding_id,
                             detail={"exemption_id": exemption_id,
                                     "check_type": row["check_type"], "subject": row["subject"],
                                     "reason": reason}, occurred_at=self._now())
                return "stat_exemption", exemption_id, {"exemption_id": exemption_id,
                                                        "finding_id": finding_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.exemption.approve", payload=payload, create=create)

    def record_review_opinion(self, *, request_id: str, actor_id: str, target_type: str,
                              target_id: str, conclusion: str, content: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "target_type": target_type, "target_id": target_id,
                   "conclusion": conclusion, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            if target_type not in ("dataset", "candidate"):
                raise ValidationError("target_type 必须是 dataset 或 candidate")
            if conclusion not in ("approve", "reject", "comment"):
                raise ValidationError("conclusion 不在允许范围内")
            content = self._text(content, "content", 2000)
            if target_type == "dataset":
                exists = connection.execute("SELECT 1 FROM stat_datasets WHERE dataset_id=?",
                                            (target_id,)).fetchone()
            else:
                exists = connection.execute("SELECT 1 FROM stat_candidates WHERE candidate_id=?",
                                            (target_id,)).fetchone()
            if exists is None:
                raise NotFoundError("复核对象不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                opinion_id = f"op-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO stat_review_opinions(opinion_id,target_type,target_id,reviewer_id,"
                    "conclusion,content,created_at) VALUES(?,?,?,?,?,?,?)",
                    (opinion_id, target_type, target_id, actor_id, conclusion, content, self._now()))
                append_event(connection, actor_id=actor_id, action="stat.review.recorded",
                             resource_type=f"stat_{target_type}", resource_id=target_id,
                             detail={"opinion_id": opinion_id, "conclusion": conclusion},
                             occurred_at=self._now())
                return "stat_opinion", opinion_id, {"opinion_id": opinion_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.review.record", payload=payload, create=create)

    def list_review_opinions(self, *, actor_id: str, target_type: str,
                             target_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_reader(actor)
        rows = connection.execute(
            "SELECT * FROM stat_review_opinions WHERE target_type=? AND target_id=? "
            "ORDER BY created_at, opinion_id", (target_type, target_id)).fetchall()
        return [{"opinion_id": row["opinion_id"], "target_type": row["target_type"],
                 "target_id": row["target_id"], "reviewer_id": row["reviewer_id"],
                 "conclusion": row["conclusion"], "content": row["content"],
                 "created_at": row["created_at"]} for row in rows]

    # ---- 派生指标 ----

    def grant_analyst_scope(self, *, request_id: str, actor_id: str, organization_id: str,
                            scope: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id, "scope": scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            organization_id = self._identifier(organization_id, "organization_id")
            scope = self._identifier(scope, "scope")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_analyst_scopes(organization_id,scope,granted_by,created_at) "
                        "VALUES(?,?,?,?)",
                        (organization_id, scope, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("该机构已获得此口径授权") from exc
                append_event(connection, actor_id=actor_id, action="stat.scope.granted",
                             resource_type="organization", resource_id=organization_id,
                             detail={"scope": scope}, occurred_at=self._now())
                return "stat_scope", f"{organization_id}:{scope}", {
                    "organization_id": organization_id, "scope": scope}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.scope.grant", payload=payload, create=create)

    def create_derived_indicator(self, *, request_id: str, actor_id: str, code: str,
                                 name: str, formula: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "code": code, "name": name, "formula": formula}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "analyst", "admin")
            code = self._identifier(code, "code")
            name = self._text(name, "name")
            rules = connection.execute(
                "SELECT * FROM stat_rules_versions WHERE status='active' "
                "ORDER BY created_at DESC, rules_version DESC LIMIT 1").fetchone()
            if rules is None:
                raise NotFoundError("没有可用的规则版本")
            definition = json.loads(rules["definition_json"])
            try:
                normalized = validate_formula(formula, definition)
            except RulesError as exc:
                raise ValidationError(str(exc)) from exc
            scopes = formula_scopes(normalized, definition)
            if actor["role"] != "admin":
                for scope in scopes:
                    grant = connection.execute(
                        "SELECT 1 FROM stat_analyst_scopes WHERE organization_id=? AND scope=?",
                        (actor["organization_id"], scope)).fetchone()
                    if grant is None:
                        raise PermissionDenied(f"分析员未获得口径 {scope} 的授权")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_derived_indicators(code,rules_version,name,formula_json,"
                        "scopes_json,organization_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (code, rules["rules_version"], name, canonical_json(normalized),
                         canonical_json(scopes), actor["organization_id"], actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("派生指标代码在该规则版本下已存在") from exc
                append_event(connection, actor_id=actor_id, action="stat.derived.created",
                             resource_type="stat_derived", resource_id=code,
                             detail={"rules_version": rules["rules_version"], "scopes": scopes},
                             occurred_at=self._now())
                return "stat_derived", code, {"code": code,
                                              "rules_version": rules["rules_version"],
                                              "scopes": scopes}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.derived.create", payload=payload, create=create)

    def list_derived_indicators(self, *, actor_id: str,
                                rules_version: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        query = "SELECT * FROM stat_derived_indicators"
        parameters: list[Any] = []
        if rules_version:
            query += " WHERE rules_version=?"
            parameters.append(rules_version)
        query += " ORDER BY code"
        return [{"code": row["code"], "rules_version": row["rules_version"], "name": row["name"],
                 "formula": json.loads(row["formula_json"]),
                 "scopes": json.loads(row["scopes_json"]),
                 "organization_id": row["organization_id"], "created_by": row["created_by"],
                 "created_at": row["created_at"]}
                for row in connection.execute(query, parameters).fetchall()]

    # ---- 候选版本与会签 ----

    def assemble_candidate(self, *, request_id: str, actor_id: str, report_period: str,
                           rules_version: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "report_period": report_period,
                   "rules_version": rules_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "analyst", "admin")
            report_period = self._identifier(report_period, "report_period")
            rules_version = self._identifier(rules_version, "rules_version")
            rules = self._rules_row(connection, rules_version)
            definition = json.loads(rules["definition_json"])
            datasets = connection.execute(
                "SELECT * FROM stat_datasets WHERE period=? AND status='received' "
                "ORDER BY dataset_id", (report_period,)).fetchall()
            if not datasets:
                raise ValidationError("该报告期没有可用的报送数据")
            blockers = []
            runs: dict[str, Any] = {}
            for dataset in datasets:
                run = connection.execute(
                    "SELECT * FROM stat_validation_runs WHERE dataset_id=? AND rules_version=? "
                    "AND status='completed' ORDER BY completed_at DESC, run_id DESC LIMIT 1",
                    (dataset["dataset_id"], rules_version)).fetchone()
                if run is None:
                    blockers.append(f"数据集 {dataset['dataset_id']} 未完成规则 "
                                    f"{rules_version} 的校验")
                    continue
                open_errors = connection.execute(
                    "SELECT COUNT(*) AS count FROM stat_findings WHERE run_id=? "
                    "AND severity='error' AND status='open'", (run["run_id"],)).fetchone()["count"]
                if open_errors:
                    blockers.append(f"数据集 {dataset['dataset_id']} 存在 "
                                    f"{open_errors} 个未豁免错误")
                    continue
                runs[dataset["dataset_id"]] = run
            if blockers:
                raise ConflictError("；".join(blockers))
            seen: dict[tuple[str, str], str] = {}
            for dataset in datasets:
                run = runs[dataset["dataset_id"]]
                exempted = {(item["check_type"], item["subject"])
                            for item in self._run_exemption_descriptors(connection,
                                                                        [run["run_id"]])}
                normalized, _ = normalize_records(
                    json.loads(dataset["records_json"]), definition, period=report_period,
                    currency=dataset["currency"], unit=dataset["unit"], exempted=exempted)
                for item in normalized:
                    key = (dataset["economy"], item["indicator"])
                    if key in seen:
                        raise ConflictError(
                            f"经济体 {key[0]} 的指标 {key[1]} 在多个数据集中重复")
                    seen[key] = dataset["dataset_id"]
            run_ids = [runs[dataset["dataset_id"]]["run_id"] for dataset in datasets]
            methodologies = sorted({dataset["methodology"] for dataset in datasets})
            derived = []
            for row in connection.execute(
                    "SELECT * FROM stat_derived_indicators WHERE rules_version=? ORDER BY code",
                    (rules_version,)).fetchall():
                scopes = json.loads(row["scopes_json"])
                if all(scope in methodologies for scope in scopes):
                    derived.append({"code": row["code"], "name": row["name"],
                                    "formula": json.loads(row["formula_json"])})
            exemptions = self._run_exemption_descriptors(connection, run_ids)
            growth_base = self._latest_release(connection, report_period, inclusive=False)
            snapshot = {
                "report_period": report_period,
                "rules_version": rules_version,
                "rules_hash": rules["definition_hash"],
                "datasets": [{"dataset_id": dataset["dataset_id"],
                              "records_hash": dataset["records_hash"],
                              "run_id": runs[dataset["dataset_id"]]["run_id"]}
                             for dataset in datasets],
                "exemptions": exemptions,
                "derived": derived,
                "base_release": growth_base["version_id"] if growth_base else None,
            }
            snapshot_hash = digest(snapshot)
            covered = sorted({dataset["economy"] for dataset in datasets})
            coverage = decimal_text(coverage_ratio(definition, covered))

            def create() -> tuple[str, str, dict[str, Any]]:
                candidate_id = f"cd-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO stat_candidates(candidate_id,report_period,rules_version,"
                    "snapshot_json,snapshot_hash,coverage,status,created_by,creator_org,created_at) "
                    "VALUES(?,?,?,?,?,?,'ready',?,?,?)",
                    (candidate_id, report_period, rules_version, canonical_json(snapshot),
                     snapshot_hash, coverage, actor_id, actor["organization_id"], self._now()))
                append_event(connection, actor_id=actor_id, action="stat.candidate.assembled",
                             resource_type="stat_candidate", resource_id=candidate_id,
                             detail={"report_period": report_period, "rules_version": rules_version,
                                     "snapshot_hash": snapshot_hash, "coverage": coverage,
                                     "datasets": len(datasets)}, occurred_at=self._now())
                return "stat_candidate", candidate_id, {
                    "candidate_id": candidate_id, "snapshot_hash": snapshot_hash,
                    "coverage": coverage, "datasets": len(datasets)}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.candidate.assemble", payload=payload, create=create)

    def get_candidate(self, *, actor_id: str, candidate_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *INTERNAL_READERS)
        row = connection.execute(
            "SELECT * FROM stat_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if row is None:
            raise NotFoundError("候选版本不存在")
        signers = connection.execute(
            "SELECT * FROM stat_countersigns WHERE candidate_id=? ORDER BY created_at, signer_id",
            (candidate_id,)).fetchall()
        return {"candidate_id": row["candidate_id"], "report_period": row["report_period"],
                "rules_version": row["rules_version"],
                "snapshot": json.loads(row["snapshot_json"]),
                "snapshot_hash": row["snapshot_hash"], "coverage": row["coverage"],
                "status": row["status"], "created_by": row["created_by"],
                "creator_org": row["creator_org"], "created_at": row["created_at"],
                "countersigns": [{"signer_id": item["signer_id"],
                                  "signer_org": item["signer_org"],
                                  "created_at": item["created_at"]} for item in signers]}

    def countersign_candidate(self, *, request_id: str, actor_id: str,
                              candidate_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "candidate_id": candidate_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            candidate = connection.execute(
                "SELECT * FROM stat_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选版本不存在")
            if candidate["status"] not in ("ready", "countersigned"):
                raise ConflictError("候选版本当前状态不能会签")
            if actor_id == candidate["created_by"] or \
                    actor["organization_id"] == candidate["creator_org"]:
                raise PermissionDenied("会签人员必须独立于候选版本编制方")
            snapshot = json.loads(candidate["snapshot_json"])
            run_ids = [item["run_id"] for item in snapshot["datasets"]]
            if actor_id in self._run_approvers(connection, run_ids):
                raise PermissionDenied("豁免批准人不得会签同一版本")
            rules = self._rules_row(connection, candidate["rules_version"])
            required = json.loads(rules["definition_json"])["required_countersigns"]

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_countersigns(candidate_id,signer_id,signer_org,created_at) "
                        "VALUES(?,?,?,?)",
                        (candidate_id, actor_id, actor["organization_id"], self._now()))
                except Exception as exc:
                    raise ConflictError("该人员已会签此候选版本") from exc
                orgs = connection.execute(
                    "SELECT COUNT(DISTINCT signer_org) AS count FROM stat_countersigns "
                    "WHERE candidate_id=?", (candidate_id,)).fetchone()["count"]
                status = candidate["status"]
                if orgs >= required and status == "ready":
                    status = "countersigned"
                    connection.execute(
                        "UPDATE stat_candidates SET status='countersigned' WHERE candidate_id=?",
                        (candidate_id,))
                append_event(connection, actor_id=actor_id, action="stat.candidate.countersigned",
                             resource_type="stat_candidate", resource_id=candidate_id,
                             detail={"signer_id": actor_id, "signer_orgs": orgs,
                                     "required_orgs": required, "status": status},
                             occurred_at=self._now())
                return "stat_candidate", candidate_id, {
                    "candidate_id": candidate_id, "signer_orgs": orgs,
                    "required_orgs": required, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.candidate.countersign",
                                    payload=payload, create=create)

    # ---- 发布锁 ----

    def acquire_lock(self, *, request_id: str, actor_id: str,
                     lock_name: str = RELEASE_LOCK) -> WriteReceipt:
        payload = {"actor_id": actor_id, "lock_name": lock_name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "publisher", "admin")
            lock_name = self._identifier(lock_name, "lock_name")
            existing = connection.execute(
                "SELECT * FROM stat_release_locks WHERE lock_name=?", (lock_name,)).fetchone()
            if existing is not None and existing["holder"] != actor_id:
                raise ConflictError("发布锁已被其他操作者持有")

            def create() -> tuple[str, str, dict[str, Any]]:
                if existing is None:
                    connection.execute(
                        "INSERT INTO stat_release_locks(lock_name,holder,acquired_at) "
                        "VALUES(?,?,?)", (lock_name, actor_id, self._now()))
                    append_event(connection, actor_id=actor_id, action="stat.lock.acquired",
                                 resource_type="stat_lock", resource_id=lock_name,
                                 detail={"holder": actor_id}, occurred_at=self._now())
                return "stat_lock", lock_name, {"lock_name": lock_name, "holder": actor_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.lock.acquire", payload=payload, create=create)

    def release_lock(self, *, request_id: str, actor_id: str,
                     lock_name: str = RELEASE_LOCK) -> WriteReceipt:
        payload = {"actor_id": actor_id, "lock_name": lock_name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "publisher", "admin")
            lock_name = self._identifier(lock_name, "lock_name")
            existing = connection.execute(
                "SELECT * FROM stat_release_locks WHERE lock_name=?", (lock_name,)).fetchone()
            if existing is None:
                raise NotFoundError("发布锁不存在")
            if existing["holder"] != actor_id and actor["role"] != "admin":
                raise PermissionDenied("只能释放本人持有的发布锁")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "DELETE FROM stat_release_locks WHERE lock_name=?", (lock_name,))
                append_event(connection, actor_id=actor_id, action="stat.lock.released",
                             resource_type="stat_lock", resource_id=lock_name,
                             detail={"holder": existing["holder"]}, occurred_at=self._now())
                return "stat_lock", lock_name, {"lock_name": lock_name, "released": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.lock.release", payload=payload, create=create)

    def get_lock(self, *, actor_id: str, lock_name: str = RELEASE_LOCK) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *INTERNAL_READERS)
        row = connection.execute(
            "SELECT * FROM stat_release_locks WHERE lock_name=?", (lock_name,)).fetchone()
        if row is None:
            raise NotFoundError("发布锁不存在")
        return {"lock_name": row["lock_name"], "holder": row["holder"],
                "acquired_at": row["acquired_at"]}

    # ---- 发布与更正 ----

    def publish_candidate(self, *, request_id: str, actor_id: str, candidate_id: str,
                          correction_of: str | None = None,
                          impact_note: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "candidate_id": candidate_id,
                   "correction_of": correction_of, "impact_note": impact_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "publisher")
            candidate = connection.execute(
                "SELECT * FROM stat_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选版本不存在")
            if candidate["status"] != "countersigned":
                raise ConflictError("候选版本尚未完成独立会签")
            rules = self._rules_row(connection, candidate["rules_version"])
            definition = json.loads(rules["definition_json"])
            threshold = parse_decimal(definition["coverage_threshold"], "coverage_threshold")
            if parse_decimal(candidate["coverage"], "coverage") < threshold:
                raise ConflictError("候选版本未达到规定覆盖度")
            lock = connection.execute(
                "SELECT * FROM stat_release_locks WHERE lock_name=?", (RELEASE_LOCK,)).fetchone()
            if lock is None or lock["holder"] != actor_id:
                raise PermissionDenied("发布前必须持有发布锁")
            snapshot = json.loads(candidate["snapshot_json"])
            run_ids = [item["run_id"] for item in snapshot["datasets"]]
            if actor_id in self._run_approvers(connection, run_ids):
                raise PermissionDenied("批准豁免的人不得发布同一版本")
            current = connection.execute(
                "SELECT * FROM stat_releases WHERE report_period=? AND status='published' "
                "ORDER BY sequence DESC LIMIT 1", (candidate["report_period"],)).fetchone()
            old_internal = None
            if correction_of is None:
                if current is not None:
                    raise ConflictError("公开后的更正必须形成新版本并声明 correction_of")
            else:
                target = connection.execute(
                    "SELECT * FROM stat_releases WHERE version_id=?", (correction_of,)).fetchone()
                if target is None:
                    raise NotFoundError("被更正的版本不存在")
                if target["report_period"] != candidate["report_period"]:
                    raise ValidationError("更正必须针对同一报告期")
                if target["status"] != "published" or current is None \
                        or current["version_id"] != correction_of:
                    raise ConflictError("只能更正当前公开版本")
                old_internal = json.loads(current["internal_results_json"])
            datasets, derived, exempted = self._snapshot_inputs(
                connection, snapshot, require_current=True)
            base_results = None
            if snapshot.get("base_release"):
                base_row = connection.execute(
                    "SELECT * FROM stat_releases WHERE version_id=?",
                    (snapshot["base_release"],)).fetchone()
                if base_row is not None:
                    base_results = json.loads(base_row["internal_results_json"])
            try:
                internal = compute_results(
                    period=candidate["report_period"], definition=definition,
                    datasets=datasets, derived=derived, base_results=base_results,
                    exempted=exempted)
                public = compute_results(
                    period=candidate["report_period"], definition=definition,
                    datasets=[item for item in datasets if item["confidentiality"] == "public"],
                    derived=derived, base_results=base_results, exempted=exempted)
            except ComputeError as exc:
                raise ConflictError(str(exc)) from exc
            results_hash = digest({"snapshot_hash": candidate["snapshot_hash"],
                                   "internal": internal, "public": public})
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS seq FROM stat_releases "
                "WHERE report_period=?", (candidate["report_period"],)).fetchone()["seq"]
            if old_internal is not None:
                impact: dict[str, Any] = diff_results(old_internal, internal)
            else:
                impact = {"affected_economies": [], "affected_indicators": [],
                          "affected_charts": [], "affected_conclusions": []}
            if impact_note and str(impact_note).strip():
                impact["note"] = str(impact_note).strip()
            version_id = f"{candidate['report_period']}-v{sequence}"

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO stat_releases(version_id,candidate_id,report_period,sequence,"
                    "snapshot_json,snapshot_hash,internal_results_json,public_results_json,"
                    "results_hash,status,correction_of,impact_json,published_by,published_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'published',?,?,?,?)",
                    (version_id, candidate_id, candidate["report_period"], sequence,
                     candidate["snapshot_json"], candidate["snapshot_hash"],
                     canonical_json(internal), canonical_json(public), results_hash,
                     correction_of, canonical_json(impact), actor_id, self._now()))
                if correction_of is not None:
                    connection.execute(
                        "UPDATE stat_releases SET status='corrected' WHERE version_id=?",
                        (correction_of,))
                connection.execute(
                    "UPDATE stat_candidates SET status='published' WHERE candidate_id=?",
                    (candidate_id,))
                append_event(connection, actor_id=actor_id, action="stat.release.published",
                             resource_type="stat_release", resource_id=version_id,
                             detail={"report_period": candidate["report_period"],
                                     "sequence": sequence, "results_hash": results_hash,
                                     "snapshot_hash": candidate["snapshot_hash"],
                                     "correction_of": correction_of,
                                     "coverage": candidate["coverage"]},
                             occurred_at=self._now())
                return "stat_release", version_id, {
                    "version_id": version_id, "sequence": sequence,
                    "results_hash": results_hash, "coverage": candidate["coverage"],
                    "impact": impact}

            return self._idempotent(connection, request_id=request_id,
                                    action="stat.release.publish", payload=payload, create=create)

    # ---- 版本查询与审计重算 ----

    def _release_dict(self, row, *, internal: bool) -> dict[str, Any]:
        result = {"version_id": row["version_id"], "candidate_id": row["candidate_id"],
                  "report_period": row["report_period"], "sequence": row["sequence"],
                  "status": row["status"], "correction_of": row["correction_of"],
                  "snapshot_hash": row["snapshot_hash"], "results_hash": row["results_hash"],
                  "impact": json.loads(row["impact_json"]) if row["impact_json"] else None,
                  "published_by": row["published_by"], "published_at": row["published_at"],
                  "public_results": json.loads(row["public_results_json"])}
        if internal:
            result["internal_results"] = json.loads(row["internal_results_json"])
        return result

    def list_releases(self, *, actor_id: str,
                      report_period: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *INTERNAL_READERS)
        query = "SELECT * FROM stat_releases"
        parameters: list[Any] = []
        if report_period:
            query += " WHERE report_period=?"
            parameters.append(report_period)
        query += " ORDER BY report_period, sequence"
        return [{"version_id": row["version_id"], "report_period": row["report_period"],
                 "sequence": row["sequence"], "status": row["status"],
                 "correction_of": row["correction_of"], "results_hash": row["results_hash"],
                 "published_by": row["published_by"], "published_at": row["published_at"]}
                for row in connection.execute(query, parameters).fetchall()]

    def get_release(self, *, actor_id: str, version_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *INTERNAL_READERS)
        row = connection.execute(
            "SELECT * FROM stat_releases WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("发布版本不存在")
        return self._release_dict(row, internal=actor["role"] in ("auditor", "admin"))

    def get_snapshot(self, *, actor_id: str, version_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "auditor", "admin")
        row = connection.execute(
            "SELECT * FROM stat_releases WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("发布版本不存在")
        return {"version_id": row["version_id"], "snapshot": json.loads(row["snapshot_json"]),
                "snapshot_hash": row["snapshot_hash"], "results_hash": row["results_hash"]}

    def recompute_indicator(self, *, actor_id: str, version_id: str,
                            indicator_code: str) -> dict[str, Any]:
        """以发布时固定的证据快照确定性重算指标并核对结果摘要。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "auditor", "admin")
        indicator_code = self._identifier(indicator_code, "indicator_code")
        row = connection.execute(
            "SELECT * FROM stat_releases WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("发布版本不存在")
        snapshot = json.loads(row["snapshot_json"])
        rules = self._rules_row(connection, snapshot["rules_version"])
        definition = json.loads(rules["definition_json"])
        datasets, derived, exempted = self._snapshot_inputs(
            connection, snapshot, require_current=False)
        base_results = None
        if snapshot.get("base_release"):
            base_row = connection.execute(
                "SELECT * FROM stat_releases WHERE version_id=?",
                (snapshot["base_release"],)).fetchone()
            if base_row is not None:
                base_results = json.loads(base_row["internal_results_json"])
        internal = compute_results(
            period=snapshot["report_period"], definition=definition,
            datasets=datasets, derived=derived, base_results=base_results, exempted=exempted)
        public = compute_results(
            period=snapshot["report_period"], definition=definition,
            datasets=[item for item in datasets if item["confidentiality"] == "public"],
            derived=derived, base_results=base_results, exempted=exempted)
        recomputed_hash = digest({"snapshot_hash": row["snapshot_hash"],
                                  "internal": internal, "public": public})
        detail = internal["indicators"].get(indicator_code)
        if detail is None:
            raise NotFoundError("该版本不包含此指标")
        return {"version_id": version_id, "indicator_code": indicator_code,
                "match": recomputed_hash == row["results_hash"],
                "stored_hash": row["results_hash"], "recomputed_hash": recomputed_hash,
                "detail": detail}

    def get_public_release(self, report_period: str | None = None) -> dict[str, Any]:
        """公共接口：只返回允许公开的当前版本结果。"""

        connection = self.database.connection
        if report_period:
            row = connection.execute(
                "SELECT * FROM stat_releases WHERE report_period=? AND status='published' "
                "ORDER BY sequence DESC LIMIT 1", (report_period,)).fetchone()
            if row is None:
                raise NotFoundError("该报告期没有公开版本")
            return self._public_view(row)
        rows = connection.execute(
            "SELECT * FROM stat_releases WHERE status='published' "
            "ORDER BY report_period, sequence").fetchall()
        return {"items": [self._public_view(row) for row in rows]}

    def _public_view(self, row) -> dict[str, Any]:
        return {"version_id": row["version_id"], "report_period": row["report_period"],
                "sequence": row["sequence"], "published_at": row["published_at"],
                "results": json.loads(row["public_results_json"])}
