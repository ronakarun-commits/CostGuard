"""Rich rendering helpers shared by CLI commands. Presentation only — no business logic."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
from rich.console import Console
from rich.table import Table

from costguard.core.models import InjectedAnomaly

console = Console()
err_console = Console(stderr=True)


def money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "INR": "₹"}.get(currency, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def summary_table(df: pd.DataFrame, title: str, key_label: str, currency: str = "USD") -> Table:
    total = float(df["cost"].sum()) if not df.empty else 0.0
    table = Table(title=title, title_justify="left", show_footer=True)
    table.add_column(key_label, footer="Total")
    table.add_column("Cost", justify="right", footer=money(total, currency))
    table.add_column("Share", justify="right", footer="100%" if total else "")
    for key, cost in zip(df["key"].astype(str), df["cost"].astype(float), strict=True):
        share = f"{cost / total:.1%}" if total else "-"
        table.add_row(key, money(cost, currency), share)
    return table


def anomalies_table(anomalies: Sequence[InjectedAnomaly], accounts: dict[str, str]) -> Table:
    table = Table(title="Injected anomalies (ground truth)", title_justify="left")
    for col in ("ID", "Kind", "Account"):
        table.add_column(col, no_wrap=True)
    table.add_column("Service", overflow="fold")
    for col in ("Start", "End"):
        table.add_column(col, no_wrap=True)
    table.add_column("Description", overflow="fold")
    for a in anomalies:
        table.add_row(
            a.id,
            a.kind.value,
            accounts.get(a.sub_account_id, a.sub_account_id),
            a.service_name,
            a.start_date.isoformat(),
            a.end_date.isoformat(),
            a.description.split(" — ", 1)[-1],
        )
    return table
