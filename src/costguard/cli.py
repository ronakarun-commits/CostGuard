"""Command-line interface (the outermost adapter in the ports-and-adapters layout).

Only argument parsing and output live here; business logic belongs in ``costguard.core``.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import typer

from costguard import __version__
from costguard.core.models import DateRange
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

DEFAULT_DB = Path(".costguard") / "costguard.duckdb"

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
