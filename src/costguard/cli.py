"""Command-line interface (the outermost adapter in the ports-and-adapters layout).

Only argument parsing, wiring and output live here; every number is computed in ``costguard.core``.

Exit codes: 0 ok · 1 error · 2 bad usage/config · 3 no data · 4 AWS auth · 5 AWS permission ·
6 AWS API error · 10 ``audit --fail-over`` threshold exceeded.
"""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import pandas as pd
import typer
from rich.markup import escape

from costguard import __version__
from costguard.config import Settings, load_settings
from costguard.core.models import DateRange
from costguard.core.reports import Report
from costguard.errors import CostGuardError, NoDataError
from costguard.interfaces.console import anomalies_table, console, err_console, summary_table
from costguard.interfaces.render import Format, render
from costguard.logging_setup import setup_logging
from costguard.providers.synthetic import ACCOUNTS, SyntheticProvider
from costguard.services import Display, require_data
from costguard.storage.duckdb_repo import DuckDBRepository, GroupBy

log = logging.getLogger("costguard")

app = typer.Typer(
    name="costguard",
    help="AWS FinOps and cost-risk toolkit.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
aws_app = typer.Typer(help="Inspect the AWS connection (read-only).", no_args_is_help=True)
app.add_typer(aws_app, name="aws")

Detail = Literal["service", "usage_type"]
Metric = Literal["UnblendedCost", "AmortizedCost", "NetUnblendedCost"]
Currency = Literal["USD", "INR"]


@dataclass(slots=True)
class State:
    settings: Settings

    @property
    def currency(self) -> str:
        return self.settings.reporting_currency

    @property
    def fx(self) -> float:
        """USD -> reporting currency multiplier (static fallback; Phase 6 uses real rates)."""
        return self.settings.usd_inr_fallback_rate if self.currency == "INR" else 1.0

    @property
    def display(self) -> Display:
        return Display(self.currency, self.fx)

    def repo(self) -> DuckDBRepository:
        return DuckDBRepository(self.settings.db_path)


def handle_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn CostGuardError into a friendly message + a specific exit code (no traceback)."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except CostGuardError as exc:
            # escape(): messages may contain [brackets] (e.g. costguard[api]) that Rich would
            # otherwise treat as markup and silently drop
            err_console.print(f"[bold red]Error:[/] {escape(str(exc))}")
            if exc.hint:
                err_console.print(f"[yellow]Hint:[/] {escape(exc.hint)}")
            raise typer.Exit(code=exc.exit_code) from exc

    return wrapper


def _state(ctx: typer.Context) -> State:
    obj = ctx.find_root().obj
    if not isinstance(obj, State):  # pragma: no cover - main() always sets it
        msg = "CLI state not initialised"
        raise RuntimeError(msg)
    return obj


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"costguard {__version__}")
        raise typer.Exit()


