"""从证据快照确定性重算发布结果与影响清单。

compute_results 只依赖传入的快照输入（数据集原文、规则定义、派生公式、
基线版本结果、豁免集合），不读取任何外部状态，因此审计员用同一快照
必然重算出完全一致的结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .rules import coverage_ratio, decimal_text, normalize_records, parse_decimal, quantize


class ComputeError(ValueError):
    """证据快照内容无法确定性重算。"""


def _eval_formula(formula: dict[str, Any], values: dict[str, Decimal],
                  base_values: dict[str, str] | None) -> Decimal | None:
    """在单个经济体上求值派生公式；输入不足时返回 None。"""

    if formula["op"] == "sum":
        present = [values[code] for code in formula["terms"] if code in values]
        if not present:
            return None
        return sum(present, Decimal("0"))
    if formula["op"] == "ratio":
        numerator = values.get(formula["numerator"])
        denominator = values.get(formula["denominator"])
        if numerator is None or denominator is None or denominator == 0:
            return None
        return quantize(numerator / denominator)
    if formula["op"] == "growth":
        new = values.get(formula["base"])
        old_text = (base_values or {}).get(formula["base"])
        if new is None or old_text is None:
            return None
        old = parse_decimal(old_text, "growth.base")
        if old == 0:
            return None
        return quantize((new - old) / abs(old))
    raise ComputeError(f"未知公式算子 {formula.get('op')}")


def compute_results(*, period: str, definition: dict[str, Any],
                    datasets: list[dict[str, Any]], derived: list[dict[str, Any]],
                    base_results: dict[str, Any] | None = None,
                    exempted: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """根据快照输入确定性计算发布结果。

    datasets 元素需包含 dataset_id、economy、currency、unit、records；
    derived 元素需包含 code、name、formula。
    """

    exempted = exempted or set()
    matrix: dict[str, dict[str, Decimal]] = {}
    names = {indicator["code"]: indicator["name"] for indicator in definition["indicators"]}
    for dataset in sorted(datasets, key=lambda item: item["dataset_id"]):
        normalized, findings = normalize_records(
            dataset["records"], definition, period=period,
            currency=dataset["currency"], unit=dataset["unit"], exempted=exempted)
        errors = [item for item in findings if item["severity"] == "error"]
        if errors:
            raise ComputeError(f"数据集 {dataset['dataset_id']} 存在未豁免错误，无法重算")
        values = matrix.setdefault(dataset["economy"], {})
        for item in normalized:
            code = item["indicator"]
            if code in values:
                raise ComputeError(f"经济体 {dataset['economy']} 的指标 {code} 在快照中重复")
            values[code] = Decimal(item["value"])
    base_economies: dict[str, dict[str, str]] = {}
    base_globals: dict[str, str] = {}
    if base_results:
        for code, payload in base_results.get("indicators", {}).items():
            if payload.get("global_total") is not None:
                base_globals[code] = payload["global_total"]
            for economy, value in payload.get("economies", {}).items():
                base_economies.setdefault(economy, {})[code] = value
    for item in sorted(derived, key=lambda entry: entry["code"]):
        names[item["code"]] = item.get("name") or item["code"]
        for economy in sorted(matrix):
            value = _eval_formula(item["formula"], matrix[economy], base_economies.get(economy))
            if value is not None:
                matrix[economy][item["code"]] = value
    indicators: dict[str, Any] = {}
    charts: dict[str, Any] = {}
    conclusions: list[dict[str, str]] = []
    all_codes = sorted({code for values in matrix.values() for code in values})
    for code in all_codes:
        economies = {economy: matrix[economy][code] for economy in sorted(matrix)
                     if code in matrix[economy]}
        if not economies:
            continue
        total = sum(economies.values(), Decimal("0"))
        groups: dict[str, Decimal] = {}
        for group in definition["economy_groups"]:
            members = [economies[member] for member in group["members"] if member in economies]
            if members:
                groups[group["code"]] = sum(members, Decimal("0"))
        if total != 0:
            contribution_economies = {economy: decimal_text(value / total)
                                      for economy, value in economies.items()}
            contribution_groups = {code_: decimal_text(value / total)
                                   for code_, value in groups.items()}
        else:
            contribution_economies = {}
            contribution_groups = {}
        growth = None
        previous_total = base_globals.get(code)
        if previous_total is not None:
            old_total = parse_decimal(previous_total, "base.global_total")
            if old_total != 0:
                growth = decimal_text((total - old_total) / abs(old_total))
        name = names.get(code, code)
        indicators[code] = {
            "name": name,
            "global_total": decimal_text(total),
            "economies": {economy: decimal_text(value) for economy, value in economies.items()},
            "groups": {code_: decimal_text(value) for code_, value in groups.items()},
            "contributions": {"economies": contribution_economies,
                              "groups": contribution_groups},
            "growth": growth,
        }
        charts[code] = [[economy, decimal_text(value)] for economy, value in economies.items()]
        conclusions.append({"key": f"global:{code}",
                            "text": f"{name}({code})全球合计为{decimal_text(total)}美元"})
        if growth is not None:
            conclusions.append({"key": f"growth:{code}",
                                "text": f"{name}({code})全球增速为"
                                        f"{decimal_text(Decimal(growth) * 100)}%"})
    conclusions.sort(key=lambda item: item["key"])
    covered = sorted(matrix)
    return {
        "period": period,
        "indicators": indicators,
        "charts": charts,
        "conclusions": conclusions,
        "covered_economies": covered,
        "coverage": decimal_text(coverage_ratio(definition, covered)),
    }


def diff_results(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """比较两个发布结果，列出受影响的经济体、指标、图表数据和文字结论。"""

    old_indicators = old.get("indicators", {})
    new_indicators = new.get("indicators", {})
    affected_economies: set[str] = set()
    affected_indicators: set[str] = set()
    affected_charts: set[str] = set()
    for code in sorted(set(old_indicators) | set(new_indicators)):
        before = old_indicators.get(code)
        after = new_indicators.get(code)
        if before == after:
            continue
        affected_indicators.add(code)
        before_economies = (before or {}).get("economies", {})
        after_economies = (after or {}).get("economies", {})
        for economy in set(before_economies) | set(after_economies):
            if before_economies.get(economy) != after_economies.get(economy):
                affected_economies.add(economy)
        if old.get("charts", {}).get(code) != new.get("charts", {}).get(code):
            affected_charts.add(code)
    old_conclusions = {item["key"]: item["text"] for item in old.get("conclusions", [])}
    new_conclusions = {item["key"]: item["text"] for item in new.get("conclusions", [])}
    affected_conclusions = sorted(
        key for key in set(old_conclusions) | set(new_conclusions)
        if old_conclusions.get(key) != new_conclusions.get(key))
    return {
        "affected_economies": sorted(affected_economies),
        "affected_indicators": sorted(affected_indicators),
        "affected_charts": sorted(affected_charts),
        "affected_conclusions": affected_conclusions,
    }
