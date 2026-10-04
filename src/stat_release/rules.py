"""规则版本定义解析与确定性校验、规范化逻辑。

所有函数都是纯函数：同一规则版本、同一报送内容、同一期间必然得到
同一组规范化记录与发现项，这是审计重算一致性的基础。
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

QUANTUM = Decimal("0.000001")
UNIT_SCALE = {
    "unit": Decimal("1"),
    "thousand": Decimal("1000"),
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
}
CONFIDENTIALITIES = ("public", "internal", "restricted")
FORMULA_OPS = ("sum", "ratio", "growth")


class RulesError(ValueError):
    """规则定义、公式或报送内容不符合约束。"""


def parse_decimal(value: Any, field: str) -> Decimal:
    """把外部输入解析为有限 Decimal，拒绝布尔与非数值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise RulesError(f"{field} 必须是数值")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise RulesError(f"{field} 必须是数值") from exc
    if not result.is_finite():
        raise RulesError(f"{field} 必须是有限数值")
    return result


def quantize(value: Decimal) -> Decimal:
    """按统一精度舍入，保证跨次计算结果一致。"""

    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    """输出固定精度的十进制文本。"""

    return format(quantize(value), "f")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RulesError(f"{field} 不能为空")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RulesError(f"{field} 必须是非空数组")
    result = [_required_text(item, f"{field} 元素") for item in value]
    if len(set(result)) != len(result):
        raise RulesError(f"{field} 不能重复")
    return result


def _period_bounds(item: dict[str, Any], field: str) -> tuple[str, str]:
    start = _required_text(item.get("effective_from"), f"{field}.effective_from")
    end = _required_text(item.get("effective_to"), f"{field}.effective_to")
    if start > end:
        raise RulesError(f"{field} 的生效期起止颠倒")
    return start, end


def validate_definition(definition: Any) -> dict[str, Any]:
    """校验并规范化规则版本定义，返回规范化副本。"""

    if not isinstance(definition, dict):
        raise RulesError("规则定义必须是对象")
    methodologies = _string_list(definition.get("methodologies"), "methodologies")
    raw_indicators = definition.get("indicators")
    if not isinstance(raw_indicators, list) or not raw_indicators:
        raise RulesError("indicators 必须是非空数组")
    indicators = []
    codes: set[str] = set()
    for item in raw_indicators:
        if not isinstance(item, dict):
            raise RulesError("指标定义必须是对象")
        code = _required_text(item.get("code"), "indicator.code")
        name = _required_text(item.get("name"), "indicator.name")
        methodology = _required_text(item.get("methodology"), "indicator.methodology")
        if methodology not in methodologies:
            raise RulesError(f"指标 {code} 的口径未在 methodologies 中声明")
        if code in codes:
            raise RulesError(f"指标代码 {code} 重复")
        codes.add(code)
        indicators.append({"code": code, "name": name, "methodology": methodology})
    raw_fx = definition.get("fx_rates")
    if not isinstance(raw_fx, list) or not raw_fx:
        raise RulesError("fx_rates 必须是非空数组")
    fx_rates = []
    usd_anchor = False
    for item in raw_fx:
        if not isinstance(item, dict):
            raise RulesError("汇率定义必须是对象")
        currency = _required_text(item.get("currency"), "fx.currency").upper()
        rate = parse_decimal(item.get("usd_per_unit"), "fx.usd_per_unit")
        if rate <= 0:
            raise RulesError("汇率必须为正数")
        start, end = _period_bounds(item, "fx")
        if currency == "USD" and rate == 1:
            usd_anchor = True
        fx_rates.append({"currency": currency, "usd_per_unit": decimal_text(rate),
                         "effective_from": start, "effective_to": end})
    if not usd_anchor:
        raise RulesError("fx_rates 必须包含取值为 1 的 USD 基准汇率")
    raw_map = definition.get("classification_map", [])
    if not isinstance(raw_map, list):
        raise RulesError("classification_map 必须是数组")
    classification_map = []
    sources: set[str] = set()
    for item in raw_map:
        if not isinstance(item, dict):
            raise RulesError("分类映射必须是对象")
        source = _required_text(item.get("source"), "mapping.source")
        target = _required_text(item.get("target"), "mapping.target")
        if target not in codes:
            raise RulesError(f"映射目标 {target} 不是标准指标")
        if source in sources:
            raise RulesError(f"映射来源 {source} 重复")
        sources.add(source)
        start, end = _period_bounds(item, "mapping")
        classification_map.append({"source": source, "target": target,
                                   "effective_from": start, "effective_to": end})
    required_economies = _string_list(definition.get("required_economies"), "required_economies")
    raw_groups = definition.get("economy_groups", [])
    if not isinstance(raw_groups, list):
        raise RulesError("economy_groups 必须是数组")
    economy_groups = []
    group_codes: set[str] = set()
    for item in raw_groups:
        if not isinstance(item, dict):
            raise RulesError("分组定义必须是对象")
        code = _required_text(item.get("code"), "group.code")
        name = _required_text(item.get("name"), "group.name")
        members = _string_list(item.get("members"), "group.members")
        unknown = [member for member in members if member not in required_economies]
        if unknown:
            raise RulesError(f"分组 {code} 包含未登记经济体: {','.join(unknown)}")
        if code in group_codes:
            raise RulesError(f"分组代码 {code} 重复")
        group_codes.add(code)
        economy_groups.append({"code": code, "name": name, "members": members})
    tolerance = parse_decimal(definition.get("consistency_tolerance", "0.5"), "consistency_tolerance")
    if tolerance < 0:
        raise RulesError("consistency_tolerance 不能为负")
    threshold = parse_decimal(definition.get("coverage_threshold", "0.8"), "coverage_threshold")
    if threshold <= 0 or threshold > 1:
        raise RulesError("coverage_threshold 必须在 (0,1] 区间")
    countersigns = definition.get("required_countersigns", 2)
    if isinstance(countersigns, bool) or not isinstance(countersigns, int) or countersigns < 1:
        raise RulesError("required_countersigns 必须是正整数")
    return {
        "methodologies": methodologies,
        "indicators": indicators,
        "fx_rates": fx_rates,
        "classification_map": classification_map,
        "required_economies": required_economies,
        "economy_groups": economy_groups,
        "consistency_tolerance": decimal_text(tolerance),
        "coverage_threshold": decimal_text(threshold),
        "required_countersigns": countersigns,
    }


