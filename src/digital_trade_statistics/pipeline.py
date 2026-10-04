"""统计校验管线与指标汇总的纯函数实现。

校验顺序：结构检查 → 币种换算 → 分类映射 → 缺口标记 → 跨期一致性。
汇总输出全球合计、分组值和分经济体贡献，全部使用 Decimal 保证
同一证据快照可以被确定性重算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .rules import (
    CONFIDENTIALITY_LEVELS,
    RuleSet,
    format_decimal,
    parse_decimal,
    quantize,
)


@dataclass(frozen=True)
class DatasetInput:
    """一次原始报送中参与校验的字段。"""

    economy_code: str
    period: str
    methodology: str
    currency: str
    confidentiality: str
    source_evidence: str
    observations: Any


@dataclass(frozen=True)
class Finding:
    """一条自动检查产生的发现项。"""

    check_type: str
    severity: str
    code: str
    message: str
    context: dict[str, Any]


@dataclass(frozen=True)
class NormObs:
    """一条换算到基准币种、映射到目标指标后的观测。"""

    indicator: str
    methodology: str
    value_base: Decimal
    gap: bool


def validate_dataset(dataset: DatasetInput, rule: RuleSet,
                     previous_values: Mapping[str, Decimal]) -> tuple[list[Finding], list[NormObs]]:
    """按指定规则版本校验一个数据集，返回发现项与规范化观测。"""

    findings: list[Finding] = []
    observations: list[NormObs] = []

    def add(check_type: str, severity: str, code: str, message: str, **context: Any) -> None:
        findings.append(Finding(check_type, severity, code, message, context))

    # 1. 结构检查
    if dataset.methodology not in rule.allowed_methodologies:
        add("structure", "error", "methodology_not_allowed",
            f"口径 {dataset.methodology} 不在规则 {rule.rule_version} 允许范围内",
            methodology=dataset.methodology)
    if dataset.confidentiality not in CONFIDENTIALITY_LEVELS:
        add("structure", "error", "confidentiality_unknown",
            f"保密级别 {dataset.confidentiality} 无效", confidentiality=dataset.confidentiality)
    if not str(dataset.source_evidence).strip():
        add("structure", "error", "source_evidence_missing", "来源证明不能为空")
    raw_observations = dataset.observations
    if not isinstance(raw_observations, list) or not raw_observations:
        add("structure", "error", "observations_missing", "观测列表不能为空")
        return findings, observations

    parsed: list[tuple[str, Decimal]] = []
    for index, raw in enumerate(raw_observations):
        if not isinstance(raw, dict) or not isinstance(raw.get("category"), str) \
                or not raw.get("category", "").strip() or "value" not in raw:
            add("structure", "error", "observation_shape",
                f"第 {index} 条观测必须包含 category 与 value", index=index)
            continue
        try:
            value = parse_decimal(raw["value"], "value")
        except Exception:
            add("structure", "error", "value_not_numeric",
                f"第 {index} 条观测的 value 不是有限数值", index=index)
            continue
        parsed.append((raw["category"].strip(), value))

    # 2. 币种换算（汇率按生效期匹配）
    rate = rule.rate_for(dataset.currency, dataset.period)
    if rate is None:
        add("currency", "error", "fx_rate_missing",
            f"币种 {dataset.currency} 在期间 {dataset.period} 缺少有效汇率",
            currency=dataset.currency, period=dataset.period)

    # 3. 分类映射（映射按生效期匹配）
    for category, value in parsed:
        target = rule.target_for(category, dataset.period)
        if target is None:
            add("mapping", "error", "mapping_missing",
                f"分类 {category} 在期间 {dataset.period} 缺少有效映射",
                category=category, period=dataset.period)
            continue
        if rate is None:
            observations.append(NormObs(target, dataset.methodology, Decimal(0), True))
            continue
        observations.append(NormObs(target, dataset.methodology, quantize(value * rate), False))

    # 4. 缺口标记：规则要求但该数据集未覆盖的指标
    present = {obs.indicator for obs in observations}
    for indicator in sorted(rule.expected_indicators(dataset.period) - present):
        add("gap", "warning", "indicator_gap",
            f"指标 {indicator} 在期间 {dataset.period} 缺少报送值", indicator=indicator)
        observations.append(NormObs(indicator, dataset.methodology, Decimal(0), True))

    # 5. 跨期一致性验证
    tolerance = rule.consistency_tolerance
    for obs in observations:
        if obs.gap:
            continue
        previous = previous_values.get(obs.indicator)
        if previous is None:
            continue
        if previous == 0:
            if obs.value_base != 0:
                add("consistency", "warning", "consistency_break",
                    f"指标 {obs.indicator} 上期为零、本期非零", indicator=obs.indicator)
            continue
        change = quantize(abs(obs.value_base - previous) / abs(previous))
        if change > tolerance:
            add("consistency", "warning", "consistency_break",
                f"指标 {obs.indicator} 跨期变动 {format_decimal(change)} 超过容差 "
                f"{format_decimal(tolerance)}",
                indicator=obs.indicator, change=format_decimal(change),
                previous=format_decimal(previous), current=format_decimal(obs.value_base))

    return findings, observations


@dataclass(frozen=True)
class MetricObs:
    """参与汇总的一条观测，带经济体与公开属性。"""

    economy: str
    indicator: str
    value: Decimal
    gap: bool
    public: bool


@dataclass(frozen=True)
class MetricRow:
    """一个指标在某个口径范围（全球/分组/经济体）上的结果。"""

    scope: str
    value: Decimal
    gap: bool
    all_public: bool


@dataclass(frozen=True)
class DerivedSpec:
    """分析员创建的派生指标定义。"""

    indicator_id: str
    code: str
    formula: dict[str, Any]

    def inputs(self) -> list[str]:
        if self.formula.get("op") == "sum":
            return list(self.formula.get("inputs", []))
        return [self.formula.get("numerator", ""), self.formula.get("denominator", "")]


def _sum(values) -> Decimal:
    total = Decimal(0)
    for value in values:
        total += value
    return quantize(total)


def _contributions(observations: list[MetricObs]) -> dict[str, dict[str, tuple[Decimal, bool, bool]]]:
    """按指标 → 经济体汇总贡献（值、是否缺口、是否全部可公开）。"""

    grouped: dict[str, dict[str, list[MetricObs]]] = {}
    for obs in observations:
        grouped.setdefault(obs.indicator, {}).setdefault(obs.economy, []).append(obs)
    result: dict[str, dict[str, tuple[Decimal, bool, bool]]] = {}
    for indicator, per_economy in grouped.items():
        contribution: dict[str, tuple[Decimal, bool, bool]] = {}
        for economy, items in per_economy.items():
            non_gap = [item for item in items if not item.gap]
            value = _sum(item.value for item in non_gap)
            gap = any(item.gap for item in items) or not non_gap
            public = all(item.public for item in non_gap) if non_gap else True
            contribution[economy] = (value, gap, public)
        result[indicator] = contribution
    return result


def _apply_derived(contributions: dict[str, dict[str, tuple[Decimal, bool, bool]]],
                   derived: list[DerivedSpec]) -> None:
    """在每个经济体上计算派生指标，再并入贡献表。"""

    for spec in derived:
        inputs = spec.inputs()
        economies: set[str] = set()
        for code in inputs:
            economies |= set(contributions.get(code, {}).keys())
        contribution: dict[str, tuple[Decimal, bool, bool]] = {}
        for economy in sorted(economies):
            values: list[Decimal] = []
            gap = False
            public = True
            for code in inputs:
                value, item_gap, item_public = contributions.get(code, {}).get(
                    economy, (Decimal(0), True, True))
                values.append(value)
                gap = gap or item_gap
                public = public and item_public
            if spec.formula.get("op") == "ratio":
                numerator, denominator = values
                if denominator == 0:
                    contribution[economy] = (Decimal(0), True, public)
                else:
                    contribution[economy] = (quantize(numerator / denominator), gap, public)
            else:
                contribution[economy] = (_sum(values), gap, public)
        if contribution:
            contributions[spec.code] = contribution


def compute_indicator_rows(observations: list[MetricObs], economy_groups: Mapping[str, str],
                           derived: list[DerivedSpec]) -> dict[str, list[MetricRow]]:
    """计算每个指标的全球合计、分组值和分经济体贡献。"""

    contributions = _contributions(observations)
    _apply_derived(contributions, derived)
    result: dict[str, list[MetricRow]] = {}
    for indicator in sorted(contributions):
        contribution = contributions[indicator]
        rows: list[MetricRow] = []
        for economy in sorted(contribution):
            value, gap, public = contribution[economy]
            rows.append(MetricRow(f"economy:{economy}", value, gap, public))
        for group in sorted(set(economy_groups.values())):
            members = [economy for economy in sorted(contribution)
                       if economy_groups.get(economy) == group]
            if not members:
                continue
            rows.append(MetricRow(
                f"group:{group}",
                _sum(contribution[economy][0] for economy in members),
                any(contribution[economy][1] for economy in members),
                all(contribution[economy][2] for economy in members),
            ))
        rows.append(MetricRow(
            "global",
            _sum(item[0] for item in contribution.values()),
            any(item[1] for item in contribution.values()),
            all(item[2] for item in contribution.values()),
        ))
        result[indicator] = rows
    return result


def coverage_ratio(expected_economies, expected_indicators,
                   filled_cells) -> Decimal:
    """覆盖度 = 已有非缺口值的（经济体 × 指标）格数 / 应有格数。"""

    total = len(expected_economies) * len(expected_indicators)
    if total == 0:
        return Decimal(1)
    return quantize(Decimal(len(filled_cells)) / Decimal(total))
