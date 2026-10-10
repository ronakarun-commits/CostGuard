"""Command-line interface (the outermost adapter in the ports-and-adapters layout).

Only argument parsing and output live here; business logic belongs in ``costguard.core``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import typer

from costguard import __version__
from costguard.core.models import DateRange
from costguard.errors import CostGuardError
from costguard.interfaces.console import anomalies_table, console, err_console, summary_table
from costguard.providers.synthetic import ACCOUNTS, SyntheticProvider
from costguard.storage.duckdb_repo import DuckDBRepository, GroupBy

app = typer.Typer(
    name="costguard",
    help="AWS FinOps and cost-risk toolkit.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

aws_app = typer.Typer(help="Inspect the AWS connection (read-only).", no_args_is_help=True)
app.add_typer(aws_app, name="aws")

DEFAULT_DB = Path(".costguard") / "costguard.duckdb"

Detail = Literal["service", "usage_type"]
Metric = Literal["UnblendedCost", "AmortizedCost", "NetUnblendedCost"]


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


ProfileOption = Annotated[
    str | None, typer.Option("--profile", envvar="AWS_PROFILE", help="AWS named profile.")
]
RegionOption = Annotated[str | None, typer.Option("--region", envvar="AWS_REGION")]

DbOption = Annotated[
    Path,
    typer.Option("--db", envvar="COSTGUARD_DB", help="DuckDB file holding cost history."),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"costguard {__version__}")
        raise typer.Exit()


@app.callback()
def main(
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
    """AWS FinOps and cost-risk toolkit."""


@app.command()
def info() -> None:
    """Show build and environment information."""
    import platform

    typer.echo(f"costguard {__version__}")
    typer.echo(f"python    {platform.python_version()} ({platform.system()})")


@app.command()
def generate(
    db: DbOption = DEFAULT_DB,
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
    last = date.fromisoformat(end) if end else date.today() - timedelta(days=1)
    start = last - timedelta(days=days - 1)
    provider = SyntheticProvider(start=start, days=days, seed=seed, anomaly_rate=anomaly_rate)
    period = provider.world
    with console.status("Generating synthetic cost data..."):
        records = provider.get_costs(period)
        anomalies = provider.get_anomalies(period)
    with DuckDBRepository(db) as repo:
        repo.delete_source(provider.name)
        n = repo.upsert_costs(records, source=provider.name)
        repo.upsert_anomalies(anomalies)
    total = sum(r.billed_cost for r in records)
    console.print(
        f"[green]✓[/] {n:,} cost records ({start} → {last}, {len(ACCOUNTS)} accounts, "
        f"${total:,.0f} total) and {len(anomalies)} labelled anomalies written to [bold]{db}[/]"
    )


@app.command()
def show(
    db: DbOption = DEFAULT_DB,
    by: Annotated[GroupBy, typer.Option(help="Dimension to group by.")] = "service",
    last: Annotated[int | None, typer.Option(min=1, help="Only the last N days of data.")] = 30,
    all_time: Annotated[bool, typer.Option("--all", help="Use the full history.")] = False,
) -> None:
    """Summarise stored cost by service, account, region, category, date or month."""
    with DuckDBRepository(db) as repo:
        bounds = repo.date_bounds()
        if bounds is None:
            err_console.print("[red]No cost data.[/] Run [bold]costguard generate[/] first.")
            raise typer.Exit(code=1)
        lo, hi = bounds
        period = None
        label = f"{lo} → {hi}"
        if not all_time and last is not None:
            start = max(lo, hi - timedelta(days=last - 1))
            period = DateRange(start, hi + timedelta(days=1))
            label = f"{start} → {hi}"
        df = repo.summary(by, period)
    console.print(summary_table(df, title=f"Cost by {by} ({label})", key_label=by.title()))


@app.command()
def labels(db: DbOption = DEFAULT_DB) -> None:
    """List the ground-truth anomalies injected into the synthetic data."""
    with DuckDBRepository(db) as repo:
        anomalies = repo.get_anomalies()
    if not anomalies:
        err_console.print("No labelled anomalies stored.")
        raise typer.Exit(code=1)
    names = {a.id: a.name for a in ACCOUNTS}
    console.print(anomalies_table(anomalies, names))


# ---- AWS ---------------------------------------------------------------------------------------


@aws_app.command("whoami")
@handle_errors
def aws_whoami(profile: ProfileOption = None, region: RegionOption = None) -> None:
    """Show which AWS identity costguard would use (free: sts:GetCallerIdentity)."""
    from costguard.providers.aws.session import make_session, whoami

    ident = whoami(make_session(profile, region))
    console.print(f"Account [bold]{ident.account_id}[/]\nARN     {ident.arn}")
    if ident.is_root:
        err_console.print(
            "[bold red]Warning:[/] these are ROOT credentials. Create a read-only IAM user or "
            "SSO role for costguard and delete the root access keys."
        )


@aws_app.command("budgets")
@handle_errors
def aws_budgets(profile: ProfileOption = None, region: RegionOption = None) -> None:
    """List AWS Budgets for the account (free API)."""
    from costguard.providers.aws.budgets import get_budgets
    from costguard.providers.aws.session import make_session, whoami

    session = make_session(profile, region)
    found = get_budgets(session, whoami(session).account_id)
    if not found:
        console.print("No cost budgets found.")
        return
    for b in found:
        console.print(f"{b.name}: {b.amount:,.2f} {b.currency} / {b.period.lower()}")


@app.command()
@handle_errors
def pull(
    db: DbOption = DEFAULT_DB,
    profile: ProfileOption = None,
    region: RegionOption = None,
    days: Annotated[int, typer.Option(min=1, max=365, help="Days of history to pull.")] = 90,
    detail: Annotated[
        Detail, typer.Option(help="service (multi-account) or usage_type (price-volume ready).")
    ] = "service",
    metric: Annotated[Metric, typer.Option(help="Cost metric to store.")] = ("UnblendedCost"),
    refresh: Annotated[bool, typer.Option(help="Ignore the cache (costs $0.01/request).")] = False,
) -> None:
    """Pull daily cost from AWS Cost Explorer into the local database.

    [yellow]Costs $0.01 per Cost Explorer request.[/] Responses are cached, so re-running is free.
    """
    from costguard.providers.aws.cost_explorer import CostExplorerProvider
    from costguard.providers.aws.session import make_session

    today = date.today()
    period = DateRange(today - timedelta(days=days), today)
    session = make_session(profile, region)
    with DuckDBRepository(db) as repo:
        provider = CostExplorerProvider(
            session,
            repo,
            detail=detail,
            metric=metric,
            refresh=refresh,
            today=today,
        )
        with console.status("Querying Cost Explorer..."):
            records = provider.get_costs(period)
        n = repo.upsert_costs(records, source=provider.name)
    console.print(
        f"[green]✓[/] {n:,} records ({period.start} → {period.end - timedelta(days=1)}) saved to "
        f"[bold]{db}[/]. Requests: {provider.paid_requests} paid "
        f"(~${provider.estimated_cost_usd:.2f}), {provider.cached_requests} from cache."
    )