def fx_for_period(definition: dict[str, Any], currency: str, period: str) -> Decimal | None:
    """选取对指定期间生效的汇率；多条命中时取生效期最晚的一条。"""

    best = None
    for rate in definition["fx_rates"]:
        if rate["currency"] != currency:
            continue
        if not (rate["effective_from"] <= period <= rate["effective_to"]):
            continue
        if best is None or rate["effective_from"] > best["effective_from"]:
            best = rate
    if best is None:
        return None
    return Decimal(best["usd_per_unit"])


def classify(definition: dict[str, Any], code: str, period: str) -> str | None:
    """把报送分类代码映射为标准指标代码；标准代码直接通过。"""

    if any(indicator["code"] == code for indicator in definition["indicators"]):
        return code
    for mapping in definition["classification_map"]:
        if mapping["source"] == code and mapping["effective_from"] <= period <= mapping["effective_to"]:
            return mapping["target"]
    return None


def normalize_records(records: Any, definition: dict[str, Any], *, period: str,
                      currency: str, unit: str,
                      exempted: set[tuple[str, str]] | None = None
                      ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """把原始记录规范化为美元标准指标，返回 (规范化记录, 发现项)。

    exempted 是 (check_type, subject) 集合：命中的错误视为已豁免，
    不再产生发现项，对应记录仍按错误处理（不进入规范化结果）。
    """

    exempted = exempted or set()
    findings: list[dict[str, Any]] = []

    def report(check_type: str, severity: str, subject: str, detail: dict[str, Any]) -> None:
        if severity == "error" and (check_type, subject) in exempted:
            return
        findings.append({"check_type": check_type, "severity": severity,
                         "subject": subject, "detail": detail})

    pending: list[tuple[str, str, Decimal]] = []
    seen: set[str] = set()
    if not isinstance(records, list):
        report("structure", "error", "records", {"reason": "records 必须是数组"})
        records = []
    for index, record in enumerate(records):
        subject = f"record[{index}]"
        if not isinstance(record, dict):
            report("structure", "error", subject, {"reason": "记录必须是对象"})
            continue
        indicator = record.get("indicator")
        if not isinstance(indicator, str) or not indicator.strip():
            report("structure", "error", subject, {"reason": "indicator 不能为空"})
            continue
        indicator = indicator.strip()
        try:
            amount = parse_decimal(record.get("value"), f"{subject}.value")
        except RulesError as exc:
            report("structure", "error", subject, {"reason": str(exc)})
            continue
        target = classify(definition, indicator, period)
        if target is None:
            report("classification", "error", subject,
                   {"indicator": indicator, "reason": "分类代码未映射"})
            continue
        if target in seen:
            report("structure", "error", subject,
                   {"indicator": target, "reason": "同一指标在数据集中重复"})
            continue
        seen.add(target)
        pending.append((target, indicator, amount))
    normalized: list[dict[str, str]] = []
    if pending:
        if unit not in UNIT_SCALE:
            report("structure", "error", "unit", {"unit": unit, "reason": "未知单位"})
        else:
            rate = fx_for_period(definition, currency, period)
            if rate is None:
                report("fx", "error", f"currency:{currency}",
                       {"currency": currency, "period": period, "reason": "缺少有效汇率"})
            else:
                scale = UNIT_SCALE[unit]
                for target, source, amount in pending:
                    normalized.append({"indicator": target, "source_indicator": source,
                                       "value": decimal_text(amount * scale * rate)})
    normalized.sort(key=lambda item: item["indicator"])
    findings.sort(key=lambda item: (item["check_type"], item["subject"],
                                    json.dumps(item["detail"], ensure_ascii=False, sort_keys=True)))
    return normalized, findings


def gap_findings(definition: dict[str, Any], *, economy: str, methodology: str,
                 present_indicators: Iterable[str]) -> list[dict[str, Any]]:
    """对报送口径下的必填指标做缺口标记。"""

    present = set(present_indicators)
    findings = []
    for indicator in definition["indicators"]:
        if indicator["methodology"] != methodology:
            continue
        if indicator["code"] not in present:
            findings.append({"check_type": "gap", "severity": "warning",
                             "subject": f"{economy}:{indicator['code']}",
                             "detail": {"economy": economy, "indicator": indicator["code"],
                                        "methodology": methodology, "reason": "必填指标缺失"}})
    return findings


def consistency_findings(definition: dict[str, Any], *, economy: str,
                         normalized: list[dict[str, str]],
                         base_values: dict[str, str]) -> list[dict[str, Any]]:
    """对照基线版本做跨期一致性验证，超出容忍度即标记。"""

    tolerance = Decimal(definition["consistency_tolerance"])
    findings = []
    for item in normalized:
        previous = base_values.get(item["indicator"])
        if previous is None:
            continue
        old = Decimal(previous)
        if old == 0:
            continue
        new = Decimal(item["value"])
        change = (new - old) / abs(old)
        if abs(change) > tolerance:
            findings.append({"check_type": "consistency", "severity": "warning",
                             "subject": f"{economy}:{item['indicator']}",
                             "detail": {"economy": economy, "indicator": item["indicator"],
                                        "previous": decimal_text(old), "current": decimal_text(new),
                                        "change": decimal_text(change),
                                        "tolerance": definition["consistency_tolerance"],
                                        "reason": "跨期变动超过容忍度"}})
    return findings


def coverage_ratio(definition: dict[str, Any], covered_economies: Iterable[str]) -> Decimal:
    """计算必需经济体的覆盖度。"""

    required = definition["required_economies"]
    if not required:
        return Decimal("1")
    covered = set(covered_economies)
    hit = sum(1 for economy in required if economy in covered)
    return quantize(Decimal(hit) / Decimal(len(required)))


def validate_formula(formula: Any, definition: dict[str, Any]) -> dict[str, Any]:
    """校验派生指标公式并返回规范化形式。"""

    if not isinstance(formula, dict):
        raise RulesError("公式必须是对象")
    op = formula.get("op")
    catalog = {indicator["code"] for indicator in definition["indicators"]}

    def known(code: Any) -> str:
        code = _required_text(code, "公式引用的指标代码")
        if code not in catalog:
            raise RulesError(f"公式引用了未登记指标 {code}")
        return code

    if op == "sum":
        terms = _string_list(formula.get("terms"), "formula.terms")
        return {"op": "sum", "terms": [known(term) for term in terms]}
    if op == "ratio":
        numerator = known(formula.get("numerator"))
        denominator = known(formula.get("denominator"))
        if numerator == denominator:
            raise RulesError("ratio 公式的分子分母不能相同")
        return {"op": "ratio", "numerator": numerator, "denominator": denominator}
    if op == "growth":
        return {"op": "growth", "base": known(formula.get("base"))}
    raise RulesError("公式 op 必须是 sum、ratio 或 growth")


def formula_scopes(formula: dict[str, Any], definition: dict[str, Any]) -> list[str]:
    """计算公式涉及的口径集合，用于分析员授权范围核对。"""

    if formula["op"] == "sum":
        codes = formula["terms"]
    elif formula["op"] == "ratio":
        codes = [formula["numerator"], formula["denominator"]]
    else:
        codes = [formula["base"]]
    catalog = {indicator["code"]: indicator for indicator in definition["indicators"]}
    return sorted({catalog[code]["methodology"] for code in codes})
