from __future__ import annotations

import json

import pandas as pd
from rich.console import Console

from costguard.core.reports import Kpi, Report, Section, dashboard
from costguard.interfaces.render import (
    fmt_money,
    fmt_pct,
    render,
    sparkline,
    to_csv,
    to_html,
    to_json,
)


def _report() -> Report:
    idx = pd.date_range("2026-02-01", "2026-03-10")
    return dashboard(pd.DataFrame({"EC2": 100.0, "S3 <b>": 10.0}, index=idx), budget=5000)


def test_formatting_helpers() -> None:
    assert fmt_money(1234.5, "USD") == "$1,234.50"
    assert fmt_money(-5, "INR") == "-₹5.00"
    assert fmt_money(1, "EUR") == "1.00 EUR"
    assert fmt_pct(0.1234) == "+12.3%"
    assert fmt_pct(None) == "–"
    assert fmt_pct(float("nan")) == "–"
    assert sparkline([1, 2, 3]) == "▁▄█"
    assert sparkline([]) == ""


def test_json_is_valid_and_has_no_nan() -> None:
    data = json.loads(to_json(_report()))
    assert data["kpis"][0]["label"] == "Month to date"
    assert data["sections"][0]["rows"][0]["service"] == "EC2"
    assert "NaN" not in to_json(_report())


def test_csv_long_format() -> None:
    lines = to_csv(_report()).splitlines()
    assert lines[0].startswith("section,")
    assert any(line.startswith("Top services") for line in lines)
    assert to_csv(Report("empty", "USD")) == ""


def test_html_is_self_contained_and_escaped() -> None:
    html = to_html(_report())
    assert html.startswith("<!doctype html>")
    assert "<svg" in html
    assert "S3 &lt;b&gt;" in html
    assert "<script" not in html
    assert "prefers-color-scheme" in html


def test_rich_render_all_kpi_units() -> None:
    console = Console(record=True, width=160)
    report = Report(
        "t", "USD",
        kpis=[Kpi("m", 1.0), Kpi("p", 0.5, unit="percent", delta_pct=-0.1),
              Kpi("c", 3.0, unit="count"), Kpi("x", "text", unit="text"), Kpi("n", None)],
        sections=[Section("s", pd.DataFrame({"k": ["a"], "v": [1.5], "change": [-2.0]}),
                          money_columns=("change",), note="note")],
    )  # fmt: skip
    assert render(report, "table", console) is None
    out = console.export_text()
    assert "50.0%" in out
    assert "-$2.00" in out
    assert "text" in out


def test_render_dispatch() -> None:
    console = Console(record=True)
    assert render(_report(), "json", console).startswith("{")  # type: ignore[union-attr]
    assert render(_report(), "csv", console).startswith("section")  # type: ignore[union-attr]
