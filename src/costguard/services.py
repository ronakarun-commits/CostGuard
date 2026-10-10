"""Application services: one function per use case, shared by the CLI, the HTTP API and the
dashboard. Each loads what it needs from the repository, calls the pure core, and returns a
format-neutral ``Report``. Interfaces only parse input and render output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from costguard.core.models import DateRange
from costguard.core.reports import Report
from costguard.core.resources import Inventory
from costguard.errors import ConfigError, NoDataError
from costguard.storage.duckdb_repo import DuckDBRepository, GroupBy

Freq = Literal["day", "week", "month"]
Method = Literal["baseline", "stl", "iforest"]
Model = Literal["ets", "naive"]


@dataclass(frozen=True, slots=True)
class Display:
    """Reporting currency and the USD -> currency multiplier."""

    currency: str = "USD"
    fx: float = 1.0


def require_data(repo: DuckDBRepository) -> tuple[date, date]:
    bounds = repo.date_bounds()
    if bounds is None:
        raise NoDataError(
            "No cost data in the database.",
            "Run `costguard generate` (synthetic) or `costguard pull` (AWS) first.",
        )
    return bounds


def dashboard(repo: DuckDBRepository, display: Display, budget: float | None = None) -> Report:
    from costguard.core.reports import dashboard as build

    require_data(repo)
    wide = repo.daily_series(by="service") * display.fx
    return build(wide, budget=budget * display.fx if budget else None, currency=display.currency)


def trend(
    repo: DuckDBRepository,
    display: Display,
    *,
    by: GroupBy = "service",
    freq: Freq = "month",
    top: int = 5,
    periods: int = 12,
) -> Report:
    from costguard.core.reports import trend as build

    if by in ("date", "month"):
        raise ConfigError("trend --by must be service, account, region or category")
    require_data(repo)
    wide = repo.daily_series(by=by) * display.fx
    return build(wide, freq=freq, top=top, periods=periods, currency=display.currency,
                 dimension=by)  # fmt: skip


def anomalies(
    repo: DuckDBRepository,
    display: Display,
    *,
    method: Method = "baseline",
    last_days: int | None = 30,
    threshold: float = 4.0,
    min_excess: float = 10.0,
) -> Report:
    from costguard.core.analytics.anomaly import DetectorConfig, detect
    from costguard.core.insights import anomalies_report

    lo, hi = require_data(repo)
    since = lo if last_days is None else max(lo, hi - timedelta(days=last_days - 1))
    found = detect(
        repo.costs_frame(),
        method,
        DetectorConfig(threshold=threshold, min_excess=min_excess),
        since=since,
    )
    return anomalies_report(found, method=method, currency=display.currency, fx=display.fx,
                            since=str(since))  # fmt: skip


def forecast(
    repo: DuckDBRepository, display: Display, *, days: int = 30, model: Model = "ets",
    paths: int = 2000, seed: int = 0,
) -> Report:  # fmt: skip
    from costguard.core.analytics.forecast import forecast as run
    from costguard.core.insights import forecast_report

    require_data(repo)
    fc = run(repo.daily_series()["cost"], days, model=model, n_paths=paths, seed=seed)
    return forecast_report(fc, currency=display.currency, fx=display.fx)


def risk(
    repo: DuckDBRepository, display: Display, *, budget: float | None = None,
    level: float = 0.95, model: Model = "ets", calibrate: bool = True, paths: int = 5000,
    seed: int = 0,
) -> Report:  # fmt: skip
    from costguard.core.analytics.risk import month_end_risk
    from costguard.core.insights import risk_report

    require_data(repo)
    result = month_end_risk(repo.daily_series()["cost"], budget=budget, level=level, model=model,
                            n_paths=paths, seed=seed, calibrate=calibrate)  # fmt: skip
    return risk_report(result, currency=display.currency, fx=display.fx, calibrated=calibrate)


def month_range(month: str) -> DateRange:
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise ConfigError(f"month must be YYYY-MM, not {month!r}") from exc
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return DateRange(start, nxt)


@dataclass(frozen=True, slots=True)
class VarianceOutcome:
    report: Report
    mostly_unattributed: bool


def variance(
    repo: DuckDBRepository, display: Display, *, current: str | None = None,
    base: str | None = None, budget: float | None = None,
) -> VarianceOutcome:  # fmt: skip
    from costguard.core.finance.variance import price_volume
    from costguard.core.insights import variance_report

    _, hi = require_data(repo)
    if current is None:  # default: last complete month
        last_full = (
            hi if (hi + timedelta(days=1)).day == 1 else hi.replace(day=1) - timedelta(days=1)
        )
        current = f"{last_full:%Y-%m}"
    cur = month_range(current)
    base_range = (
        month_range(base) if base else month_range(f"{cur.start - timedelta(days=1):%Y-%m}")
    )
    result = price_volume(repo.costs_frame(), base_range, cur)
    t = result.totals
    if t["cost_base"] == 0 and t["cost_current"] == 0:
        raise NoDataError(f"No cost data for {base_range.start:%Y-%m} or {current}.")
    mostly = bool(t["unattributed"]) and abs(t["unattributed"]) > 0.5 * abs(t["change"] or 1)
    report = variance_report(result, currency=display.currency, fx=display.fx, budget=budget)
    return VarianceOutcome(report, mostly)


def audit(
    inventory: Inventory, display: Display, *, rule_ids: list[str] | None = None,
    min_saving: float = 0.0,
) -> tuple[Report, float]:  # fmt: skip
    """Waste report and the total estimated USD saving per month."""
    from costguard.core.reports import audit_report
    from costguard.core.waste import run_rules

    try:
        findings = run_rules(inventory, only=rule_ids, min_saving=min_saving)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    total = sum(f.monthly_saving_usd for f in findings)
    return audit_report(findings, currency=display.currency, fx=display.fx), total


def overview(
    repo: DuckDBRepository, display: Display, *, budget: float | None = None, level: float = 0.95
) -> dict[str, object]:
    """Everything the web dashboard shows, as plain JSON-ready data (money already converted).

    One call instead of eight keeps the page simple and lets the expensive parts (Monte Carlo,
    detector, optimiser) share one database read.
    """
    import pandas as pd

    from costguard.core.analytics.anomaly import detect
    from costguard.core.analytics.risk import month_end_fan
    from costguard.core.finance.commitments import recommend
    from costguard.core.reports import _same_day_last_month
    from costguard.providers.synthetic_inventory import synthetic_inventory

    lo, hi = require_data(repo)
    fx = display.fx
    costs = repo.costs_frame()
    by_service = repo.daily_series(by="service")
    by_account = repo.daily_series(by="account")
    total = by_service.sum(axis=1).asfreq("D", fill_value=0.0)

    def money(v: float) -> float:
        return round(float(v) * fx, 2)

    # --- month-end risk + fan -------------------------------------------------------------------
    risk, fan = month_end_fan(total, budget=budget, level=level, n_paths=4000)
    month_start = pd.Timestamp(hi.replace(day=1))
    mtd_days = total[total.index >= month_start]
    prev_end = _same_day_last_month(hi)
    prev_mtd = float(total[pd.Timestamp(prev_end.replace(day=1)) : pd.Timestamp(prev_end)].sum())
    last_month_total = float(
        total[
            (total.index < month_start) & (total.index >= month_start - pd.offsets.MonthBegin(1))
        ].sum()
    )

    # --- daily history + anomalies --------------------------------------------------------------
    window = total.iloc[-120:]
    since = window.index[0].date()
    incidents = detect(costs, "baseline", since=since)

    # --- services (month to date vs same days last month) ---------------------------------------
    mtd_svc = by_service[by_service.index >= month_start].sum()
    prev_svc = by_service[pd.Timestamp(prev_end.replace(day=1)) : pd.Timestamp(prev_end)].sum()
    svc = pd.DataFrame(
        {"mtd": mtd_svc, "prev": prev_svc.reindex(mtd_svc.index, fill_value=0.0)}
    ).sort_values("mtd", ascending=False)

    # --- accounts by month ----------------------------------------------------------------------
    monthly = by_account.groupby(pd.DatetimeIndex(by_account.index).to_period("M")).sum().iloc[-12:]
    account_order = [str(a) for a in by_account.sum().sort_values(ascending=False).index]
    month_complete = (hi + timedelta(days=1)).day == 1
    current_partial = "" if month_complete else f"{hi:%Y-%m}"

    # --- variance bridge for the last complete month ----------------------------------------------
    variance_data: dict[str, object] | None = None
    try:
        outcome = variance(repo, display)
        bridge = outcome.report.sections[0].frame
        variance_data = {
            "title": outcome.report.title.split("— ", 1)[-1],
            "steps": [
                {"step": str(r["step"]), "amount": round(float(r["amount"]), 2)}
                for r in bridge.to_dict(orient="records")
            ],
            "mostly_unattributed": outcome.mostly_unattributed,
        }
    except (NoDataError, ConfigError):
        variance_data = None

    # --- waste (demo inventory) -----------------------------------------------------------------
    from costguard.core.waste import run_rules

    findings = run_rules(synthetic_inventory(), min_saving=1.0)

    # --- Savings Plans --------------------------------------------------------------------------
    compute = [c for c in ("Amazon Elastic Compute Cloud - Compute", "AWS Lambda", "AWS Fargate")
               if c in by_service.columns]  # fmt: skip
    plans = (
        recommend(by_service[compute].sum(axis=1), n_paths=1000)
        if compute and len(total) >= 60
        else []
    )

    return {
        "meta": {
            "currency": display.currency,
            "fx": fx,
            "as_of": hi.isoformat(),
            "first": lo.isoformat(),
            "days": len(total),
            "accounts": account_order,
            "level": level,
            "budget": money(budget) if budget else None,
            "sources": sorted(costs["source"].unique().tolist()),
        },
        "kpis": {
            "mtd": money(risk.month_to_date),
            "prev_mtd": money(prev_mtd),
            "last_month": money(last_month_total),
            "expected": money(risk.expected),
            "median": money(risk.median),
            "p5": money(risk.p5),
            "car": money(risk.car),
            "es": money(risk.expected_shortfall),
            "breach_probability": risk.breach_probability,
            "remaining_days": risk.remaining_days,
        },
        "fan": {
            "actual": [{"date": _iso(d), "cum": money(v)} for d, v in mtd_days.cumsum().items()],
            "bands": [
                {"date": _iso(d), **{str(k): money(v) for k, v in row.items()}}
                for d, row in fan.iterrows()
            ],
            "month_end": (month_start + pd.offsets.MonthEnd(0)).date().isoformat(),
        },
        "daily": [{"date": _iso(d), "cost": money(v)} for d, v in window.items()],
        "incidents": [
            {
                "start": i.start_date.isoformat(),
                "end": i.end_date.isoformat(),
                "account": i.sub_account_name,
                "service": i.service_name,
                "ratio": round(i.peak_ratio, 1),
                "excess": money(i.excess_cost),
            }
            for i in incidents
        ],
        "services": [
            {"name": str(n), "mtd": money(r["mtd"]), "prev": money(r["prev"])}
            for n, r in svc.iterrows()
        ],
        "accounts_monthly": {
            "accounts": account_order,
            "months": [
                {
                    "month": str(m),
                    "partial": str(m) == current_partial,
                    "values": {a: money(row[a]) for a in account_order},
                }
                for m, row in monthly.iterrows()
            ],
        },
        "variance": variance_data,
        "waste": {
            "demo": True,
            "total": money(sum(f.monthly_saving_usd for f in findings)),
            "findings": [
                {
                    "rule": f.rule_id,
                    "title": f.title,
                    "resource": f.resource_id,
                    "region": f.region,
                    "detail": f.detail,
                    "saving": money(f.monthly_saving_usd),
                    "severity": f.severity,
                    "action": f.action,
                }
                for f in findings
            ],
        },
        "commitments": [
            {
                "option": p.option.name,
                "per_hour": money(p.hourly_commitment),
                "npv": money(p.npv_mean),
                "npv_p5": money(p.npv_p5),
                "p_loss": p.prob_loss,
                "saving_per_year": money(p.annual_saving_mean),
                "utilisation": p.utilisation_mean,
                "hedge_ratio": p.hedge_ratio,
                "upfront": money(p.upfront),
            }
            for p in plans
        ],
    }


def _iso(d: object) -> str:
    import pandas as pd

    return pd.Timestamp(d).date().isoformat()  # type: ignore[arg-type]
