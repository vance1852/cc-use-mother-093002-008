"""规则版本、期间与十进制数值的公共工具。

所有金额计算使用 Decimal 并按固定精度量化，保证同一证据快照可以被
确定性地重算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from digital_trade_foundation.errors import ValidationError


PERIOD_PATTERN = re.compile(r"^\d{4}(-Q[1-4])?$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
VALUE_QUANT = Decimal("0.000001")
CONFIDENTIALITY_LEVELS = frozenset({"public", "internal", "confidential"})


def parse_decimal(raw: Any, field: str) -> Decimal:
    """把输入解析为有限 Decimal，失败时抛出 ValidationError。"""

    if isinstance(raw, bool) or raw is None:
        raise ValidationError(f"{field} 必须是数值")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field} 必须是数值") from exc
    if not value.is_finite():
        raise ValidationError(f"{field} 必须是有限数值")
    return value


def quantize(value: Decimal) -> Decimal:
    """按服务统一精度量化。"""

    return value.quantize(VALUE_QUANT, rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal) -> str:
    """生成确定性的十进制文本，用于存储与比对。"""

    return format(quantize(value), "f")


def check_period(period: Any) -> str:
    """校验期间格式：YYYY 或 YYYY-Qn。"""

    period = str(period).strip()
    if not PERIOD_PATTERN.fullmatch(period):
        raise ValidationError("period 必须是 YYYY 或 YYYY-Qn 格式")
    return period


def period_key(period: str) -> tuple[int, int]:
    """把期间转换为可比较的二元组。"""

    if "-Q" in period:
        year, quarter = period.split("-Q")
        return int(year), int(quarter)
    return int(period), 0


def previous_period(period: str) -> str:
    """返回上一期间：年度为上一年，季度为上一季度。"""

    year, quarter = period_key(period)
    if quarter == 0:
        return f"{year - 1:04d}"
    if quarter == 1:
        return f"{year - 1:04d}-Q4"
    return f"{year:04d}-Q{quarter - 1}"


def period_in_range(period: str, effective_from: str, effective_to: str) -> bool:
    """判断期间是否落在生效区间内（含端点）。"""

    key = period_key(period)
    return period_key(effective_from) <= key <= period_key(effective_to)


@dataclass(frozen=True)
class FxRate:
    """某一币种在生效区间内折算基准币种的汇率。"""

    currency: str
    effective_from: str
    effective_to: str
    rate_to_base: Decimal


@dataclass(frozen=True)
class ClassMapping:
    """源分类到目标指标分类的映射，带生效区间。"""

    source_category: str
    target_category: str
    effective_from: str
    effective_to: str


@dataclass(frozen=True)
class RuleSet:
    """一个规则版本的完整可执行内容。"""

    rule_version: str
    base_currency: str
    allowed_methodologies: frozenset[str]
    expected_economies: tuple[str, ...]
    coverage_threshold: Decimal
    consistency_tolerance: Decimal
    required_cosignatures: int
    fx_rates: tuple[FxRate, ...]
    mappings: tuple[ClassMapping, ...]

    def rate_for(self, currency: str, period: str) -> Decimal | None:
        """查找币种在指定期间有效的汇率；基准币种恒为 1。"""

        if currency == self.base_currency:
            return Decimal(1)
        for rate in self.fx_rates:
            if rate.currency == currency and period_in_range(period, rate.effective_from, rate.effective_to):
                return rate.rate_to_base
        return None

    def target_for(self, source_category: str, period: str) -> str | None:
        """查找源分类在指定期间映射到的目标指标。"""

        for mapping in self.mappings:
            if mapping.source_category == source_category and period_in_range(
                period, mapping.effective_from, mapping.effective_to
            ):
                return mapping.target_category
        return None

    def expected_indicators(self, period: str) -> frozenset[str]:
        """规则在该期间要求覆盖的目标指标集合。"""

        return frozenset(
            mapping.target_category
            for mapping in self.mappings
            if period_in_range(period, mapping.effective_from, mapping.effective_to)
        )
