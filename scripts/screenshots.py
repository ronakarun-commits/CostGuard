"""Regenerate the terminal screenshots and sample HTML reports used in the README.

    uv run python scripts/screenshots.py

Uses the same service functions as the CLI, renders with a recording Rich console, and saves SVG
(crisp, small, and diff-able in git) to docs/img/.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import MONOKAI

from costguard import services
from costguard.interfaces.render import render_rich, to_html
from costguard.providers.synthetic import SyntheticProvider
from costguard.providers.synthetic_inventory import synthetic_inventory
from costguard.storage.duckdb_repo import DuckDBRepository

OUT = Path(__file__).resolve().parents[1] / "docs" / "img"
SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "samples"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    p = SyntheticProvider(date(2025, 10, 10), 365, seed=42)
    disp = services.Display()
    with DuckDBRepository() as repo:
        repo.upsert_costs(p.get_costs(p.world), "synthetic")
        repo.upsert_anomalies(p.get_anomalies(p.world))
        reports = {
            "dashboard": services.dashboard(repo, disp, budget=26000),
            "risk": services.risk(repo, disp, budget=26000),
            "anomalies": services.anomalies(repo, disp, last_days=60),
            "variance": services.variance(repo, disp).report,
        }
    reports["audit"], _ = services.audit(synthetic_inventory(), disp, min_saving=5)

    for name, report in reports.items():
        console = Console(record=True, width=112, force_terminal=True, color_system="truecolor")
        if name in ("variance", "audit"):  # keep screenshots short: KPIs + first table
            report.sections = report.sections[:1]
        render_rich(report, console)
        console.save_svg(str(OUT / f"{name}.svg"), title=f"costguard {name}", theme=MONOKAI)
        (SAMPLES / f"{name}.html").write_text(to_html(report), encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
