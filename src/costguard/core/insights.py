"""Report builders for the analytics and finance commands (pure: results in, ``Report`` out)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from costguard.core.analytics.anomaly import Evaluation, Incident
from costguard.core.analytics.forecast import Forecast
from costguard.core.analytics.risk import KupiecResult, MonthEndBacktest, MonthEndRisk
from costguard.core.finance.commitments import PlanEvaluation
from costguard.core.finance.fx import FxBridge, FxStress
from costguard.core.finance.variance import EFFECTS, VarianceResult
from costguard.core.reports import Kpi, Report, Section


def anomalies_report(
    incidents: Sequence[Incident], *, method: str, currency: str, fx: float, since: str
) -> Report:
    rows = pd.DataFrame(
        [
            {
                "start": i.start_date.isoformat(),
                "end": i.end_date.isoformat(),
                "days": (i.end_date - i.start_date).days + 1,
                "account": i.sub_account_name,
                "service": i.service_name,
                "peak_ratio": f"{i.peak_ratio:.1f}x",
                "score": round(i.peak_score, 1),
                "excess_cost": i.excess_cost * fx,
            }
            for i in incidents
        ],
        columns=[
            "start",
            "end",
            "days",
            "account",
            "service",
            "peak_ratio",
            "score",
            "excess_cost",
        ],
    )
    total = sum(i.excess_cost for i in incidents) * fx
    return Report(
        title=f"Cost anomalies ({method})",
        currency=currency,
        kpis=[
            Kpi("Incidents", float(len(incidents)), unit="count", note=f"since {since}"),
            Kpi("Excess spend", total, note="actual minus expected, flagged days"),
            Kpi(
                "Largest",
                incidents[0].excess_cost * fx if incidents else 0.0,
                note=f"{incidents[0].sub_account_name}: {incidents[0].service_name}"
                if incidents
                else "",
            ),
        ],
        sections=[
            Section("Incidents (largest excess first)", rows, money_columns=("excess_cost",))
        ],
    )


def evaluation_report(results: Sequence[Evaluation]) -> Report:
    summary = pd.DataFrame(
        [
            {
                "method": e.method,
                "labels": e.labels,
                "detected": e.detected,
                "incidents": e.incidents,
                "precision": e.precision,
                "recall": e.recall,
                "f1": e.f1,
                "mean_delay_days": round(e.mean_delay_days, 1)
                if e.mean_delay_days is not None
                else None,
            }
            for e in results
        ]
    )
    kinds = sorted({k for e in results for k in e.recall_by_kind})
    by_kind = pd.DataFrame(
        [
            {"method": e.method, **{k: e.recall_by_kind.get(k, np.nan) for k in kinds}}
            for e in results
        ]
    )
    best = max(results, key=lambda e: e.f1)
    return Report(
        title="Anomaly detector evaluation (vs injected ground truth)",
        currency="USD",
        kpis=[
            Kpi("Best F1", best.f1, unit="percent", note=best.method),
            Kpi("Labelled anomalies", float(best.labels), unit="count"),
        ],
        sections=[
            Section(
                "Event-level scores",
                summary,
                percent_columns=("precision", "recall", "f1"),
                note="A label counts as detected if an incident on the same account+service "
                "overlaps its window (+3 days).",
            ),
            Section("Recall by anomaly kind", by_kind, percent_columns=tuple(kinds)),
        ],
    )


def forecast_report(fc: Forecast, *, currency: str, fx: float) -> Report:
    q = fc.quantiles((0.05, 0.5, 0.95)) * fx
    weekly = q.groupby(pd.DatetimeIndex(q.index).to_period("W-SUN").start_time).sum()
    # Sums of quantiles are not quantiles of sums, so weekly bands come from the paths.
    week_id = pd.DatetimeIndex(fc.index).to_period("W-SUN").start_time
    rows = []
    for wk in weekly.index:
        cols = np.asarray(week_id == wk)
        totals = fc.paths[:, cols].sum(axis=1) * fx
        p5, p50, p95 = np.quantile(totals, [0.05, 0.5, 0.95])
        rows.append(
            {
                "week_of": wk.strftime("%Y-%m-%d"),
                "days": int(cols.sum()),
                "p5": p5,
                "median": p50,
                "p95": p95,
            }
        )
    total = fc.totals() * fx
    return Report(
        title=f"Spend forecast — next {len(fc.index)} days ({fc.model})",
        currency=currency,
        period=(fc.index[0].date(), fc.index[-1].date()),
        kpis=[
            Kpi("Expected total", float(total.mean())),
            Kpi(
                "90% interval",
                f"{np.quantile(total, 0.05):,.0f} – {np.quantile(total, 0.95):,.0f}",
                unit="text",
            ),
            Kpi("Last 30 days actual", float(fc.history.iloc[-30:].sum()) * fx),
        ],
        sections=[
            Section("Weekly forecast", pd.DataFrame(rows), money_columns=("p5", "median", "p95"))
        ],
        series=pd.concat([fc.history.iloc[-60:] * fx, fc.mean() * fx]),
    )


def risk_report(r: MonthEndRisk, *, currency: str, fx: float, calibrated: bool) -> Report:
    pct = round(r.level * 100)
    kpis = [
        Kpi("Month to date", r.month_to_date * fx, note=f"{r.remaining_days} days left"),
        Kpi("Expected month-end", r.expected * fx),
        Kpi(
            f"Cost-at-Risk ({pct}%)", r.car * fx, note=f"+{(r.car_excess) * fx:,.0f} over expected"
        ),
        Kpi(
            f"Expected Shortfall ({pct}%)",
            r.expected_shortfall * fx,
            note=f"mean of worst {100 - pct}%",
        ),
    ]
    if r.budget is not None and r.breach_probability is not None:
        kpis.append(
            Kpi(
                "P(over budget)",
                r.breach_probability,
                unit="percent",
                note=f"budget {r.budget * fx:,.0f}",
            )
        )
    dist = pd.DataFrame(
        [
            {"measure": "5th percentile", "month_end": r.p5 * fx},
            {"measure": "median", "month_end": r.median * fx},
            {"measure": "expected (mean)", "month_end": r.expected * fx},
            {"measure": f"CaR {pct}%", "month_end": r.car * fx},
            {"measure": f"Expected Shortfall {pct}%", "month_end": r.expected_shortfall * fx},
        ]
    )
    if r.budget is not None:
        dist["vs_budget"] = dist["month_end"] - r.budget * fx
    note = (
        "Bounds conformally calibrated on the model's past errors."
        if calibrated
        else "Raw model bounds (not calibrated)."
    )
    return Report(
        title=f"Month-end cost risk — {r.as_of:%B %Y}",
        currency=currency,
        period=(r.as_of.replace(day=1), r.as_of),
        kpis=kpis,
        sections=[
            Section(
                "Month-end spend distribution",
                dist,
                money_columns=("month_end", "vs_budget"),
                note=note,
            )
        ],
    )


def backtest_report(
    raw: KupiecResult, cal: KupiecResult, month: MonthEndBacktest, *, level: float, model: str
) -> Report:
    def row(name: str, k: KupiecResult) -> dict[str, object]:
        return {
            "bounds": name,
            "days": k.observations,
            "exceedances": k.exceedances,
            "expected_rate": k.expected_rate,
            "observed_rate": k.observed_rate,
            "kupiec_p": round(k.p_value, 4),
            "verdict": "calibrated"
            if k.calibrated
            else ("risk understated" if k.observed_rate > k.expected_rate else "too conservative"),
        }

    kup = pd.DataFrame([row("raw model", raw), row("conformal", cal)])
    months = month.rows.copy()
    return Report(
        title=f"Risk model backtest ({model}, {level:.0%} daily bounds)",
        currency="USD",
        kpis=[
            Kpi(
                "Kupiec p (calibrated)",
                cal.p_value,
                unit="percent",
                note="≥ 5% = cannot reject calibration",
            ),
            Kpi("Month-end MAPE", month.mape, unit="percent", note="forecast made on day 7/14/21"),
            Kpi("90% interval coverage", month.coverage, unit="percent", note="target 90%"),
        ],
        sections=[
            Section(
                "Daily bound exceedances (Kupiec POF test)",
                kup,
                percent_columns=("expected_rate", "observed_rate"),
            ),
            Section(
                "Month-end forecasts vs actual",
                months,
                money_columns=("actual", "forecast", "p5", "p95"),
                percent_columns=("abs_pct_error",),
            ),
        ],
    )


def variance_report(
    v: VarianceResult, *, currency: str, fx: float, budget: float | None = None
) -> Report:
    t = {k: val * fx for k, val in v.totals.items()}
    label = f"{v.current.start:%b %Y} vs {v.base.start:%b %Y}"
    kpis = [
        Kpi(
            "Change",
            t["change"],
            delta_pct=t["change"] / t["cost_base"] if t["cost_base"] else None,
            note=label,
        ),
        Kpi("Volume effect", t["volume"], note="used more / less"),
        Kpi("Price effect", t["price"], note="rate per unit changed"),
        Kpi("Calendar effect", t["calendar"], note=f"{v.current.days} vs {v.base.days} days"),
    ]
    if budget:
        kpis.append(
            Kpi(
                "Budget variance",
                t["cost_current"] - budget * fx,
                note=f"actual vs budget {budget * fx:,.0f}",
            )
        )
    money = ("cost_base", "cost_current", "change", *EFFECTS)
    services = v.by_service.copy()
    services[list(money)] = services[list(money)] * fx
    lines = v.lines.head(15).copy()
    lines[list(money)] = lines[list(money)] * fx
    lines = lines.drop(columns=["price_base", "price_current"])
    bridge = pd.DataFrame(
        [{"step": f"{v.base.start:%b %Y} actual", "amount": t["cost_base"]}]
        + [{"step": e.replace("_", " "), "amount": t[e]} for e in EFFECTS if abs(t[e]) > 0.005]
        + [{"step": f"{v.current.start:%b %Y} actual", "amount": t["cost_current"]}]
    )
    return Report(
        title=f"Price-volume variance — {label}",
        currency=currency,
        period=(v.base.start, v.current.end),
        kpis=kpis,
        sections=[
            Section("Bridge", bridge, money_columns=("amount",)),
            Section("By service", services, money_columns=money),
            Section(
                "Largest line items",
                lines,
                money_columns=money,
                note="Line = service × usage type × unit.",
            ),
        ],
    )


def commitments_report(
    plans: Sequence[PlanEvaluation], *, currency: str, fx: float, rate: float, objective: str
) -> Report:
    rows = pd.DataFrame(
        [
            {
                "option": p.option.name,
                "commit_per_hour": p.hourly_commitment * fx,
                "upfront": p.upfront * fx,
                "npv": p.npv_mean * fx,
                "npv_p5": p.npv_p5 * fx,
                "p_loss": p.prob_loss,
                "saving_per_year": p.annual_saving_mean * fx,
                "utilisation": p.utilisation_mean,
                "coverage": p.coverage_mean,
                "hedge_ratio": p.hedge_ratio,
                "break_even_util": p.option.break_even_utilisation,
            }
            for p in plans
        ]
    )
    best = max(plans, key=lambda p: p.npv_p5 if objective == "p5" else p.npv_mean)
    return Report(
        title="Savings Plan analysis",
        currency=currency,
        kpis=[
            Kpi(
                "Best option",
                best.option.name,
                unit="text",
                note=f"objective: {'5th-percentile' if objective == 'p5' else 'expected'} NPV",
            ),
            Kpi(
                "Commit per hour",
                best.hourly_commitment * fx,
                note=f"{best.hedge_ratio:.0%} of current usage",
            ),
            Kpi("NPV", best.npv_mean * fx, note=f"at {rate:.0%} cost of capital"),
            Kpi("P(loss)", best.prob_loss, unit="percent"),
        ],
        sections=[
            Section(
                "Optimal commitment per option",
                rows,
                money_columns=("commit_per_hour", "upfront", "npv", "npv_p5", "saving_per_year"),
                percent_columns=(
                    "p_loss",
                    "utilisation",
                    "coverage",
                    "hedge_ratio",
                    "break_even_util",
                ),
                note="Usage simulated from history (log-normal monthly growth + observed "
                "daily shape). Discounts approximate; intra-day variation ignored.",
            ),
        ],
    )


def fx_report(bridge: FxBridge, stress_result: FxStress | None, *, source: str) -> Report:
    kpis: list[Kpi] = []
    sections = [
        Section(
            "Monthly INR bridge (usage vs FX effect)",
            bridge.table,
            money_columns=("inr", "usage_effect", "fx_effect", "change_inr"),
            note=f"Rates: {source}. Effective rate = INR bill / USD bill (daily conversion).",
        ),
    ]
    if stress_result is not None:
        s = stress_result
        kpis = [
            Kpi("Spot USD/INR", f"{s.spot:.2f}", unit="text"),
            Kpi("Expected month-end", s.expected_inr),
            Kpi(f"INR Cost-at-Risk ({s.level:.0%})", s.car_inr, note="spend and FX both uncertain"),
        ]
        sections.insert(
            0,
            Section(
                "FX stress scenarios (month-end)",
                s.shocks,
                money_columns=("inr_total", "change_vs_expected"),
            ),
        )
    return Report(
        title="FX exposure — USD bill, INR reporting", currency="INR", kpis=kpis, sections=sections
    )
