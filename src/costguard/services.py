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