@app.callback()
@handle_errors
def main(
    ctx: typer.Context,
    db: Annotated[
        Path | None, typer.Option("--db", help="DuckDB file. [env: COSTGUARD_DB_PATH]")
    ] = None,
    currency: Annotated[
        Currency | None, typer.Option("--currency", "-c", help="Reporting currency.")
    ] = None,
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v info, -vv debug")
    ] = 0,
    log_json: Annotated[
        bool, typer.Option("--log-json", help="Logs as JSON lines on stderr.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """AWS FinOps and cost-risk toolkit.

    Settings come from flags, COSTGUARD_* env vars, .env, then costguard.toml.
    """  # fmt: skip
    level = {0: None, 1: "INFO"}.get(verbose, "DEBUG")
    settings = load_settings(
        db_path=db, reporting_currency=currency, log_level=level,
        log_format="json" if log_json else None,
    )  # fmt: skip
    setup_logging(settings.log_level, settings.log_format)
    ctx.obj = State(settings)


# ---- shared options --------------------------------------------------------------------------

FormatOption = Annotated[
    Format, typer.Option("--format", "-f", help="Output format.", case_sensitive=False)
]
OutputOption = Annotated[
    Path | None, typer.Option("--output", "-o", help="Write to a file instead of stdout.")
]
ProfileOption = Annotated[
    str | None, typer.Option("--profile", envvar="AWS_PROFILE", help="AWS named profile.")
]
RegionOption = Annotated[str | None, typer.Option("--region", envvar="AWS_REGION")]


def _emit(report: Report, fmt: Format, output: Path | None) -> None:
    text = render(report, fmt, console)
    if text is None:
        return
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        err_console.print(f"[green]✓[/] wrote {output}")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


# ---- general ---------------------------------------------------------------------------------


@app.command()
def info(ctx: typer.Context) -> None:
    """Show version, environment and effective settings (secrets hidden)."""
    import platform

    s = _state(ctx).settings
    typer.echo(f"costguard {__version__}")
    typer.echo(f"python    {platform.python_version()} ({platform.system()})")
    typer.echo(f"database  {s.db_path}")
    typer.echo(f"currency  {s.reporting_currency}")
    typer.echo(f"budget    {s.budget_monthly_usd or '-'}")
    typer.echo(f"regions   {', '.join(s.aws_regions)}")


@app.command()
@handle_errors
def generate(
    ctx: typer.Context,
    days: Annotated[int, typer.Option(min=1, max=3650, help="Days of history.")] = 365,
    seed: Annotated[int, typer.Option(help="Random seed (same seed = same data).")] = 42,
    end: Annotated[
        str | None, typer.Option(help="Last day of data, YYYY-MM-DD (default: yesterday).")
    ] = None,
    anomaly_rate: Annotated[
        float, typer.Option(min=0.0, max=1.0, help="Expected anomalies per day.")
    ] = 1 / 30,
) -> None:
    """Generate synthetic multi-account AWS cost data with labelled anomalies."""
    state = _state(ctx)
    last = date.fromisoformat(end) if end else date.today() - timedelta(days=1)
    start = last - timedelta(days=days - 1)
    provider = SyntheticProvider(start=start, days=days, seed=seed, anomaly_rate=anomaly_rate)
    with console.status("Generating synthetic cost data..."):
        records = provider.get_costs(provider.world)
        anomalies = provider.get_anomalies(provider.world)
    with state.repo() as repo:
        repo.delete_source(provider.name)
        n = repo.upsert_costs(records, source=provider.name)
        repo.upsert_anomalies(anomalies)
    total = sum(r.billed_cost for r in records)
    log.info("generated", extra={"records": n, "anomalies": len(anomalies), "seed": seed})
    console.print(
        f"[green]✓[/] {n:,} cost records ({start} → {last}, {len(ACCOUNTS)} accounts, "
        f"${total:,.0f} total) and {len(anomalies)} labelled anomalies written to "
        f"[bold]{state.settings.db_path}[/]"
    )


@app.command()
@handle_errors
def show(
    ctx: typer.Context,
    by: Annotated[GroupBy, typer.Option(help="Dimension to group by.")] = "service",
    last: Annotated[int, typer.Option(min=1, help="Only the last N days of data.")] = 30,
    all_time: Annotated[bool, typer.Option("--all", help="Use the full history.")] = False,
) -> None:
    """Summarise stored cost by service, account, region, category, date or month."""
    state = _state(ctx)
    with state.repo() as repo:
        lo, hi = require_data(repo)
        period = None
        label = f"{lo} → {hi}"
        if not all_time:
            start = max(lo, hi - timedelta(days=last - 1))
            period = DateRange(start, hi + timedelta(days=1))
            label = f"{start} → {hi}"
        df = repo.summary(by, period)
    df["cost"] = df["cost"] * state.fx
    console.print(
        summary_table(df, title=f"Cost by {by} ({label})", key_label=by.title(),
                      currency=state.currency)
    )  # fmt: skip


@app.command()
@handle_errors
def labels(ctx: typer.Context) -> None:
    """List the ground-truth anomalies injected into the synthetic data."""
    with _state(ctx).repo() as repo:
        anomalies = repo.get_anomalies()
    if not anomalies:
        raise NoDataError("No labelled anomalies stored.", "Run `costguard generate` first.")
    console.print(anomalies_table(anomalies, {a.id: a.name for a in ACCOUNTS}))


# ---- reports ---------------------------------------------------------------------------------


@app.command()
@handle_errors
def dashboard(
    ctx: typer.Context,
    budget: Annotated[
        float | None, typer.Option(min=0, help="Monthly budget in USD (overrides config).")
    ] = None,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Month-to-date spend, run-rate forecast, budget use, top services and biggest movers."""
    from costguard import services

    state = _state(ctx)
    with state.repo() as repo:
        report = services.dashboard(repo, state.display,
                                    budget or state.settings.budget_monthly_usd)  # fmt: skip
    _emit(report, fmt, output)


@app.command()
@handle_errors
def trend(
    ctx: typer.Context,
    by: Annotated[GroupBy, typer.Option(help="Dimension.")] = "service",
    freq: Annotated[Literal["day", "week", "month"], typer.Option(help="Period length.")] = "month",
    top: Annotated[int, typer.Option(min=1, max=20, help="Keys shown before 'Other'.")] = 5,
    periods: Annotated[int, typer.Option(min=2, max=120, help="Number of periods.")] = 12,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Cost per day/week/month for the top services (or accounts, regions...)."""
    from costguard import services

    state = _state(ctx)
    with state.repo() as repo:
        report = services.trend(repo, state.display, by=by, freq=freq, top=top, periods=periods)
    _emit(report, fmt, output)


@app.command()
@handle_errors
def audit(
    ctx: typer.Context,
    source: Annotated[
        Literal["synthetic", "aws"], typer.Option(help="Where to read the inventory from.")
    ] = "synthetic",
    profile: ProfileOption = None,
    regions: Annotated[
        str | None, typer.Option(help="Comma-separated regions (AWS only; default from config).")
    ] = None,
    with_cpu: Annotated[
        bool, typer.Option(help="Fetch 14-day CPU from CloudWatch (AWS; may cost a little).")
    ] = False,
    rule_ids: Annotated[
        list[str] | None, typer.Option("--rule", help="Only run these rule ids (repeatable).")
    ] = None,
    min_saving: Annotated[
        float, typer.Option(min=0, help="Hide findings below this USD/month.")
    ] = 0,
    fail_over: Annotated[
        float | None,
        typer.Option(help="Exit 10 if total saving/month exceeds this (USD). For CI/cron."),
    ] = None,
    seed: Annotated[int, typer.Option(help="Synthetic inventory seed.")] = 42,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Find waste (idle volumes, idle IPs, old snapshots, oversized/old instances), ranked by $."""
    from costguard import services

    state = _state(ctx)
    if source == "aws":
        from costguard.providers.aws.resources import Ec2InventoryProvider
        from costguard.providers.aws.session import make_session, whoami

        session = make_session(profile or state.settings.aws_profile)
        region_list = regions.split(",") if regions else state.settings.aws_regions
        with console.status("Collecting EC2 inventory..."):
            inventory = Ec2InventoryProvider(session, region_list, with_cpu=with_cpu).collect(
                whoami(session).account_id
            )
    else:
        from costguard.providers.synthetic_inventory import synthetic_inventory

        inventory = synthetic_inventory(seed)
    report, total = services.audit(inventory, state.display, rule_ids=rule_ids,
                                   min_saving=min_saving)  # fmt: skip
    _emit(report, fmt, output)
    if fail_over is not None and total > fail_over:
        err_console.print(f"[red]Potential saving ${total:,.2f}/month exceeds ${fail_over:,.2f}[/]")
        raise typer.Exit(code=10)


@app.command("rules")
def list_rules() -> None:
    """List the available waste rules."""
    from rich.table import Table

    from costguard.core.waste import REGISTRY

    table = Table(title="Waste rules", title_justify="left")
    table.add_column("Id", no_wrap=True)
    table.add_column("Title")
    table.add_column("What it checks")
    for spec in REGISTRY.values():
        table.add_row(spec.id, spec.title, spec.description)
    console.print(table)


# ---- AWS -------------------------------------------------------------------------------------


@aws_app.command("whoami")
@handle_errors
def aws_whoami(
    ctx: typer.Context, profile: ProfileOption = None, region: RegionOption = None
) -> None:
    """Show which AWS identity costguard would use (free: sts:GetCallerIdentity)."""
    from costguard.providers.aws.session import make_session, whoami

    ident = whoami(make_session(profile or _state(ctx).settings.aws_profile, region))
    console.print(f"Account [bold]{ident.account_id}[/]\nARN     {ident.arn}")
    if ident.is_root:
        err_console.print(
            "[bold red]Warning:[/] these are ROOT credentials. Create a read-only IAM user or "
            "SSO role for costguard and delete the root access keys."
        )


@aws_app.command("budgets")
@handle_errors
def aws_budgets(
    ctx: typer.Context, profile: ProfileOption = None, region: RegionOption = None
) -> None:
    """List AWS Budgets for the account (free API)."""
    from costguard.providers.aws.budgets import get_budgets
    from costguard.providers.aws.session import make_session, whoami

    session = make_session(profile or _state(ctx).settings.aws_profile, region)
    found = get_budgets(session, whoami(session).account_id)
    if not found:
        console.print("No cost budgets found.")
        return
    for b in found:
        console.print(f"{b.name}: {b.amount:,.2f} {b.currency} / {b.period.lower()}")


@app.command()
@handle_errors
def pull(
    ctx: typer.Context,
    profile: ProfileOption = None,
    region: RegionOption = None,
    days: Annotated[int, typer.Option(min=1, max=365, help="Days of history to pull.")] = 90,
    detail: Annotated[
        Detail, typer.Option(help="service (multi-account) or usage_type (price-volume ready).")
    ] = "service",
    metric: Annotated[Metric, typer.Option(help="Cost metric to store.")] = "UnblendedCost",
    refresh: Annotated[bool, typer.Option(help="Ignore the cache (costs $0.01/request).")] = False,
) -> None:
    """Pull daily cost from AWS Cost Explorer into the local database.

    [yellow]Costs $0.01 per Cost Explorer request.[/] Responses are cached, so re-running is free.
    """
    from costguard.providers.aws.cost_explorer import CostExplorerProvider
    from costguard.providers.aws.session import make_session

    state = _state(ctx)
    today = date.today()
    period = DateRange(today - timedelta(days=days), today)
    session = make_session(profile or state.settings.aws_profile, region)
    with state.repo() as repo:
        provider = CostExplorerProvider(
            session, repo, detail=detail, metric=metric, refresh=refresh, today=today
        )
        with console.status("Querying Cost Explorer..."):
            records = provider.get_costs(period)
        n = repo.upsert_costs(records, source=provider.name)
    console.print(
        f"[green]✓[/] {n:,} records ({period.start} → {period.end - timedelta(days=1)}) saved to "
        f"[bold]{state.settings.db_path}[/]. Requests: {provider.paid_requests} paid "
        f"(~${provider.estimated_cost_usd:.2f}), {provider.cached_requests} from cache."
    )


# ---- analytics (Phase 5) ---------------------------------------------------------------------

MethodOption = Annotated[
    Literal["baseline", "stl", "iforest"],
    typer.Option("--method", "-m", help="baseline (causal, live), stl (retrospective), iforest."),
]
ModelOption = Annotated[
    Literal["ets", "naive"], typer.Option("--model", help="ets (default) or naive benchmark.")
]
LevelOption = Annotated[
    float, typer.Option("--level", min=0.8, max=0.995, help="Confidence level, e.g. 0.95.")
]


@app.command()
@handle_errors
def anomalies(
    ctx: typer.Context,
    method: MethodOption = "baseline",
    last: Annotated[int, typer.Option(min=1, help="Report incidents from the last N days.")] = 30,
    all_time: Annotated[bool, typer.Option("--all", help="Whole history.")] = False,
    threshold: Annotated[float, typer.Option(min=1, help="Robust z-score threshold.")] = 4.0,
    min_excess: Annotated[float, typer.Option(min=0, help="Min USD/day above expected.")] = 10.0,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Detect unusual spend per account and service."""
    from costguard import services

    state = _state(ctx)
    with state.repo() as repo, console.status(f"Scoring with {method}..."):
        report = services.anomalies(repo, state.display, method=method,
                                    last_days=None if all_time else last, threshold=threshold,
                                    min_excess=min_excess)  # fmt: skip
    _emit(report, fmt, output)


@app.command()
@handle_errors
def evaluate(
    ctx: typer.Context,
    methods: Annotated[
        list[str] | None, typer.Option("--method", "-m", help="Methods to compare (default all).")
    ] = None,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Score anomaly detectors against the labelled synthetic anomalies (precision/recall/F1)."""
    from costguard.core.analytics.anomaly import METHODS, detect
    from costguard.core.analytics.anomaly import evaluate as score
    from costguard.core.insights import evaluation_report

    state = _state(ctx)
    chosen = methods or list(METHODS)
    bad = set(chosen) - set(METHODS)
    if bad:
        raise typer.BadParameter(f"unknown method(s): {', '.join(sorted(bad))}", param_hint="-m")
    with state.repo() as repo:
        require_data(repo)
        costs = repo.costs_frame()
        labels = repo.get_anomalies()
    if not labels:
        raise NoDataError("No labelled anomalies.", "Evaluation needs `costguard generate` data.")
    results = []
    for m in chosen:
        with console.status(f"Evaluating {m}..."):
            results.append(score(detect(costs, m), labels, m))  # type: ignore[arg-type]
    _emit(evaluation_report(results), fmt, output)


@app.command()
@handle_errors
def forecast(
    ctx: typer.Context,
    days: Annotated[int, typer.Option(min=1, max=180, help="Days ahead.")] = 30,
    model: ModelOption = "ets",
    paths: Annotated[int, typer.Option(min=100, max=20000, help="Monte Carlo paths.")] = 2000,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 0,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Probabilistic forecast of total daily spend (median and 90% interval)."""
    from costguard import services

    state = _state(ctx)
    with state.repo() as repo, console.status("Simulating..."):
        report = services.forecast(repo, state.display, days=days, model=model, paths=paths,
                                   seed=seed)  # fmt: skip
    _emit(report, fmt, output)


@app.command()
@handle_errors
def risk(
    ctx: typer.Context,
    budget: Annotated[float | None, typer.Option(min=0, help="Monthly budget in USD.")] = None,
    level: LevelOption = 0.95,
    model: ModelOption = "ets",
    calibrate: Annotated[
        bool, typer.Option(help="Conformally calibrate bounds on past forecast errors.")
    ] = True,
    paths: Annotated[int, typer.Option(min=500, max=50000, help="Monte Carlo paths.")] = 5000,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 0,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Month-end Cost-at-Risk, Expected Shortfall and probability of breaching the budget."""
    from costguard import services

    state = _state(ctx)
    with state.repo() as repo, console.status("Simulating month-end..."):
        report = services.risk(
            repo,
            state.display,
            budget=budget or state.settings.budget_monthly_usd,
            level=level,
            model=model,
            calibrate=calibrate,
            paths=paths,
            seed=seed,
        )
    _emit(report, fmt, output)


@app.command()
@handle_errors
def backtest(
    ctx: typer.Context,
    level: LevelOption = 0.95,
    model: ModelOption = "ets",
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Backtest risk bounds (Kupiec test, raw vs calibrated) and month-end forecast accuracy."""
    from costguard.core.analytics.risk import kupiec_pof, month_end_backtest, var_backtest
    from costguard.core.insights import backtest_report

    state = _state(ctx)
    with state.repo() as repo:
        require_data(repo)
        series = repo.daily_series()["cost"]
    if len(series) < 150:
        raise NoDataError("Backtesting needs at least 150 days of history.")
    with console.status("Backtesting (rolling re-fits)..."):
        df, cal = var_backtest(series, level=level, model=model)
        raw = kupiec_pof(int((df["actual"] > df["raw_bound"]).sum()), len(df), 1 - level)
        month = month_end_backtest(series, model=model, level=level)
    _emit(backtest_report(raw, cal, month, level=level, model=model), fmt, output)


# ---- finance (Phase 6) -----------------------------------------------------------------------


@app.command()
@handle_errors
def variance(
    ctx: typer.Context,
    current: Annotated[str | None, typer.Option(help="Month to explain, YYYY-MM.")] = None,
    base: Annotated[str | None, typer.Option(help="Comparison month, YYYY-MM.")] = None,
    budget: Annotated[float | None, typer.Option(min=0, help="Monthly budget in USD.")] = None,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Explain a month's change: calendar, volume (usage) and price (rate) effects."""
    from costguard import services

    state = _state(ctx)
    with state.repo() as repo:
        outcome = services.variance(repo, state.display, current=current, base=base,
                                    budget=budget or state.settings.budget_monthly_usd)  # fmt: skip
    if outcome.mostly_unattributed:
        err_console.print(
            "[yellow]Most of the change is unattributed:[/] this data has mixed usage units. "
            "Pull with `costguard pull --detail usage_type` for a price/volume split."
        )
    _emit(outcome.report, fmt, output)


COMMITMENT_SERVICES = ("Amazon Elastic Compute Cloud - Compute", "AWS Lambda", "AWS Fargate")


@app.command()
@handle_errors
def commitments(
    ctx: typer.Context,
    rate: Annotated[float, typer.Option(min=0, max=1, help="Annual cost of capital.")] = 0.10,
    objective: Annotated[
        Literal["p5", "mean"],
        typer.Option(help="p5 = robust (5th-percentile NPV), mean = expected NPV."),
    ] = "p5",
    paths: Annotated[int, typer.Option(min=200, max=20000, help="Monte Carlo paths.")] = 2000,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 0,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """Size a Compute Savings Plan: optimal commitment, NPV and risk for each term/payment."""
    from costguard.core.finance.commitments import recommend
    from costguard.core.insights import commitments_report

    state = _state(ctx)
    with state.repo() as repo:
        require_data(repo)
        wide = repo.daily_series(by="service")
    cols = [c for c in COMMITMENT_SERVICES if c in wide.columns]
    if not cols:
        raise NoDataError("No Savings-Plan-eligible compute spend (EC2, Lambda, Fargate) found.")
    if len(wide) < 60:
        raise NoDataError("Need at least 60 days of history to model usage.")
    with console.status("Simulating usage and optimising commitments..."):
        plans = recommend(wide[cols].sum(axis=1), annual_rate=rate, objective=objective,
                          n_paths=paths, seed=seed)  # fmt: skip
    _emit(commitments_report(plans, currency=state.currency, fx=state.fx, rate=rate,
                             objective=objective), fmt, output)  # fmt: skip


@app.command()
@handle_errors
def fx(
    ctx: typer.Context,
    fx_source: Annotated[
        Literal["ecb", "synthetic"],
        typer.Option("--fx-source", help="ecb = real ECB rates (internet), synthetic = offline."),
    ] = "ecb",
    refresh: Annotated[bool, typer.Option(help="Re-download rates even if cached.")] = False,
    stress_pct: Annotated[
        list[float] | None,
        typer.Option("--stress", help="INR depreciation shocks in %, repeatable (default 5, 10)."),
    ] = None,
    level: LevelOption = 0.95,
    fmt: FormatOption = "table",
    output: OutputOption = None,
) -> None:
    """USD bill in INR: monthly usage-vs-FX bridge and month-end FX stress test."""
    from costguard.core.analytics.forecast import forecast as run
    from costguard.core.analytics.forecast import month_end_horizon
    from costguard.core.finance.fx import (
        daily_rates,
        fx_vol,
        monthly_bridge,
        stress,
        synthetic_rates,
    )
    from costguard.core.insights import fx_report

    with _state(ctx).repo() as repo:
        lo, hi = require_data(repo)
        series = repo.daily_series()["cost"]
        rates, source = repo.get_fx("USD", "INR")
        have = (not rates.empty and rates.index.min() <= pd.Timestamp(lo) + pd.Timedelta(days=5)
                and rates.index.max() >= pd.Timestamp(hi) - pd.Timedelta(days=5))  # fmt: skip
        if refresh or not have or (source or "") != fx_source:
            if fx_source == "ecb":
                from costguard.providers.fx_ecb import fetch_rates

                with console.status("Downloading ECB USD/INR rates..."):
                    rates = fetch_rates(lo - timedelta(days=7), hi)
            else:
                rates = synthetic_rates(pd.Timestamp(lo) - pd.Timedelta(days=7), pd.Timestamp(hi))
            repo.upsert_fx(rates, "USD", "INR", fx_source)
            source = fx_source
    bridge = monthly_bridge(series, rates)
    as_of = hi
    remaining = month_end_horizon(as_of)
    s_full = series.asfreq("D", fill_value=0.0)
    month_mask = s_full.index >= pd.Timestamp(as_of.replace(day=1))
    r_daily = daily_rates(rates, pd.DatetimeIndex(s_full.index))
    mtd_inr = float((s_full[month_mask] * r_daily[month_mask]).sum())
    spot = float(r_daily.iloc[-1])
    rest = run(s_full, remaining, n_paths=4000).totals() if remaining > 0 else np.zeros(4000)
    shocks = tuple(p / 100 for p in (stress_pct or [5.0, 10.0]))
    result = stress(mtd_inr, rest, spot, fx_vol(rates), remaining, shocks=shocks, level=level)
    label = (
        "ECB reference rates (frankfurter.dev)" if source == "ecb" else "synthetic (offline demo)"
    )
    _emit(fx_report(bridge, result, source=label), fmt, output)


# ---- interfaces (Phase 7) --------------------------------------------------------------------


@app.command()
@handle_errors
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    no_auth: Annotated[
        bool, typer.Option("--no-auth", help="Disable API keys (only allowed on localhost).")
    ] = False,
) -> None:
    """Run the read-only HTTP API (needs `costguard[api]`). Docs at /docs."""
    from costguard.errors import ConfigError

    try:
        import uvicorn

        from costguard.interfaces.api import create_app
    except ImportError as exc:
        raise ConfigError("The API needs extra packages.", 'pip install "costguard[api]"') from exc
    if no_auth and host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("--no-auth is only allowed when binding to localhost.")
    api = create_app(_state(ctx).settings, require_auth=not no_auth)
    uvicorn.run(api, host=host, port=port, log_level="info")


@app.command()
@handle_errors
def ui(
    ctx: typer.Context,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8501,
) -> None:
    """Open the Streamlit dashboard (needs `costguard[dashboard]`)."""
    import os
    import shutil
    import subprocess
    from importlib.util import find_spec

    from costguard.errors import ConfigError

    if find_spec("streamlit") is None:
        raise ConfigError(
            "The dashboard needs extra packages.", 'pip install "costguard[dashboard]"'
        )
    script = Path(__file__).parent / "interfaces" / "dashboard_app.py"
    env = {**os.environ, "COSTGUARD_DB_PATH": str(_state(ctx).settings.db_path.resolve())}
    exe = shutil.which("streamlit") or "streamlit"
    raise typer.Exit(
        subprocess.call(  # noqa: S603 - fixed arguments, no shell
            [exe, "run", str(script), "--server.port", str(port), "--server.headless", "false",
             "--browser.gatherUsageStats", "false"],
            env=env,
        )
    )  # fmt: skip


@app.command()
@handle_errors
def alert(
    ctx: typer.Context,
    days: Annotated[int, typer.Option(min=1, help="Look for anomalies in the last N days.")] = 2,
    budget: Annotated[float | None, typer.Option(min=0, help="Monthly budget in USD.")] = None,
    breach_threshold: Annotated[
        float, typer.Option(min=0, max=1, help="Alert if P(over budget) is at least this.")
    ] = 0.5,
    dry_run: Annotated[bool, typer.Option(help="Print the alert instead of posting it.")] = False,
) -> None:
    """Check for new anomalies and budget risk; post to Slack if anything needs attention.

    Designed for cron / scheduled CI. Exit code 0 = all clear, 11 = alerts raised.
    """
    from costguard.core.analytics.anomaly import detect
    from costguard.core.analytics.risk import month_end_risk
    from costguard.interfaces.notify import build_alerts, post_slack, slack_payload

    state = _state(ctx)
    budget_usd = budget or state.settings.budget_monthly_usd
    with state.repo() as repo:
        _, hi = require_data(repo)
        incidents = detect(repo.costs_frame(), "baseline", since=hi - timedelta(days=days - 1))
        risk_result = (
            month_end_risk(repo.daily_series()["cost"], budget=budget_usd, n_paths=3000)
            if budget_usd
            else None
        )
    alerts = build_alerts(incidents, risk_result, breach_threshold=breach_threshold,
                          currency=state.currency, fx=state.fx)  # fmt: skip
    if not alerts:
        console.print("[green]✓[/] All clear: no new anomalies, budget risk below threshold.")
        return
    webhook = state.settings.slack_webhook_url
    if dry_run or webhook is None:
        if webhook is None and not dry_run:
            err_console.print("[yellow]COSTGUARD_SLACK_WEBHOOK_URL not set; printing instead.[/]")
        import json

        sys.stdout.write(json.dumps(slack_payload(alerts), indent=2, ensure_ascii=False) + "\n")
    else:
        post_slack(webhook.get_secret_value(), alerts)
        console.print(f"[green]✓[/] Posted {len(alerts)} alert(s) to Slack.")
    raise typer.Exit(code=11)
