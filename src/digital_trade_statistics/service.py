"""统计资料接收、校验与版本发布的领域服务。

在基础服务（主体、幂等、审计链、SQLite 事务）之上实现：
- 报送机构提交带期间、口径、单位、来源证明和保密级别的数据集，原始报送全部保留；
- 修订必须声明替代对象与原因，自动检查、人工豁免、复核意见分别记录；
- 校验队列与发布锁持久化在 SQLite，服务重启后位置不变；
- 候选版本达到规定覆盖度并由独立人员会签后才能公开；
- 公开后的更正形成新版本并列出受影响的经济体、指标、图表数据和文字结论；
- 审计员可以凭同一证据快照确定性重算任一指标。
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any, Callable

from digital_trade_foundation.audit import append_event, canonical_json, digest
from digital_trade_foundation.clock import Clock, SystemClock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.models import Actor, WriteReceipt
from digital_trade_foundation.storage import Database

from .pipeline import (
    DatasetInput,
    DerivedSpec,
    MetricObs,
    compute_indicator_rows,
    coverage_ratio,
    validate_dataset,
)
from .rules import (
    CURRENCY_PATTERN,
    ClassMapping,
    FxRate,
    RuleSet,
    check_period,
    format_decimal,
    parse_decimal,
    period_key,
    previous_period,
    quantize,
)
from .schema import ensure_schema


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RELEASE_LOCK = "release"
READ_ROLES = frozenset({"admin", "analyst", "reviewer", "auditor",
                        "exemption_approver", "release_approver"})


class StatisticsService:
    """协调统计报送、校验、发布与审计重算的规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_schema(database.connection)
        self._recover_queue()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _replay(self, connection, *, request_id: str, action: str,
                payload: dict[str, Any]) -> WriteReceipt | None:
        """在业务状态检查之前识别幂等重放，保证已完成的请求可安全重试。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != payload_hash:
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

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
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _recover_queue(self) -> None:
        """重启后把中断的 running 运行退回 queued，队列位置保持不变。"""

        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE stat_validation_runs SET status='queued', started_at=NULL "
                "WHERE status='running'")

    def _load_ruleset(self, connection, rule_version: str) -> RuleSet:
        row = connection.execute(
            "SELECT * FROM stat_rule_versions WHERE rule_version=?", (rule_version,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        fx_rates = []
        for rate in connection.execute(
                "SELECT * FROM stat_fx_rates WHERE rule_version=? ORDER BY currency, effective_from",
                (rule_version,)):
            fx_rates.append({
                "currency": rate["currency"],
                "effective_from": rate["effective_from"],
                "effective_to": rate["effective_to"],
                "rate_to_base": rate["rate_to_base"],
            })
        mappings = []
        for mapping in connection.execute(
                "SELECT * FROM stat_class_mappings WHERE rule_version=? "
                "ORDER BY source_category, effective_from", (rule_version,)):
            mappings.append({
                "source_category": mapping["source_category"],
                "target_category": mapping["target_category"],
                "effective_from": mapping["effective_from"],
                "effective_to": mapping["effective_to"],
            })
        return RuleSet(
            rule_version=row["rule_version"],
            base_currency=row["base_currency"],
            allowed_methodologies=frozenset(json.loads(row["allowed_methodologies_json"])),
            expected_economies=tuple(json.loads(row["expected_economies_json"])),
            coverage_threshold=Decimal(row["coverage_threshold"]),
            consistency_tolerance=Decimal(row["consistency_tolerance"]),
            required_cosignatures=int(row["required_cosignatures"]),
            fx_rates=tuple(FxRate(r["currency"], r["effective_from"], r["effective_to"],
                                  Decimal(r["rate_to_base"])) for r in fx_rates),
            mappings=tuple(ClassMapping(m["source_category"], m["target_category"],
                                        m["effective_from"], m["effective_to"]) for m in mappings),
        )

    def _economy_groups(self, connection) -> dict[str, str]:
        return {row["economy_code"]: row["group_code"]
                for row in connection.execute("SELECT economy_code, group_code FROM stat_economies")}

    # ------------------------------------------------------------------
    # 规则与参考资料登记（管理员）
    # ------------------------------------------------------------------

    def register_rule_version(self, *, request_id: str, actor_id: str, rule_version: str,
                              description: str, allowed_methodologies: list[str],
                              expected_economies: list[str], base_currency: str,
                              coverage_threshold: str, consistency_tolerance: str,
                              required_cosignatures: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rule_version": rule_version,
                   "description": description, "allowed_methodologies": allowed_methodologies,
                   "expected_economies": expected_economies, "base_currency": base_currency,
                   "coverage_threshold": coverage_threshold,
                   "consistency_tolerance": consistency_tolerance,
                   "required_cosignatures": required_cosignatures}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rule_version = self._identifier(rule_version, "rule_version")
            description = self._text(description, "description", 500)
            if not isinstance(allowed_methodologies, list) or not allowed_methodologies:
                raise ValidationError("allowed_methodologies 必须是非空列表")
            methodologies = [self._identifier(item, "methodology") for item in allowed_methodologies]
            if not isinstance(expected_economies, list) or not expected_economies:
                raise ValidationError("expected_economies 必须是非空列表")
            economies = [self._identifier(item, "economy_code") for item in expected_economies]
            for economy in economies:
                if connection.execute("SELECT 1 FROM stat_economies WHERE economy_code=?",
                                      (economy,)).fetchone() is None:
                    raise NotFoundError(f"经济体 {economy} 未登记")
            if not CURRENCY_PATTERN.fullmatch(str(base_currency)):
                raise ValidationError("base_currency 必须是三位大写币种代码")
            threshold = parse_decimal(coverage_threshold, "coverage_threshold")
            if threshold < 0 or threshold > 1:
                raise ValidationError("coverage_threshold 必须在 0 与 1 之间")
            tolerance = parse_decimal(consistency_tolerance, "consistency_tolerance")
            if tolerance < 0:
                raise ValidationError("consistency_tolerance 不能为负")
            if not isinstance(required_cosignatures, int) or required_cosignatures < 1:
                raise ValidationError("required_cosignatures 必须是正整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_rule_versions(rule_version,description,"
                        "allowed_methodologies_json,expected_economies_json,base_currency,"
                        "coverage_threshold,consistency_tolerance,required_cosignatures,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rule_version, description, canonical_json(methodologies),
                         canonical_json(economies), base_currency, format_decimal(threshold),
                         format_decimal(tolerance), required_cosignatures, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("规则版本已经存在") from exc
                append_event(connection, actor_id=actor_id, action="statistics.rule.registered",
                             resource_type="rule_version", resource_id=rule_version,
                             detail={"base_currency": base_currency,
                                     "coverage_threshold": format_decimal(threshold)},
                             occurred_at=self._now())
                return "rule_version", rule_version, {"rule_version": rule_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_rule_version", payload=payload, create=create)

    def register_economy(self, *, request_id: str, actor_id: str, economy_code: str,
                         name: str, group_code: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "economy_code": economy_code,
                   "name": name, "group_code": group_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            economy_code = self._identifier(economy_code, "economy_code")
            name = self._text(name, "name")
            group_code = self._identifier(group_code, "group_code")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_economies(economy_code,name,group_code,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (economy_code, name, group_code, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("经济体代码已经存在") from exc
                append_event(connection, actor_id=actor_id, action="statistics.economy.registered",
                             resource_type="economy", resource_id=economy_code,
                             detail={"name": name, "group_code": group_code},
                             occurred_at=self._now())
                return "economy", economy_code, {"economy_code": economy_code}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_economy", payload=payload, create=create)

    def _check_no_overlap(self, connection, table: str, key_column: str, key_value: str,
                          rule_version: str, effective_from: str, effective_to: str) -> None:
        for row in connection.execute(
                f"SELECT effective_from, effective_to FROM {table} "
                f"WHERE rule_version=? AND {key_column}=?", (rule_version, key_value)):
            if period_key(effective_from) <= period_key(row["effective_to"]) and \
                    period_key(effective_to) >= period_key(row["effective_from"]):
                raise ConflictError(f"{key_column}={key_value} 的生效区间与既有记录重叠")

    def register_fx_rate(self, *, request_id: str, actor_id: str, rule_version: str,
                         currency: str, effective_from: str, effective_to: str,
                         rate_to_base: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rule_version": rule_version, "currency": currency,
                   "effective_from": effective_from, "effective_to": effective_to,
                   "rate_to_base": rate_to_base}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rule_version = self._identifier(rule_version, "rule_version")
            if connection.execute("SELECT 1 FROM stat_rule_versions WHERE rule_version=?",
                                  (rule_version,)).fetchone() is None:
                raise NotFoundError("规则版本不存在")
            if not CURRENCY_PATTERN.fullmatch(str(currency)):
                raise ValidationError("currency 必须是三位大写币种代码")
            effective_from = check_period(effective_from)
            effective_to = check_period(effective_to)
            if period_key(effective_from) > period_key(effective_to):
                raise ValidationError("生效区间起点不能晚于终点")
            rate = parse_decimal(rate_to_base, "rate_to_base")
            if rate <= 0:
                raise ValidationError("rate_to_base 必须为正数")
            self._check_no_overlap(connection, "stat_fx_rates", "currency", currency,
                                   rule_version, effective_from, effective_to)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO stat_fx_rates(rule_version,currency,effective_from,effective_to,"
                    "rate_to_base,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (rule_version, currency, effective_from, effective_to,
                     format_decimal(rate), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="statistics.fx.registered",
                             resource_type="fx_rate",
                             resource_id=f"{rule_version}:{currency}:{effective_from}",
                             detail={"rate_to_base": format_decimal(rate),
                                     "effective_to": effective_to},
                             occurred_at=self._now())
                return "fx_rate", f"{rule_version}:{currency}:{effective_from}", {
                    "rule_version": rule_version, "currency": currency}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_fx_rate", payload=payload, create=create)

    def register_class_mapping(self, *, request_id: str, actor_id: str, rule_version: str,
                               source_category: str, target_category: str,
                               effective_from: str, effective_to: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rule_version": rule_version,
                   "source_category": source_category, "target_category": target_category,
                   "effective_from": effective_from, "effective_to": effective_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rule_version = self._identifier(rule_version, "rule_version")
            if connection.execute("SELECT 1 FROM stat_rule_versions WHERE rule_version=?",
                                  (rule_version,)).fetchone() is None:
                raise NotFoundError("规则版本不存在")
            source_category = self._identifier(source_category, "source_category")
            target_category = self._identifier(target_category, "target_category")
            effective_from = check_period(effective_from)
            effective_to = check_period(effective_to)
            if period_key(effective_from) > period_key(effective_to):
                raise ValidationError("生效区间起点不能晚于终点")
            self._check_no_overlap(connection, "stat_class_mappings", "source_category",
                                   source_category, rule_version, effective_from, effective_to)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO stat_class_mappings(rule_version,source_category,target_category,"
                    "effective_from,effective_to,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (rule_version, source_category, target_category, effective_from,
                     effective_to, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="statistics.mapping.registered",
                             resource_type="class_mapping",
                             resource_id=f"{rule_version}:{source_category}:{effective_from}",
                             detail={"target_category": target_category,
                                     "effective_to": effective_to},
                             occurred_at=self._now())
                return "class_mapping", f"{rule_version}:{source_category}:{effective_from}", {
                    "rule_version": rule_version, "source_category": source_category}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_class_mapping", payload=payload, create=create)

    def grant_analyst_scope(self, *, request_id: str, actor_id: str, analyst_id: str,
                            scope_type: str, scope_value: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "analyst_id": analyst_id,
                   "scope_type": scope_type, "scope_value": scope_value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            analyst = self._actor(connection, analyst_id)
            if analyst.role != "analyst":
                raise ValidationError("只能向分析员授权")
            if scope_type != "indicator":
                raise ValidationError("scope_type 目前只支持 indicator")
            scope_value = self._identifier(scope_value, "scope_value")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_analyst_grants(actor_id,scope_type,scope_value,"
                        "granted_by,granted_at) VALUES(?,?,?,?,?)",
                        (analyst_id, scope_type, scope_value, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该授权已经存在") from exc
                append_event(connection, actor_id=actor_id, action="statistics.grant.created",
                             resource_type="analyst_grant",
                             resource_id=f"{analyst_id}:{scope_type}:{scope_value}",
                             detail={"scope_value": scope_value}, occurred_at=self._now())
                return "analyst_grant", f"{analyst_id}:{scope_type}:{scope_value}", {
                    "analyst_id": analyst_id, "scope_value": scope_value}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_analyst_scope", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 数据集报送与修订
    # ------------------------------------------------------------------

    def submit_dataset(self, *, request_id: str, actor_id: str, organization_id: str,
                       economy_code: str, period: str, methodology: str, currency: str,
                       confidentiality: str, source_evidence: str, rule_version: str,
                       observations: list[dict[str, Any]],
                       revision_of: str | None = None,
                       revision_reason: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "economy_code": economy_code, "period": period, "methodology": methodology,
                   "currency": currency, "confidentiality": confidentiality,
                   "source_evidence": source_evidence, "rule_version": rule_version,
                   "observations": observations, "revision_of": revision_of,
                   "revision_reason": revision_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "submitter", "admin")
            replayed = self._replay(connection, request_id=request_id,
                                    action="submit_dataset", payload=payload)
            if replayed is not None:
                return replayed
            organization_id = self._identifier(organization_id, "organization_id")
            if actor.role == "submitter" and actor.organization_id != organization_id:
                raise PermissionDenied("报送员只能处理本机构资料")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("报送机构不存在")
            economy_code = self._identifier(economy_code, "economy_code")
            if connection.execute("SELECT 1 FROM stat_economies WHERE economy_code=?",
                                  (economy_code,)).fetchone() is None:
                raise NotFoundError("经济体未登记")
            period = check_period(period)
            methodology = self._identifier(methodology, "methodology")
            if not CURRENCY_PATTERN.fullmatch(str(currency)):
                raise ValidationError("currency 必须是三位大写币种代码")
            confidentiality = str(confidentiality).strip()
            source_evidence = self._text(source_evidence, "source_evidence", 500)
            rule_version = self._identifier(rule_version, "rule_version")
            if connection.execute("SELECT 1 FROM stat_rule_versions WHERE rule_version=?",
                                  (rule_version,)).fetchone() is None:
                raise NotFoundError("规则版本不存在")
            if not isinstance(observations, list) or not observations:
                raise ValidationError("observations 必须是非空列表")
            for raw in observations:
                if not isinstance(raw, dict) or "category" not in raw or "value" not in raw:
                    raise ValidationError("每条观测必须包含 category 与 value")
            data_payload = {"observations": observations}
            data_hash = digest(data_payload)

            if revision_of is not None:
                base = connection.execute("SELECT * FROM stat_datasets WHERE dataset_id=?",
                                          (revision_of,)).fetchone()
                if base is None:
                    raise NotFoundError("被替代的报送不存在")
                if base["organization_id"] != organization_id:
                    raise PermissionDenied("只能修订本机构的报送")
                if connection.execute("SELECT 1 FROM stat_datasets WHERE revision_of=?",
                                      (revision_of,)).fetchone() is not None:
                    raise ConflictError("被替代的报送已有后续修订，必须基于最新版本修订")
                if (base["economy_code"], base["period"], base["methodology"]) != \
                        (economy_code, period, methodology):
                    raise ValidationError("修订必须保持相同的经济体、期间与口径")
                if not revision_reason or not str(revision_reason).strip():
                    raise ValidationError("修订必须声明替代原因")
                revision_reason = self._text(revision_reason, "revision_reason", 500)
            else:
                revision_reason = None
                existing = connection.execute(
                    "SELECT 1 FROM stat_datasets d WHERE d.economy_code=? AND d.period=? "
                    "AND d.methodology=? AND NOT EXISTS "
                    "(SELECT 1 FROM stat_datasets n WHERE n.revision_of=d.dataset_id)",
                    (economy_code, period, methodology)).fetchone()
                if existing is not None:
                    raise ConflictError("该经济体、期间与口径已有有效报送，请使用修订")

            def create() -> tuple[str, str, dict[str, Any]]:
                dataset_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stat_datasets(dataset_id,organization_id,economy_code,period,"
                    "methodology,currency,confidentiality,source_evidence,rule_version,"
                    "revision_of,revision_reason,payload_json,payload_hash,submitted_by,"
                    "submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dataset_id, organization_id, economy_code, period, methodology, currency,
                     confidentiality, source_evidence, rule_version, revision_of, revision_reason,
                     canonical_json(data_payload), data_hash, actor_id, self._now()),
                )
                position = connection.execute(
                    "SELECT COALESCE(MAX(queue_position), 0) + 1 AS next_pos "
                    "FROM stat_validation_runs").fetchone()["next_pos"]
                run_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stat_validation_runs(run_id,dataset_id,rule_version,"
                    "queue_position,status,attempt,enqueued_at) VALUES(?,?,?,?, 'queued', 0, ?)",
                    (run_id, dataset_id, rule_version, position, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="statistics.dataset.submitted",
                             resource_type="dataset", resource_id=dataset_id,
                             detail={"organization_id": organization_id,
                                     "economy_code": economy_code, "period": period,
                                     "methodology": methodology, "rule_version": rule_version,
                                     "revision_of": revision_of, "payload_hash": data_hash,
                                     "queue_position": position},
                             occurred_at=self._now())
                return "dataset", dataset_id, {"dataset_id": dataset_id, "run_id": run_id,
                                               "queue_position": position}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_dataset", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 校验队列
    # ------------------------------------------------------------------

    def _previous_values(self, connection, dataset) -> dict[str, Decimal]:
        """上一期间同一经济体有效报送中各指标的基准币种值。"""

        previous = previous_period(dataset["period"])
        rows = connection.execute(
            "SELECT * FROM stat_datasets d WHERE d.economy_code=? AND d.period=? "
            "AND NOT EXISTS (SELECT 1 FROM stat_datasets n WHERE n.revision_of=d.dataset_id) "
            "ORDER BY d.submitted_at, d.dataset_id",
            (dataset["economy_code"], previous)).fetchall()
        values: dict[str, Decimal] = {}
        for row in rows:
            run = connection.execute(
                "SELECT * FROM stat_validation_runs WHERE dataset_id=? AND status='completed' "
                "ORDER BY queue_position DESC LIMIT 1", (row["dataset_id"],)).fetchone()
            if run is None:
                continue
            for obs in connection.execute(
                    "SELECT indicator, value_base FROM stat_normalized_observations "
                    "WHERE run_id=? AND gap=0", (run["run_id"],)):
                values[obs["indicator"]] = values.get(obs["indicator"], Decimal(0)) + \
                    Decimal(obs["value_base"])
        return values

    def _process_run(self, connection, run) -> str:
        """在事务内处理一个校验运行。"""

        dataset = connection.execute("SELECT * FROM stat_datasets WHERE dataset_id=?",
                                     (run["dataset_id"],)).fetchone()
        if dataset is None:
            raise NotFoundError("数据集不存在")
        rule = self._load_ruleset(connection, run["rule_version"])
        payload = json.loads(dataset["payload_json"])
        result_findings, result_observations = validate_dataset(
            DatasetInput(
                economy_code=dataset["economy_code"],
                period=dataset["period"],
                methodology=dataset["methodology"],
                currency=dataset["currency"],
                confidentiality=dataset["confidentiality"],
                source_evidence=dataset["source_evidence"],
                observations=payload.get("observations"),
            ),
            rule,
            self._previous_values(connection, dataset),
        )
        for finding in result_findings:
            connection.execute(
                "INSERT INTO stat_validation_findings(finding_id,run_id,check_type,severity,"
                "code,message,context_json) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, run["run_id"], finding.check_type, finding.severity,
                 finding.code, finding.message, canonical_json(finding.context)),
            )
        for index, obs in enumerate(result_observations):
            connection.execute(
                "INSERT INTO stat_normalized_observations(run_id,obs_index,dataset_id,"
                "economy_code,period,methodology,indicator,value_base,gap,confidentiality) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run["run_id"], index, dataset["dataset_id"], dataset["economy_code"],
                 dataset["period"], dataset["methodology"], obs.indicator,
                 format_decimal(obs.value_base), 1 if obs.gap else 0,
                 dataset["confidentiality"]),
            )
        errors = sum(1 for finding in result_findings if finding.severity == "error")
        warnings = len(result_findings) - errors
        connection.execute(
            "UPDATE stat_validation_runs SET status='completed', finished_at=? WHERE run_id=?",
            (self._now(), run["run_id"]))
        append_event(connection, actor_id="system", action="statistics.validation.completed",
                     resource_type="validation_run", resource_id=run["run_id"],
                     detail={"dataset_id": dataset["dataset_id"], "errors": errors,
                             "warnings": warnings, "observations": len(result_observations)},
                     occurred_at=self._now())
        return run["run_id"]

    def process_validation_queue(self, *, actor_id: str,
                                 limit: int | None = None) -> dict[str, Any]:
        """按队列位置依次处理待校验运行；失败项标记后跳过。"""

        processed: list[str] = []
        failed: list[str] = []
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "analyst")
        while limit is None or len(processed) + len(failed) < limit:
            with self.database.transaction(immediate=True) as connection:
                run = connection.execute(
                    "SELECT * FROM stat_validation_runs WHERE status='queued' "
                    "ORDER BY queue_position LIMIT 1").fetchone()
                if run is None:
                    break
                connection.execute(
                    "UPDATE stat_validation_runs SET status='running', attempt=attempt+1, "
                    "started_at=? WHERE run_id=?", (self._now(), run["run_id"]))
            try:
                with self.database.transaction(immediate=True) as connection:
                    processed.append(self._process_run(connection, run))
            except Exception as exc:  # 校验管线本身不应对数据问题抛异常
                with self.database.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE stat_validation_runs SET status='failed', finished_at=?, "
                        "error_message=? WHERE run_id=?",
                        (self._now(), str(exc)[:500], run["run_id"]))
                    append_event(connection, actor_id="system",
                                 action="statistics.validation.failed",
                                 resource_type="validation_run", resource_id=run["run_id"],
                                 detail={"error": str(exc)[:500]}, occurred_at=self._now())
                failed.append(run["run_id"])
        return {"processed": processed, "failed": failed}

    # ------------------------------------------------------------------
    # 查询（按角色限定范围）
    # ------------------------------------------------------------------

    def _reader(self, actor_id: str) -> Actor:
        actor = self._actor(self.database.connection, actor_id)
        if actor.role not in READ_ROLES and actor.role != "submitter":
            raise PermissionDenied("当前角色不能查询统计资料")
        return actor

    def list_datasets(self, *, actor_id: str, organization_id: str | None = None,
                      economy_code: str | None = None,
                      period: str | None = None) -> list[dict[str, Any]]:
        actor = self._reader(actor_id)
        query = "SELECT * FROM stat_datasets"
        conditions: list[str] = []
        parameters: list[Any] = []
        if actor.role == "submitter":
            conditions.append("organization_id=?")
            parameters.append(actor.organization_id)
        elif organization_id:
            conditions.append("organization_id=?")
            parameters.append(organization_id)
        if economy_code:
            conditions.append("economy_code=?")
            parameters.append(economy_code)
        if period:
            conditions.append("period=?")
            parameters.append(period)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY submitted_at, dataset_id"
        items = []
        for row in self.database.connection.execute(query, parameters):
            items.append(self._dataset_dict(row))
        return items

    def _dataset_dict(self, row) -> dict[str, Any]:
        return {"dataset_id": row["dataset_id"], "organization_id": row["organization_id"],
                "economy_code": row["economy_code"], "period": row["period"],
                "methodology": row["methodology"], "currency": row["currency"],
                "confidentiality": row["confidentiality"],
                "source_evidence": row["source_evidence"],
                "rule_version": row["rule_version"], "revision_of": row["revision_of"],
                "revision_reason": row["revision_reason"],
                "payload": json.loads(row["payload_json"]),
                "payload_hash": row["payload_hash"], "submitted_by": row["submitted_by"],
                "submitted_at": row["submitted_at"]}

    def _check_dataset_visible(self, actor: Actor, dataset_id: str) -> None:
        row = self.database.connection.execute(
            "SELECT organization_id FROM stat_datasets WHERE dataset_id=?",
            (dataset_id,)).fetchone()
        if row is None:
            raise NotFoundError("数据集不存在")
        if actor.role == "submitter" and row["organization_id"] != actor.organization_id:
            raise PermissionDenied("报送员只能查看本机构资料")

    def list_validation_runs(self, *, actor_id: str,
                             dataset_id: str | None = None) -> list[dict[str, Any]]:
        actor = self._reader(actor_id)
        query = "SELECT * FROM stat_validation_runs"
        parameters: list[Any] = []
        if dataset_id:
            self._check_dataset_visible(actor, dataset_id)
            query += " WHERE dataset_id=?"
            parameters.append(dataset_id)
        elif actor.role == "submitter":
            query += (" WHERE dataset_id IN (SELECT dataset_id FROM stat_datasets "
                      "WHERE organization_id=?)")
            parameters.append(actor.organization_id)
        query += " ORDER BY queue_position"
        return [{"run_id": row["run_id"], "dataset_id": row["dataset_id"],
                 "rule_version": row["rule_version"], "queue_position": row["queue_position"],
                 "status": row["status"], "attempt": row["attempt"],
                 "enqueued_at": row["enqueued_at"], "started_at": row["started_at"],
                 "finished_at": row["finished_at"], "error_message": row["error_message"]}
                for row in self.database.connection.execute(query, parameters)]

    def list_findings(self, *, actor_id: str, run_id: str) -> list[dict[str, Any]]:
        actor = self._reader(actor_id)
        run = self.database.connection.execute(
            "SELECT dataset_id FROM stat_validation_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise NotFoundError("校验运行不存在")
        self._check_dataset_visible(actor, run["dataset_id"])
        items = []
        for row in self.database.connection.execute(
                "SELECT f.*, e.exemption_id, e.approved_by, e.reason AS exemption_reason "
                "FROM stat_validation_findings f "
                "LEFT JOIN stat_exemptions e ON e.finding_id=f.finding_id "
                "WHERE f.run_id=? ORDER BY f.rowid", (run_id,)):
            items.append({"finding_id": row["finding_id"], "run_id": row["run_id"],
                          "check_type": row["check_type"], "severity": row["severity"],
                          "code": row["code"], "message": row["message"],
                          "context": json.loads(row["context_json"]),
                          "exemption": None if row["exemption_id"] is None else {
                              "exemption_id": row["exemption_id"],
                              "approved_by": row["approved_by"],
                              "reason": row["exemption_reason"]}})
        return items

    # ------------------------------------------------------------------
    # 人工豁免与复核意见（与自动检查分别记录）
    # ------------------------------------------------------------------

    def approve_exemption(self, *, request_id: str, actor_id: str, finding_id: str,
                          reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "finding_id": finding_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "exemption_approver", "admin")
            finding = connection.execute(
                "SELECT * FROM stat_validation_findings WHERE finding_id=?",
                (finding_id,)).fetchone()
            if finding is None:
                raise NotFoundError("发现项不存在")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                exemption_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO stat_exemptions(exemption_id,finding_id,reason,approved_by,"
                        "approved_at) VALUES(?,?,?,?,?)",
                        (exemption_id, finding_id, reason, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该发现项已有豁免") from exc
                append_event(connection, actor_id=actor_id,
                             action="statistics.exemption.approved",
                             resource_type="exemption", resource_id=exemption_id,
                             detail={"finding_id": finding_id, "code": finding["code"],
                                     "run_id": finding["run_id"]},
                             occurred_at=self._now())
                return "exemption", exemption_id, {"exemption_id": exemption_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_exemption", payload=payload, create=create)

    def add_review(self, *, request_id: str, actor_id: str, target_type: str,
                   target_id: str, opinion: str, comment: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "target_type": target_type, "target_id": target_id,
                   "opinion": opinion, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            if target_type not in ("dataset", "candidate"):
                raise ValidationError("target_type 必须是 dataset 或 candidate")
            table = "stat_datasets" if target_type == "dataset" else "stat_candidates"
            key = "dataset_id" if target_type == "dataset" else "candidate_id"
            if connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?",
                                  (target_id,)).fetchone() is None:
                raise NotFoundError("复核对象不存在")
            if opinion not in ("approve", "reject", "comment"):
                raise ValidationError("opinion 必须是 approve、reject 或 comment")
            comment = self._text(comment, "comment", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stat_reviews(review_id,target_type,target_id,opinion,comment,"
                    "reviewer_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (review_id, target_type, target_id, opinion, comment, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="statistics.review.added",
                             resource_type="review", resource_id=review_id,
                             detail={"target_type": target_type, "target_id": target_id,
                                     "opinion": opinion},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_review", payload=payload, create=create)

    def list_reviews(self, *, actor_id: str, target_type: str,
                     target_id: str) -> list[dict[str, Any]]:
        self._reader(actor_id)
        return [{"review_id": row["review_id"], "target_type": row["target_type"],
                 "target_id": row["target_id"], "opinion": row["opinion"],
                 "comment": row["comment"], "reviewer_id": row["reviewer_id"],
                 "created_at": row["created_at"]}
                for row in self.database.connection.execute(
                    "SELECT * FROM stat_reviews WHERE target_type=? AND target_id=? "
                    "ORDER BY created_at, review_id", (target_type, target_id))]

    # ------------------------------------------------------------------
    # 候选版本、会签与发布
    # ------------------------------------------------------------------

    def create_candidate(self, *, request_id: str, actor_id: str, period: str,
                         rule_version: str, label: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "period": period,
                   "rule_version": rule_version, "label": label}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "analyst", "admin")
            replayed = self._replay(connection, request_id=request_id,
                                    action="create_candidate", payload=payload)
            if replayed is not None:
                return replayed
            period = check_period(period)
            rule_version = self._identifier(rule_version, "rule_version")
            if connection.execute("SELECT 1 FROM stat_rule_versions WHERE rule_version=?",
                                  (rule_version,)).fetchone() is None:
                raise NotFoundError("规则版本不存在")
            label = self._text(label, "label", 200)
            if connection.execute(
                    "SELECT 1 FROM stat_candidates WHERE period=? AND rule_version=? "
                    "AND status='open'", (period, rule_version)).fetchone() is not None:
                raise ConflictError("该期间与规则版本已有开放的候选版本")

            def create() -> tuple[str, str, dict[str, Any]]:
                candidate_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stat_candidates(candidate_id,period,rule_version,label,status,"
                    "created_by,created_at) VALUES(?,?,?,?, 'open', ?, ?)",
                    (candidate_id, period, rule_version, label, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="statistics.candidate.created",
                             resource_type="candidate", resource_id=candidate_id,
                             detail={"period": period, "rule_version": rule_version,
                                     "label": label},
                             occurred_at=self._now())
                return "candidate", candidate_id, {"candidate_id": candidate_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_candidate", payload=payload, create=create)

    def _effective_datasets(self, connection, period: str) -> list[Any]:
        """某期间内每个（经济体 × 口径）的最新未替代报送。"""

        return connection.execute(
            "SELECT * FROM stat_datasets d WHERE d.period=? AND NOT EXISTS "
            "(SELECT 1 FROM stat_datasets n WHERE n.revision_of=d.dataset_id) "
            "ORDER BY d.submitted_at, d.dataset_id", (period,)).fetchall()

    def _eligible_inputs(self, connection, candidate) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """候选版本的可用报送与排除原因。"""

        eligible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for dataset in self._effective_datasets(connection, candidate["period"]):
            run = connection.execute(
                "SELECT * FROM stat_validation_runs WHERE dataset_id=? AND rule_version=? "
                "AND status='completed' ORDER BY queue_position DESC LIMIT 1",
                (dataset["dataset_id"], candidate["rule_version"])).fetchone()
            if run is None:
                excluded.append({"dataset_id": dataset["dataset_id"],
                                 "reason": "没有按候选规则版本完成的校验"})
                continue
            unexempted = connection.execute(
                "SELECT COUNT(*) AS count FROM stat_validation_findings f "
                "WHERE f.run_id=? AND f.severity='error' AND NOT EXISTS "
                "(SELECT 1 FROM stat_exemptions e WHERE e.finding_id=f.finding_id)",
                (run["run_id"],)).fetchone()["count"]
            if unexempted:
                excluded.append({"dataset_id": dataset["dataset_id"],
                                 "reason": f"存在 {unexempted} 条未豁免的错误发现项"})
                continue
            observations = connection.execute(
                "SELECT * FROM stat_normalized_observations WHERE run_id=? ORDER BY obs_index",
                (run["run_id"],)).fetchall()
            eligible.append({"dataset": dataset, "run": run, "observations": observations})
        return eligible, excluded

    def _candidate_state(self, connection, candidate) -> dict[str, Any]:
        rule = self._load_ruleset(connection, candidate["rule_version"])
        eligible, excluded = self._eligible_inputs(connection, candidate)
        expected_indicators = rule.expected_indicators(candidate["period"])
        filled = set()
        for item in eligible:
            for obs in item["observations"]:
                if not obs["gap"]:
                    filled.add((obs["economy_code"], obs["indicator"]))
        expected_cells = {(economy, indicator)
                          for economy in rule.expected_economies
                          for indicator in expected_indicators}
        coverage = coverage_ratio(rule.expected_economies, expected_indicators,
                                  filled & expected_cells)
        cosigners = [row["actor_id"] for row in connection.execute(
            "SELECT actor_id FROM stat_cosignatures WHERE candidate_id=? ORDER BY signed_at",
            (candidate["candidate_id"],))]
        exemption_approvers = [row["approved_by"] for row in connection.execute(
            "SELECT DISTINCT e.approved_by FROM stat_exemptions e "
            "JOIN stat_validation_findings f ON e.finding_id=f.finding_id "
            "JOIN stat_validation_runs r ON f.run_id=r.run_id "
            "JOIN stat_datasets d ON r.dataset_id=d.dataset_id "
            "WHERE d.period=? AND r.rule_version=?",
            (candidate["period"], candidate["rule_version"]))]
        return {"rule": rule, "eligible": eligible, "excluded": excluded,
                "coverage": coverage, "filled_cells": filled,
                "expected_cells": expected_cells, "cosigners": cosigners,
                "exemption_approvers": exemption_approvers}

    def candidate_report(self, *, actor_id: str, candidate_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        with self.database.transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM stat_candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选版本不存在")
            state = self._candidate_state(connection, candidate)
            missing = sorted(state["expected_cells"] - state["filled_cells"])
            blockers = self._publish_blockers(candidate, state, publisher_id=None)
            return {"candidate_id": candidate_id, "period": candidate["period"],
                    "rule_version": candidate["rule_version"], "label": candidate["label"],
                    "status": candidate["status"], "created_by": candidate["created_by"],
                    "coverage": format_decimal(state["coverage"]),
                    "coverage_threshold": format_decimal(state["rule"].coverage_threshold),
                    "coverage_met": state["coverage"] >= state["rule"].coverage_threshold,
                    "eligible_datasets": [item["dataset"]["dataset_id"] for item in state["eligible"]],
                    "excluded_datasets": state["excluded"],
                    "missing_cells": [{"economy_code": economy, "indicator": indicator}
                                      for economy, indicator in missing],
                    "cosigners": state["cosigners"],
                    "required_cosignatures": state["rule"].required_cosignatures,
                    "exemption_approvers": state["exemption_approvers"],
                    "publish_blockers": blockers}

    def _publish_blockers(self, candidate, state, publisher_id: str | None) -> list[str]:
        blockers: list[str] = []
        if candidate["status"] != "open":
            blockers.append("候选版本不在开放状态")
        if state["coverage"] < state["rule"].coverage_threshold:
            blockers.append("覆盖度未达到规定阈值")
        if len(state["cosigners"]) < state["rule"].required_cosignatures:
            blockers.append("会签人数不足")
        if publisher_id is not None:
            if publisher_id == candidate["created_by"]:
                blockers.append("发布人不能是候选版本创建人")
            if publisher_id in state["cosigners"]:
                blockers.append("发布人不能是已会签人员")
            if publisher_id in state["exemption_approvers"]:
                blockers.append("批准豁免的人不得发布同一版本")
        return blockers

    def cosign_candidate(self, *, request_id: str, actor_id: str,
                         candidate_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "candidate_id": candidate_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "release_approver", "admin")
            replayed = self._replay(connection, request_id=request_id,
                                    action="cosign_candidate", payload=payload)
            if replayed is not None:
                return replayed
            candidate = connection.execute(
                "SELECT * FROM stat_candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选版本不存在")
            if candidate["status"] != "open":
                raise ConflictError("候选版本不在开放状态")
            if candidate["created_by"] == actor_id:
                raise PermissionDenied("候选版本创建人不能会签")
            state = self._candidate_state(connection, candidate)
            if actor_id in state["exemption_approvers"]:
                raise PermissionDenied("批准过本版本豁免的人不能会签")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stat_cosignatures(candidate_id,actor_id,signed_at) "
                        "VALUES(?,?,?)",
                        (candidate_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该人员已经会签") from exc
                append_event(connection, actor_id=actor_id,
                             action="statistics.candidate.cosigned",
                             resource_type="candidate", resource_id=candidate_id,
                             detail={"cosigner": actor_id}, occurred_at=self._now())
                return "candidate", candidate_id, {"candidate_id": candidate_id,
                                                   "cosigner": actor_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="cosign_candidate", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 发布锁（持久化，重启后保持不变）
    # ------------------------------------------------------------------

    def acquire_release_lock(self, *, actor_id: str, candidate_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "release_approver", "admin")
            candidate = connection.execute(
                "SELECT * FROM stat_candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选版本不存在")
            if candidate["status"] != "open":
                raise ConflictError("候选版本不在开放状态")
            existing = connection.execute(
                "SELECT * FROM stat_locks WHERE lock_name=?", (RELEASE_LOCK,)).fetchone()
            if existing is not None:
                if existing["holder"] == actor_id and existing["resource_id"] == candidate_id:
                    return {"lock_name": RELEASE_LOCK, "holder": actor_id,
                            "resource_id": candidate_id, "replayed": True}
                raise ConflictError("发布锁已被占用")
            connection.execute(
                "INSERT INTO stat_locks(lock_name,holder,resource_id,acquired_at) VALUES(?,?,?,?)",
                (RELEASE_LOCK, actor_id, candidate_id, self._now()))
            append_event(connection, actor_id=actor_id, action="statistics.lock.acquired",
                         resource_type="release_lock", resource_id=candidate_id,
                         detail={"holder": actor_id}, occurred_at=self._now())
            return {"lock_name": RELEASE_LOCK, "holder": actor_id,
                    "resource_id": candidate_id, "replayed": False}

    def release_release_lock(self, *, actor_id: str, candidate_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "release_approver", "admin")
            existing = connection.execute(
                "SELECT * FROM stat_locks WHERE lock_name=?", (RELEASE_LOCK,)).fetchone()
            if existing is None or existing["resource_id"] != candidate_id:
                raise NotFoundError("该候选版本没有持有发布锁")
            if existing["holder"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能由持锁人或管理员释放发布锁")
            connection.execute("DELETE FROM stat_locks WHERE lock_name=?", (RELEASE_LOCK,))
            append_event(connection, actor_id=actor_id, action="statistics.lock.released",
                         resource_type="release_lock", resource_id=candidate_id,
                         detail={"holder": existing["holder"]}, occurred_at=self._now())
            return {"lock_name": RELEASE_LOCK, "released": True}

    def lock_state(self, *, actor_id: str) -> dict[str, Any] | None:
        self._reader(actor_id)
        row = self.database.connection.execute(
            "SELECT * FROM stat_locks WHERE lock_name=?", (RELEASE_LOCK,)).fetchone()
        if row is None:
            return None
        return {"lock_name": row["lock_name"], "holder": row["holder"],
                "resource_id": row["resource_id"], "acquired_at": row["acquired_at"]}

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    def _derived_specs(self, connection) -> list[DerivedSpec]:
        specs = []
        for row in connection.execute(
                "SELECT * FROM stat_derived_indicators ORDER BY created_at, indicator_id"):
            specs.append(DerivedSpec(row["indicator_id"], row["code"],
                                     json.loads(row["formula_json"])))
        return specs

    def publish_release(self, *, request_id: str, actor_id: str,
                        candidate_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "candidate_id": candidate_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "release_approver", "admin")
            replayed = self._replay(connection, request_id=request_id,
                                    action="publish_release", payload=payload)
            if replayed is not None:
                return replayed
            candidate = connection.execute(
                "SELECT * FROM stat_candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()
            if candidate is None:
                raise NotFoundError("候选版本不存在")
            state = self._candidate_state(connection, candidate)
            blockers = self._publish_blockers(candidate, state, publisher_id=actor_id)
            lock = connection.execute("SELECT * FROM stat_locks WHERE lock_name=?",
                                      (RELEASE_LOCK,)).fetchone()
            if lock is None or lock["resource_id"] != candidate_id or lock["holder"] != actor_id:
                blockers.append("发布人必须持有该候选版本的发布锁")
            if blockers:
                raise ConflictError("发布条件未满足：" + "；".join(blockers))

            def create() -> tuple[str, str, dict[str, Any]]:
                groups = self._economy_groups(connection)
                observations = [
                    MetricObs(economy=obs["economy_code"], indicator=obs["indicator"],
                              value=Decimal(obs["value_base"]), gap=bool(obs["gap"]),
                              public=item["dataset"]["confidentiality"] == "public")
                    for item in state["eligible"] for obs in item["observations"]
                ]
                derived = self._derived_specs(connection)
                rows_by_indicator = compute_indicator_rows(observations, groups, derived)
                version_row = connection.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version "
                    "FROM stat_releases").fetchone()
                release_id = uuid.uuid4().hex
                version_number = version_row["next_version"]
                connection.execute(
                    "INSERT INTO stat_releases(release_id,candidate_id,version_number,period,"
                    "rule_version,corrects_release_id,status,published_by,published_at) "
                    "VALUES(?,?,?,?,?,?, 'published', ?, ?)",
                    (release_id, candidate_id, version_number, candidate["period"],
                     candidate["rule_version"], candidate["correction_of_release_id"],
                     actor_id, self._now()),
                )
                for item in state["eligible"]:
                    connection.execute(
                        "INSERT INTO stat_release_inputs(release_id,dataset_id,payload_hash,"
                        "run_id) VALUES(?,?,?,?)",
                        (release_id, item["dataset"]["dataset_id"],
                         item["dataset"]["payload_hash"], item["run"]["run_id"]),
                    )
                for spec in derived:
                    connection.execute(
                        "INSERT INTO stat_release_derived(release_id,indicator_id,formula_hash) "
                        "VALUES(?,?,?)",
                        (release_id, spec.indicator_id, digest(spec.formula)),
                    )
                for indicator, rows in rows_by_indicator.items():
                    for row in rows:
                        connection.execute(
                            "INSERT INTO stat_release_metrics(release_id,indicator,scope,value,"
                            "gap,all_public) VALUES(?,?,?,?,?,?)",
                            (release_id, indicator, row.scope, format_decimal(row.value),
                             1 if row.gap else 0, 1 if row.all_public else 0),
                        )
                connection.execute(
                    "UPDATE stat_candidates SET status='published', coverage=? "
                    "WHERE candidate_id=?",
                    (format_decimal(state["coverage"]), candidate_id))
                if candidate["correction_of_release_id"]:
                    connection.execute(
                        "UPDATE stat_releases SET status='superseded' WHERE release_id=?",
                        (candidate["correction_of_release_id"],))
                    connection.execute(
                        "UPDATE stat_corrections SET new_release_id=? WHERE candidate_id=?",
                        (release_id, candidate_id))
                connection.execute("DELETE FROM stat_locks WHERE lock_name=?", (RELEASE_LOCK,))
                append_event(connection, actor_id=actor_id, action="statistics.release.published",
                             resource_type="release", resource_id=release_id,
                             detail={"candidate_id": candidate_id,
                                     "version_number": version_number,
                                     "period": candidate["period"],
                                     "coverage": format_decimal(state["coverage"]),
                                     "datasets": len(state["eligible"]),
                                     "indicators": sorted(rows_by_indicator.keys())},
                             occurred_at=self._now())
                return "release", release_id, {"release_id": release_id,
                                               "version_number": version_number}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_release", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 公开后更正
    # ------------------------------------------------------------------

    def create_correction(self, *, request_id: str, actor_id: str, release_id: str,
                          reason: str, affected_economies: list[str],
                          affected_indicators: list[str], affected_charts: list[str],
                          affected_conclusions: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "release_id": release_id, "reason": reason,
                   "affected_economies": affected_economies,
                   "affected_indicators": affected_indicators,
                   "affected_charts": affected_charts,
                   "affected_conclusions": affected_conclusions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "release_approver", "admin")
            replayed = self._replay(connection, request_id=request_id,
                                    action="create_correction", payload=payload)
            if replayed is not None:
                return replayed
            base = connection.execute("SELECT * FROM stat_releases WHERE release_id=?",
                                      (release_id,)).fetchone()
            if base is None:
                raise NotFoundError("被更正的版本不存在")
            if base["status"] != "published":
                raise ConflictError("只能更正当前公开版本")
            reason = self._text(reason, "reason", 500)
            for field, values in (("affected_economies", affected_economies),
                                  ("affected_indicators", affected_indicators),
                                  ("affected_charts", affected_charts),
                                  ("affected_conclusions", affected_conclusions)):
                if not isinstance(values, list) or not values:
                    raise ValidationError(f"{field} 必须是非空列表")
            if connection.execute(
                    "SELECT 1 FROM stat_candidates WHERE period=? AND rule_version=? "
                    "AND status='open'", (base["period"], base["rule_version"])).fetchone():
                raise ConflictError("该期间与规则版本已有开放的候选版本")

            def create() -> tuple[str, str, dict[str, Any]]:
                candidate_id = uuid.uuid4().hex
                correction_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stat_candidates(candidate_id,period,rule_version,label,status,"
                    "correction_of_release_id,created_by,created_at) "
                    "VALUES(?,?,?,?, 'open', ?, ?, ?)",
                    (candidate_id, base["period"], base["rule_version"],
                     f"更正版本（基于 v{base['version_number']}）", release_id,
                     actor_id, self._now()),
                )
                connection.execute(
                    "INSERT INTO stat_corrections(correction_id,base_release_id,candidate_id,"
                    "reason,affected_economies_json,affected_indicators_json,"
                    "affected_charts_json,affected_conclusions_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (correction_id, release_id, candidate_id, reason,
                     canonical_json(affected_economies), canonical_json(affected_indicators),
                     canonical_json(affected_charts), canonical_json(affected_conclusions),
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="statistics.correction.created",
                             resource_type="correction", resource_id=correction_id,
                             detail={"base_release_id": release_id,
                                     "candidate_id": candidate_id,
                                     "affected_economies": affected_economies,
                                     "affected_indicators": affected_indicators},
                             occurred_at=self._now())
                return "correction", correction_id, {"correction_id": correction_id,
                                                     "candidate_id": candidate_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_correction", payload=payload, create=create)

    def list_corrections(self, *, actor_id: str,
                         release_id: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id)
        query = "SELECT * FROM stat_corrections"
        parameters: list[Any] = []
        if release_id:
            query += " WHERE base_release_id=? OR new_release_id=?"
            parameters.extend([release_id, release_id])
        query += " ORDER BY created_at, correction_id"
        return [{"correction_id": row["correction_id"],
                 "base_release_id": row["base_release_id"],
                 "candidate_id": row["candidate_id"],
                 "new_release_id": row["new_release_id"], "reason": row["reason"],
                 "affected_economies": json.loads(row["affected_economies_json"]),
                 "affected_indicators": json.loads(row["affected_indicators_json"]),
                 "affected_charts": json.loads(row["affected_charts_json"]),
                 "affected_conclusions": json.loads(row["affected_conclusions_json"]),
                 "created_by": row["created_by"], "created_at": row["created_at"]}
                for row in self.database.connection.execute(query, parameters)]

    # ------------------------------------------------------------------
    # 派生指标
    # ------------------------------------------------------------------

    def create_derived_indicator(self, *, request_id: str, actor_id: str, code: str,
                                 name: str, formula: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "code": code, "name": name, "formula": formula}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "analyst", "admin")
            code = self._identifier(code, "code")
            name = self._text(name, "name", 200)
            if not isinstance(formula, dict):
                raise ValidationError("formula 必须是对象")
            op = formula.get("op")
            if op == "sum":
                inputs = formula.get("inputs")
                if not isinstance(inputs, list) or len(inputs) < 2:
                    raise ValidationError("sum 公式需要至少两个输入指标")
            elif op == "ratio":
                inputs = [formula.get("numerator"), formula.get("denominator")]
                if not all(inputs):
                    raise ValidationError("ratio 公式需要 numerator 与 denominator")
            else:
                raise ValidationError("formula.op 必须是 sum 或 ratio")
            inputs = [self._identifier(item, "indicator") for item in inputs]
            if actor.role != "admin":
                for indicator in inputs:
                    if connection.execute(
                            "SELECT 1 FROM stat_analyst_grants WHERE actor_id=? AND "
                            "scope_type='indicator' AND scope_value=?",
                            (actor_id, indicator)).fetchone() is None:
                        raise PermissionDenied(f"分析员未获得指标 {indicator} 的授权")

            def create() -> tuple[str, str, dict[str, Any]]:
                indicator_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO stat_derived_indicators(indicator_id,code,name,formula_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (indicator_id, code, name, canonical_json(formula),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("派生指标代码已经存在") from exc
                append_event(connection, actor_id=actor_id, action="statistics.derived.created",
                             resource_type="derived_indicator", resource_id=indicator_id,
                             detail={"code": code, "formula": formula},
                             occurred_at=self._now())
                return "derived_indicator", indicator_id, {"indicator_id": indicator_id,
                                                           "code": code}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_derived_indicator", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------
    # 发布结果查询
    # ------------------------------------------------------------------

    def list_releases(self, *, actor_id: str) -> list[dict[str, Any]]:
        self._reader(actor_id)
        return [self._release_dict(row) for row in self.database.connection.execute(
            "SELECT * FROM stat_releases ORDER BY version_number")]

    def _release_dict(self, row) -> dict[str, Any]:
        return {"release_id": row["release_id"], "candidate_id": row["candidate_id"],
                "version_number": row["version_number"], "period": row["period"],
                "rule_version": row["rule_version"],
                "corrects_release_id": row["corrects_release_id"], "status": row["status"],
                "published_by": row["published_by"], "published_at": row["published_at"]}

    def release_metrics(self, *, actor_id: str, release_id: str,
                        indicator: str | None = None) -> dict[str, Any]:
        self._reader(actor_id)
        release = self.database.connection.execute(
            "SELECT * FROM stat_releases WHERE release_id=?", (release_id,)).fetchone()
        if release is None:
            raise NotFoundError("发布版本不存在")
        query = "SELECT * FROM stat_release_metrics WHERE release_id=?"
        parameters: list[Any] = [release_id]
        if indicator:
            query += " AND indicator=?"
            parameters.append(indicator)
        query += " ORDER BY indicator, scope"
        metrics: dict[str, dict[str, Any]] = {}
        for row in self.database.connection.execute(query, parameters):
            entry = metrics.setdefault(row["indicator"],
                                       {"global": None, "groups": {}, "contributions": {}})
            if row["scope"] == "global":
                entry["global"] = {"value": row["value"], "gap": bool(row["gap"]),
                                   "all_public": bool(row["all_public"])}
            elif row["scope"].startswith("group:"):
                entry["groups"][row["scope"][6:]] = {
                    "value": row["value"], "gap": bool(row["gap"]),
                    "all_public": bool(row["all_public"])}
            elif row["scope"].startswith("economy:"):
                entry["contributions"][row["scope"][8:]] = {
                    "value": row["value"], "gap": bool(row["gap"]),
                    "all_public": bool(row["all_public"])}
        return {"release": self._release_dict(release), "indicators": metrics}

    # ------------------------------------------------------------------
    # 审计重算：以同一证据快照确定性重算
    # ------------------------------------------------------------------

    def recompute_release(self, *, actor_id: str, release_id: str,
                          indicator: str | None = None) -> dict[str, Any]:
        actor = self._actor(self.database.connection, actor_id)
        self._require(actor, "auditor", "admin")
        with self.database.transaction() as connection:
            release = connection.execute("SELECT * FROM stat_releases WHERE release_id=?",
                                         (release_id,)).fetchone()
            if release is None:
                raise NotFoundError("发布版本不存在")
            rule = self._load_ruleset(connection, release["rule_version"])
            differences: list[dict[str, Any]] = []
            observations: list[MetricObs] = []
            for snapshot in connection.execute(
                    "SELECT * FROM stat_release_inputs WHERE release_id=? ORDER BY dataset_id",
                    (release_id,)):
                dataset = connection.execute(
                    "SELECT * FROM stat_datasets WHERE dataset_id=?",
                    (snapshot["dataset_id"],)).fetchone()
                if dataset is None:
                    differences.append({"dataset_id": snapshot["dataset_id"],
                                        "issue": "snapshot_dataset_missing"})
                    continue
                current_hash = digest(json.loads(dataset["payload_json"]))
                if current_hash != snapshot["payload_hash"] or \
                        dataset["payload_hash"] != snapshot["payload_hash"]:
                    differences.append({"dataset_id": snapshot["dataset_id"],
                                        "issue": "payload_hash_mismatch"})
                    continue
                payload = json.loads(dataset["payload_json"])
                _, result_observations = validate_dataset(
                    DatasetInput(
                        economy_code=dataset["economy_code"],
                        period=dataset["period"],
                        methodology=dataset["methodology"],
                        currency=dataset["currency"],
                        confidentiality=dataset["confidentiality"],
                        source_evidence=dataset["source_evidence"],
                        observations=payload.get("observations"),
                    ),
                    rule,
                    {},  # 一致性检查不影响规范化数值
                )
                for obs in result_observations:
                    observations.append(MetricObs(
                        economy=dataset["economy_code"], indicator=obs.indicator,
                        value=obs.value_base, gap=obs.gap,
                        public=dataset["confidentiality"] == "public"))
            derived: list[DerivedSpec] = []
            for row in connection.execute(
                    "SELECT * FROM stat_release_derived WHERE release_id=? ORDER BY indicator_id",
                    (release_id,)):
                stored = connection.execute(
                    "SELECT * FROM stat_derived_indicators WHERE indicator_id=?",
                    (row["indicator_id"],)).fetchone()
                if stored is None:
                    differences.append({"indicator_id": row["indicator_id"],
                                        "issue": "derived_indicator_missing"})
                    continue
                formula = json.loads(stored["formula_json"])
                if digest(formula) != row["formula_hash"]:
                    differences.append({"indicator_id": row["indicator_id"],
                                        "issue": "derived_formula_mismatch"})
                    continue
                derived.append(DerivedSpec(stored["indicator_id"], stored["code"], formula))
            recomputed = compute_indicator_rows(observations, self._economy_groups(connection),
                                                derived)
            stored_rows = {}
            for row in connection.execute(
                    "SELECT * FROM stat_release_metrics WHERE release_id=?", (release_id,)):
                stored_rows[(row["indicator"], row["scope"])] = row
            recomputed_keys = set()
            for ind, rows in recomputed.items():
                for row in rows:
                    key = (ind, row.scope)
                    recomputed_keys.add(key)
                    stored = stored_rows.get(key)
                    expected = {"value": format_decimal(row.value), "gap": 1 if row.gap else 0,
                                "all_public": 1 if row.all_public else 0}
                    if stored is None:
                        differences.append({"indicator": ind, "scope": row.scope,
                                            "issue": "metric_missing", "expected": expected})
                    elif stored["value"] != expected["value"] or \
                            stored["gap"] != expected["gap"] or \
                            stored["all_public"] != expected["all_public"]:
                        differences.append({"indicator": ind, "scope": row.scope,
                                            "issue": "metric_mismatch",
                                            "expected": expected,
                                            "actual": {"value": stored["value"],
                                                       "gap": stored["gap"],
                                                       "all_public": stored["all_public"]}})
            for key in sorted(set(stored_rows) - recomputed_keys):
                differences.append({"indicator": key[0], "scope": key[1],
                                    "issue": "unexpected_stored_metric"})
            summary = self._summarize(recomputed, indicator)
            return {"release_id": release_id, "version_number": release["version_number"],
                    "match": not differences, "differences": differences,
                    "checked_metrics": len(recomputed_keys), "indicators": summary}

    def _summarize(self, recomputed: dict[str, Any],
                   indicator: str | None) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for ind, rows in sorted(recomputed.items()):
            if indicator and ind != indicator:
                continue
            entry: dict[str, Any] = {"global": None, "groups": {}, "contributions": []}
            global_value: Decimal | None = None
            for row in rows:
                if row.scope == "global":
                    global_value = row.value
                    entry["global"] = {"value": format_decimal(row.value), "gap": row.gap}
                elif row.scope.startswith("group:"):
                    entry["groups"][row.scope[6:]] = {"value": format_decimal(row.value),
                                                      "gap": row.gap}
            for row in rows:
                if row.scope.startswith("economy:"):
                    share = Decimal(0)
                    if global_value:
                        share = quantize(row.value / global_value)
                    entry["contributions"].append({
                        "economy_code": row.scope[8:],
                        "value": format_decimal(row.value),
                        "share": format_decimal(share), "gap": row.gap})
            summary[ind] = entry
        return summary

    # ------------------------------------------------------------------
    # 公共接口：只返回允许公开的结果
    # ------------------------------------------------------------------

    def public_releases(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM stat_releases ORDER BY version_number").fetchall()
        return [{"release_id": row["release_id"], "version_number": row["version_number"],
                 "period": row["period"], "rule_version": row["rule_version"],
                 "status": row["status"], "published_at": row["published_at"],
                 "corrects_release_id": row["corrects_release_id"]} for row in rows]

    def public_indicators(self, *, release_id: str | None = None,
                          indicator: str | None = None) -> dict[str, Any]:
        if release_id is None:
            release = self.database.connection.execute(
                "SELECT * FROM stat_releases WHERE status='published' "
                "ORDER BY version_number DESC LIMIT 1").fetchone()
        else:
            release = self.database.connection.execute(
                "SELECT * FROM stat_releases WHERE release_id=?", (release_id,)).fetchone()
        if release is None:
            raise NotFoundError("没有已公开的版本")
        query = "SELECT * FROM stat_release_metrics WHERE release_id=?"
        parameters: list[Any] = [release["release_id"]]
        if indicator:
            query += " AND indicator=?"
            parameters.append(indicator)
        query += " ORDER BY indicator, scope"
        indicators: dict[str, Any] = {}
        restricted: list[str] = []
        for row in self.database.connection.execute(query, parameters):
            entry = indicators.setdefault(row["indicator"],
                                          {"global": None, "groups": {}, "contributions": []})
            if row["scope"] == "global":
                if not row["all_public"]:
                    restricted.append(row["indicator"])
                    continue
                entry["global"] = {"value": row["value"], "gap": bool(row["gap"])}
            elif row["scope"].startswith("group:") and row["all_public"]:
                entry["groups"][row["scope"][6:]] = {"value": row["value"],
                                                     "gap": bool(row["gap"])}
            elif row["scope"].startswith("economy:") and row["all_public"]:
                entry["contributions"].append({"economy_code": row["scope"][8:],
                                               "value": row["value"],
                                               "gap": bool(row["gap"])})
        visible = {code: data for code, data in indicators.items()
                   if data["global"] is not None}
        return {"release": {"release_id": release["release_id"],
                            "version_number": release["version_number"],
                            "period": release["period"], "status": release["status"]},
                "indicators": visible, "restricted_indicators": sorted(set(restricted))}
