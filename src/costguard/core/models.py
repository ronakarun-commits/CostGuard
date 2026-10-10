"""Domain models.

The grain of a cost record is **one day x one account x one service x one region x one usage type**.
That is the finest grain Cost Explorer returns with `GroupBy` and it is what every analysis is built
on: totals are always aggregations of records, never stored separately.

Field names follow the FinOps FOCUS specification (snake_case), so a FOCUS export can be loaded
directly and the vocabulary matches what FinOps teams use:

=====================  ===========================
costguard field        FOCUS column
=====================  ===========================
charge_date            ChargePeriodStart (date)
sub_account_id         SubAccountId
sub_account_name       SubAccountName
service_name           ServiceName
service_category       ServiceCategory
region_id              RegionId
sku_id                 SkuId (AWS usage type)
consumed_quantity      ConsumedQuantity
consumed_unit          ConsumedUnit
billed_cost            BilledCost
billing_currency       BillingCurrency
tags                   Tags
=====================  ===========================

Frozen dataclasses (not Pydantic) are used here because the core is pure and hot: generating a
year of data creates ~100k records, and validation already happens at the edges (providers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class AnomalyKind(StrEnum):
    """Shapes of anomaly the synthetic provider can inject (and detectors should find)."""

    SPIKE = "spike"  # one to three days of very high spend, then back to normal
    LEVEL_SHIFT = "level_shift"  # spend steps up and stays up (forgotten resource)
    DRIFT = "drift"  # spend creeps up a little every day (leak, log growth)
    PRICE_CHANGE = "price_change"  # unit price changes, usage unchanged


@dataclass(frozen=True, slots=True)
class Account:
    """An AWS account (FOCUS: sub-account)."""

    id: str
    name: str
    environment: str  # prod / staging / dev / data


@dataclass(frozen=True, slots=True)
class CostRecord:
    """One row of cost at the canonical grain."""

    charge_date: date
    sub_account_id: str
    sub_account_name: str
    service_name: str
    service_category: str
    region_id: str
    sku_id: str
    consumed_quantity: float
    consumed_unit: str
    billed_cost: float
    billing_currency: str = "USD"
    tags: dict[str, str] = field(default_factory=dict, hash=False, compare=False)

    @property
    def unit_price(self) -> float:
        """Effective price per unit. Zero-usage rows have no meaningful price."""
        if self.consumed_quantity == 0:
            return 0.0
        return self.billed_cost / self.consumed_quantity


@dataclass(frozen=True, slots=True)
class InjectedAnomaly:
    """Ground truth for an anomaly the synthetic provider inserted.

    Stored alongside the data so detectors can be scored with precision/recall in Phase 5.
    """

    id: str
    kind: AnomalyKind
    sub_account_id: str
    service_name: str
    start_date: date
    end_date: date  # inclusive
    magnitude: float  # multiplier (spike/shift/price) or daily growth rate (drift)
    description: str

    def covers(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date


@dataclass(frozen=True, slots=True)
class DateRange:
    """Half-open range [start, end), matching the Cost Explorer `TimePeriod` convention."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            msg = f"end ({self.end}) must be after start ({self.start})"
            raise ValueError(msg)

    @property
    def days(self) -> int:
        return (self.end - self.start).days
