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

import typer

from costguard import __version__
from costguard.config import Settings, load_settings
from costguard.core.models import DateRange
from costguard.core.reports import Report
from costguard.errors import CostGuardError, NoDataError
from costguard.interfaces.console import anomalies_table, console, err_console, summary_table
from costguard.interfaces.render import Format, render
from costguard.logging_setup import setup_logging
from costguard.providers.synthetic import ACCOUNTS, SyntheticProvider
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

    def repo(self) -> DuckDBRepository:
        return DuckDBRepository(self.settings.db_path)


def handle_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn CostGuardError into a friendly message + a specific exit code (no traceback)."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except CostGuardError as exc:
            err_console.print(f"[bold red]Error:[/] {exc}")
            if exc.hint:
                err_console.print(f"[yellow]Hint:[/] {exc.hint}")
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


def _require_data(repo: DuckDBRepository) -> tuple[date, date]:
    bounds = repo.date_bounds()
    if bounds is None:
        raise NoDataError(
            "No cost data in the database.",
            "Run `costguard generate` (synthetic) or `costguard pull` (AWS) first.",
        )
    return bounds


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
        lo, hi = _require_data(repo)
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
    from costguard.core.reports import dashboard as build

    state = _state(ctx)
    with state.repo() as repo:
        _require_data(repo)
        wide = repo.daily_series(by="service") * state.fx
    budget_usd = budget or state.settings.budget_monthly_usd
    report = build(wide, budget=budget_usd * state.fx if budget_usd else None,
                   currency=state.currency)  # fmt: skip
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
    from costguard.core.reports import trend as build

    if by in ("date", "month"):
        raise typer.BadParameter("use service, account, region or category", param_hint="--by")
    state = _state(ctx)
    with state.repo() as repo:
        _require_data(repo)
        wide = repo.daily_series(by=by) * state.fx
    report = build(wide, freq=freq, top=top, periods=periods, currency=state.currency,
                   dimension=by)  # fmt: skip
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
    from costguard.core.reports import audit_report
    from costguard.core.waste import run_rules

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
    try:
        findings = run_rules(inventory, only=rule_ids, min_saving=min_saving)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--rule") from exc
    _emit(audit_report(findings, currency=state.currency, fx=state.fx), fmt, output)
    total = sum(f.monthly_saving_usd for f in findings)
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
