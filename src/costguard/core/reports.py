"""Report building: pure functions from cost frames to a format-neutral ``Report``.

Renderers (Rich table, JSON, CSV, HTML) live in ``costguard.interfaces.render`` and only format a
``Report``; they never compute anything. That keeps every number testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import pandas as pd

from costguard.core.waste.engine import Finding

Unit = Literal["money", "percent", "count", "text"]


@dataclass(frozen=True, slots=True)
class Kpi:
    label: str
    value: float | str | None
    unit: Unit = "money"
    delta_pct: float | None = None  # change vs a comparison period, e.g. +0.12 = +12%
    note: str = ""


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    frame: pd.DataFrame
    money_columns: tuple[str, ...] = ()
    percent_columns: tuple[str, ...] = ()
    note: str = ""


@dataclass(slots=True)
class Report:
    title: str
    currency: str
    period: tuple[date, date] | None = None
    kpis: list[Kpi] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    series: pd.Series | None = None  # optional daily total, for sparklines/charts
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _pct(new: float, old: float) -> float | None:
    return None if old == 0 else new / old - 1


def _same_day_last_month(d: date) -> date:
    first_this = d.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=min(d.day, last_prev.day))


def days_in_month(d: date) -> int:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).day


def dashboard(
    wide: pd.DataFrame,
    *,
    budget: float | None = None,
    currency: str = "USD",
    top: int = 8,
) -> Report:
    """Month-to-date view. ``wide``: daily cost indexed by date, one column per service."""
    if wide.empty:
        msg = "no cost data"
        raise ValueError(msg)
    wide = wide.sort_index()
    as_of = wide.index.max().date()
    month_start = as_of.replace(day=1)
    prev_end = _same_day_last_month(as_of)
    prev_start = prev_end.replace(day=1)

    def window(start: date, end: date) -> pd.DataFrame:
        return wide.loc[pd.Timestamp(start) : pd.Timestamp(end)]

    mtd = window(month_start, as_of)
    prev = window(prev_start, prev_end)
    mtd_total = float(mtd.to_numpy().sum())
    prev_total = float(prev.to_numpy().sum())

    daily_total = wide.sum(axis=1)
    run_rate = float(daily_total.iloc[-7:].mean())
    remaining = days_in_month(as_of) - as_of.day
    forecast = mtd_total + run_rate * remaining

    kpis = [
        Kpi("Month to date", mtd_total, delta_pct=_pct(mtd_total, prev_total),
            note=f"vs {prev_start:%d %b}–{prev_end:%d %b}"),
        Kpi("Run-rate forecast", forecast,
            note=f"7-day avg {run_rate:,.0f}/day × {remaining} days"),
        Kpi("Yesterday", float(daily_total.iloc[-1]),
            delta_pct=_pct(float(daily_total.iloc[-1]), float(daily_total.iloc[-8]))
            if len(daily_total) >= 8 else None,
            note="vs same weekday last week"),
    ]  # fmt: skip
    if budget:
        kpis.append(
            Kpi("Budget used (forecast)", forecast / budget, unit="percent",
                note=f"budget {budget:,.0f}{' — OVER' if forecast > budget else ''}")
        )  # fmt: skip

    by_service = pd.DataFrame(
        {"mtd": mtd.sum(), "prev_mtd": prev.sum().reindex(mtd.columns, fill_value=0.0)}
    )
    by_service["change"] = by_service["mtd"] - by_service["prev_mtd"]
    by_service["change_pct"] = by_service["change"] / by_service["prev_mtd"].where(
        by_service["prev_mtd"] != 0
    )
    by_service["share"] = by_service["mtd"] / mtd_total if mtd_total else 0.0
    by_service = by_service.sort_values("mtd", ascending=False).head(top)
    by_service = by_service.rename_axis("service").reset_index()

    last7 = wide.iloc[-7:].sum()
    prior7 = wide.iloc[-14:-7].sum()
    movers = pd.DataFrame({"last_7d": last7, "prior_7d": prior7})
    movers["change"] = movers["last_7d"] - movers["prior_7d"]
    movers["change_pct"] = movers["change"] / movers["prior_7d"].where(movers["prior_7d"] != 0)
    movers = (
        movers.reindex(movers["change"].abs().sort_values(ascending=False).index)
        .head(5)
        .rename_axis("service")
        .reset_index()
    )

    return Report(
        title=f"Cost dashboard — {as_of:%B %Y}",
        currency=currency,
        period=(month_start, as_of),
        kpis=kpis,
        sections=[
            Section("Top services (month to date)", by_service,
                    money_columns=("mtd", "prev_mtd", "change"),
                    percent_columns=("change_pct", "share")),
            Section("Biggest movers (last 7 days vs prior 7)", movers,
                    money_columns=("last_7d", "prior_7d", "change"),
                    percent_columns=("change_pct",)),
        ],
        series=daily_total.iloc[-60:],
    )  # fmt: skip


Freq = Literal["day", "week", "month"]
_PERIOD = {"day": "D", "week": "W-SUN", "month": "M"}  # W-SUN = weeks Monday..Sunday


def trend(
    wide: pd.DataFrame,
    *,
    freq: Freq = "month",
    top: int = 5,
    periods: int = 12,
    currency: str = "USD",
    dimension: str = "service",
) -> Report:
    """Cost per period for the top-N keys (others folded into 'Other'), with period-on-period %."""
    if wide.empty:
        msg = "no cost data"
        raise ValueError(msg)
    idx = pd.DatetimeIndex(wide.index)
    starts = idx.to_period(_PERIOD[freq]).start_time
    resampled = wide.groupby(starts).sum().iloc[-periods:]
    leaders = resampled.sum().sort_values(ascending=False).index[:top]
    table = resampled[leaders].copy()
    others = resampled.drop(columns=leaders).sum(axis=1)
    if (others > 0).any():
        table["Other"] = others
    table["Total"] = resampled.sum(axis=1)
    table["change_pct"] = table["Total"].pct_change()
    fmt = {"day": "%Y-%m-%d", "week": "wk %Y-%m-%d", "month": "%Y-%m"}[freq]
    # A partial last period (e.g. the current month) is flagged, and not compared, because
    # "-70% vs last month" on day 9 is noise, not information.
    last_day = wide.index.max()
    period_end = {
        "day": last_day,
        "week": table.index[-1] + pd.Timedelta(days=6),
        "month": table.index[-1] + pd.offsets.MonthEnd(0),
    }[freq]
    note = ""
    if last_day < period_end:
        note = f"Last period is partial (data to {last_day:%Y-%m-%d}); change not shown."
        table.loc[table.index[-1], "change_pct"] = float("nan")
    table.index = pd.DatetimeIndex(table.index).strftime(fmt)
    table = table.rename_axis("period").reset_index()
    money_cols = tuple(c for c in table.columns if c not in ("period", "change_pct"))
    return Report(
        title=f"Cost trend by {dimension} ({freq})",
        currency=currency,
        period=(wide.index.min().date(), last_day.date()),
        sections=[Section(f"Top {top} by {dimension}", table, money_columns=money_cols,
                          percent_columns=("change_pct",), note=note)],
        series=wide.sum(axis=1).iloc[-90:],
    )  # fmt: skip


def audit_report(findings: list[Finding], *, currency: str = "USD", fx: float = 1.0) -> Report:
    """Waste findings ranked by estimated monthly saving."""
    total = sum(f.monthly_saving_usd for f in findings) * fx
    by_rule = (
        pd.DataFrame(
            [{"rule": f.rule_id, "saving": f.monthly_saving_usd * fx} for f in findings],
            columns=["rule", "saving"],
        )
        .groupby("rule", as_index=False)
        .agg(findings=("saving", "size"), monthly_saving=("saving", "sum"))
        .sort_values("monthly_saving", ascending=False)
    )
    rows = pd.DataFrame(
        [
            {"severity": f.severity, "rule": f.rule_id, "resource": f.resource_id,
             "region": f.region, "detail": f.detail, "monthly_saving": f.monthly_saving_usd * fx,
             "action": f.action}
            for f in findings
        ],
        columns=["severity", "rule", "resource", "region", "detail", "monthly_saving", "action"],
    )  # fmt: skip
    return Report(
        title="Waste audit",
        currency=currency,
        kpis=[
            Kpi("Estimated saving / month", total),
            Kpi("Estimated saving / year", total * 12),
            Kpi("Findings", float(len(findings)), unit="count"),
            Kpi("High severity", float(sum(f.severity == "high" for f in findings)), unit="count"),
        ],
        sections=[
            Section("Savings by rule", by_rule, money_columns=("monthly_saving",)),
            Section("Findings (largest saving first)", rows, money_columns=("monthly_saving",),
                    note="Estimates from static list prices; verify before acting."),
        ],
    )  # fmt: skip
