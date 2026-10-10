"""Price-volume variance analysis (the standard FP&A bridge), applied to cloud spend.

For each line item (service x usage type x unit), the change in cost between a base period (0)
and a current period (1) is split into effects that sum exactly to the change:

    calendar = (D1/D0 - 1) * C0                   more/fewer days in the period
    volume   = (Q1 - Q0 * D1/D0) * P0             used more/less, at the old price
    price    = (P1 - P0) * Q1                     paid a different rate for what was used
    new / discontinued                            items present in only one period

``calendar + volume + price == C1 - C0`` for continuing items (P = C / Q). The calendar
effect stops "March is up 10% vs February" from being blamed on engineers when it is 3 more days.

Requires a real usage unit per line: Cost Explorer data pulled with ``--detail usage_type`` (or
synthetic data). Rows whose unit is ``Mixed`` (service-level pulls mix hours, GB and requests)
cannot be split; their whole change is reported as ``unattributed``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from costguard.core.models import DateRange

KEYS = ["service_name", "sku_id", "consumed_unit"]
EFFECTS = ["calendar", "volume", "price", "new", "discontinued", "unattributed"]


@dataclass(frozen=True, slots=True)
class VarianceResult:
    base: DateRange
    current: DateRange
    lines: pd.DataFrame  # one row per line item
    by_service: pd.DataFrame  # effects summed per service

    @property
    def totals(self) -> dict[str, float]:
        cols = ["cost_base", "cost_current", "change", *EFFECTS]
        return {c: float(self.lines[c].sum()) for c in cols}


def price_volume(costs: pd.DataFrame, base: DateRange, current: DateRange) -> VarianceResult:
    """Decompose the cost change between two periods. ``costs`` is the long cost frame."""
    d0, d1 = base.days, current.days

    def agg(period: DateRange) -> pd.DataFrame:
        mask = (costs["charge_date"] >= pd.Timestamp(period.start)) & (
            costs["charge_date"] < pd.Timestamp(period.end)
        )
        return costs[mask].groupby(KEYS)[["consumed_quantity", "billed_cost"]].sum()

    a, b = agg(base), agg(current)
    df = a.join(b, how="outer", lsuffix="_0", rsuffix="_1").fillna(0.0)
    df.columns = ["q0", "c0", "q1", "c1"]
    q0, c0, q1, c1 = (df[c].to_numpy() for c in ("q0", "c0", "q1", "c1"))
    mixed = df.index.get_level_values("consumed_unit").isin(["Mixed", "N/A", ""])
    cont = (q0 > 0) & (q1 > 0) & ~mixed
    with np.errstate(divide="ignore", invalid="ignore"):
        p0 = np.where(q0 > 0, c0 / q0, 0.0)
        p1 = np.where(q1 > 0, c1 / q1, 0.0)
    scale = d1 / d0

    df["calendar"] = np.where(cont, (scale - 1) * c0, 0.0)
    df["volume"] = np.where(cont, (q1 - q0 * scale) * p0, 0.0)
    df["price"] = np.where(cont, (p1 - p0) * q1, 0.0)
    df["new"] = np.where(~mixed & (c0 == 0) & (c1 != 0), c1, 0.0)
    df["discontinued"] = np.where(~mixed & (c1 == 0) & (c0 != 0), -c0, 0.0)
    df["unattributed"] = np.where(mixed | (~cont & (c0 != 0) & (c1 != 0)), c1 - c0, 0.0)
    df["cost_base"], df["cost_current"] = c0, c1
    df["change"] = c1 - c0
    df["price_base"], df["price_current"] = p0, p1
    lines = df.reset_index()[
        [*KEYS, "cost_base", "cost_current", "change", *EFFECTS, "price_base", "price_current"]
    ].sort_values("change", key=np.abs, ascending=False)
    by_service = (
        lines.groupby("service_name")[["cost_base", "cost_current", "change", *EFFECTS]]
        .sum()
        .sort_values("change", key=np.abs, ascending=False)
        .reset_index()
    )
    return VarianceResult(base, current, lines.reset_index(drop=True), by_service)
