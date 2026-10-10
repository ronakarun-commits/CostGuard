"""Render a ``Report`` as a Rich table, JSON, CSV or a self-contained HTML page."""

from __future__ import annotations

import html
import io
import json
import math
from importlib.resources import files
from typing import Any, Literal

import pandas as pd
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from costguard.core.reports import Kpi, Report, Section

Format = Literal["table", "json", "csv", "html"]

_SYMBOL = {"USD": "$", "INR": "₹"}
_BARS = "▁▂▃▄▅▆▇█"


def fmt_money(value: float, currency: str) -> str:
    sym = _SYMBOL.get(currency, "")
    sign = "-" if value < 0 else ""
    return f"{sign}{sym}{abs(value):,.2f}" if sym else f"{value:,.2f} {currency}"


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    return f"{value:+.1%}" if signed else f"{value:.1%}"


def sparkline(values: pd.Series | list[float]) -> str:
    vals = [float(v) for v in values]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    return "".join(_BARS[min(7, int((v - lo) / span * 7.999))] for v in vals)


def _kpi_value(k: Kpi, currency: str) -> str:
    if k.value is None:
        return "–"
    if k.unit == "money":
        return fmt_money(float(k.value), currency)
    if k.unit == "percent":
        return fmt_pct(float(k.value), signed=False)
    if k.unit == "count":
        return f"{float(k.value):,.0f}"
    return str(k.value)


def _cell(section: Section, col: str, value: Any, currency: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    if col in section.money_columns:
        return fmt_money(float(value), currency)
    if col in section.percent_columns:
        return fmt_pct(float(value), signed=col.endswith("change_pct"))
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _heading(col: str) -> str:
    words = col.replace("_pct", " %").replace("_", " ").strip().title()
    return words.replace("Mtd", "MTD").replace("7D", "7d")


# ---- Rich ----------------------------------------------------------------------------------------


def render_rich(report: Report, console: Console) -> None:
    parts: list[Any] = []
    if report.kpis:
        grid = Table.grid(padding=(0, 3))
        for _ in report.kpis:
            grid.add_column()
        labels, values, notes = [], [], []
        for k in report.kpis:
            labels.append(Text(k.label, style="dim"))
            v = Text(_kpi_value(k, report.currency), style="bold")
            if k.delta_pct is not None:
                v.append(f"  {fmt_pct(k.delta_pct)}", style="red" if k.delta_pct > 0 else "green")
            values.append(v)
            notes.append(Text(k.note, style="dim italic"))
        grid.add_row(*labels)
        grid.add_row(*values)
        grid.add_row(*notes)
        parts.append(grid)
    if report.series is not None and len(report.series) > 1:
        parts.append(Text(f"Daily cost  {sparkline(report.series)}", style="cyan"))
    period = f"{report.period[0]} → {report.period[1]}" if report.period else ""
    console.print(Panel(Group(*parts) if parts else "", title=report.title, subtitle=period))

    for s in report.sections:
        table = Table(title=s.title, title_justify="left", caption=s.note or None)
        for col in s.frame.columns:
            numeric = col in s.money_columns or col in s.percent_columns
            table.add_column(_heading(str(col)), justify="right" if numeric else "left")
        for row in s.frame.itertuples(index=False):
            cells = []
            for col, val in zip(s.frame.columns, row, strict=True):
                text = _cell(s, str(col), val, report.currency)
                style = ""
                if str(col).startswith("change") and isinstance(val, float) and not math.isnan(val):
                    style = "red" if val > 0 else "green" if val < 0 else ""
                cells.append(Text(text, style=style))
            table.add_row(*cells)
        console.print(table)


# ---- JSON / CSV ----------------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item())
    return value


def to_dict(report: Report) -> dict[str, Any]:
    return {
        "title": report.title,
        "currency": report.currency,
        "period": [d.isoformat() for d in report.period] if report.period else None,
        "generated_at": report.generated_at.isoformat(),
        "kpis": [
            {"label": k.label, "value": _clean(k.value), "unit": k.unit,
             "delta_pct": _clean(k.delta_pct), "note": k.note}
            for k in report.kpis
        ],
        "sections": [
            {"title": s.title, "note": s.note,
             "rows": [{str(k): _clean(v) for k, v in r.items()}
                      for r in s.frame.to_dict(orient="records")]}
            for s in report.sections
        ],
    }  # fmt: skip


def to_json(report: Report) -> str:
    return json.dumps(to_dict(report), indent=2, ensure_ascii=False)


def to_csv(report: Report) -> str:
    """All sections stacked in long format with a leading ``section`` column."""
    frames = [s.frame.assign(section=s.title) for s in report.sections]
    if not frames:
        return ""
    df = pd.concat(frames, ignore_index=True)
    df = df[["section", *[c for c in df.columns if c != "section"]]]
    buf = io.StringIO()
    df.to_csv(buf, index=False, float_format="%.4f", lineterminator="\n")
    return buf.getvalue()


# ---- HTML ----------------------------------------------------------------------------------------

_CSS = (files("costguard.interfaces") / "report.css").read_text(encoding="utf-8")


def _svg_line(series: pd.Series) -> str:
    vals = [float(v) for v in series]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    w, h = 1000, 80
    pts = " ".join(
        f"{i * w / (len(vals) - 1):.1f},{h - 4 - (v - lo) / span * (h - 8):.1f}"
        for i, v in enumerate(vals)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        f'aria-label="Daily cost trend"><polyline fill="none" stroke="var(--accent)" '
        f'stroke-width="2" vector-effect="non-scaling-stroke" points="{pts}"/></svg>'
    )


def to_html(report: Report) -> str:
    e = html.escape
    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{e(report.title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{e(report.title)}</h1>",
    ]
    period = f"{report.period[0]} → {report.period[1]} · " if report.period else ""
    out.append(
        f"<div class='sub'>{e(period)}{e(report.currency)} · generated "
        f"{report.generated_at:%Y-%m-%d %H:%M} UTC by costguard</div>"
    )
    if report.kpis:
        out.append("<div class='kpis'>")
        for k in report.kpis:
            delta = ""
            if k.delta_pct is not None:
                cls = "up" if k.delta_pct > 0 else "down"
                delta = f" <span class='{cls}' style='font-size:14px'>{fmt_pct(k.delta_pct)}</span>"
            out.append(
                f"<div class='kpi'><div class='l'>{e(k.label)}</div>"
                f"<div class='v'>{e(_kpi_value(k, report.currency))}{delta}</div>"
                f"<div class='n'>{e(k.note)}</div></div>"
            )
        out.append("</div>")
    if report.series is not None and len(report.series) > 1:
        out.append("<h2>Daily cost</h2><div class='wrap' style='padding:8px'>")
        out.append(_svg_line(report.series) + "</div>")
    for s in report.sections:
        out.append(f"<h2>{e(s.title)}</h2><div class='wrap'><table><thead><tr>")
        for col in s.frame.columns:
            num = col in s.money_columns or col in s.percent_columns
            out.append(f"<th class='{'num' if num else ''}'>{e(_heading(str(col)))}</th>")
        out.append("</tr></thead><tbody>")
        for row in s.frame.itertuples(index=False):
            out.append("<tr>")
            for col, val in zip(s.frame.columns, row, strict=True):
                num = col in s.money_columns or col in s.percent_columns
                cls = "num" if num else ""
                if str(col).startswith("change") and isinstance(val, float) and val == val:
                    cls += " up" if val > 0 else " down" if val < 0 else ""
                out.append(f"<td class='{cls}'>{e(_cell(s, str(col), val, report.currency))}</td>")
            out.append("</tr>")
        out.append("</tbody></table></div>")
        if s.note:
            out.append(f"<div class='note'>{e(s.note)}</div>")
    out.append("</main></body></html>")
    return "".join(out)


def render(report: Report, fmt: Format, console: Console) -> str | None:
    """Return text for json/csv/html; print Rich output directly for 'table'."""
    if fmt == "table":
        render_rich(report, console)
        return None
    return {"json": to_json, "csv": to_csv, "html": to_html}[fmt](report)
