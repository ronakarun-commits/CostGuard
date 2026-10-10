"""Streamlit dashboard. Run with ``costguard ui`` (needs ``pip install "costguard[dashboard]"``).

Streamlit re-runs this script top to bottom on every interaction; ``st.cache_data`` keeps the
expensive parts (database reads, Monte Carlo) from re-running unless their inputs change.
The database is opened read-only. Settings come from the usual sources (env / .env / toml);
``COSTGUARD_DB_PATH`` is set by ``costguard ui``.
"""

from __future__ import annotations

from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from costguard import services
from costguard.config import load_settings
from costguard.core.analytics.anomaly import DetectorConfig, Method, detect
from costguard.core.analytics.forecast import forecast
from costguard.core.analytics.risk import MonthEndRisk, month_end_risk
from costguard.errors import CostGuardError
from costguard.interfaces.render import fmt_money
from costguard.storage.duckdb_repo import DuckDBRepository

settings = load_settings()
st.set_page_config(page_title="costguard", page_icon="📉", layout="wide")


@st.cache_data(ttl=300, show_spinner=False)
def load(db: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with DuckDBRepository(db, read_only=True) as repo:
        services.require_data(repo)
        return repo.costs_frame(), repo.daily_series(by="service"), repo.daily_series(by="account")


@st.cache_data(ttl=300, show_spinner=False)
def simulate(
    total: pd.Series, budget: float | None, level: float
) -> tuple[MonthEndRisk, pd.DataFrame]:
    risk = month_end_risk(total, budget=budget, level=level, n_paths=3000)
    fc = forecast(total, 30, n_paths=1500)
    bands = fc.quantiles((0.05, 0.5, 0.95))
    return risk, bands


def main() -> None:
    st.title("costguard")
    try:
        costs, _, by_account = load(str(settings.db_path))
    except CostGuardError as exc:
        st.error(f"{exc} {exc.hint or ''}")
        st.stop()
        return

    with st.sidebar:
        st.header("Settings")
        currency = st.radio("Currency", ["USD", "INR"],
                            index=0 if settings.reporting_currency == "USD" else 1)  # fmt: skip
        fx = settings.usd_inr_fallback_rate if currency == "INR" else 1.0
        budget_in = st.number_input(f"Monthly budget ({currency})", min_value=0.0,
                                    value=float((settings.budget_monthly_usd or 0) * fx),
                                    step=1000.0)  # fmt: skip
        budget = budget_in / fx if budget_in > 0 else None
        level = st.select_slider("Risk confidence", [0.90, 0.95, 0.99], value=0.95)
        accounts = st.multiselect(
            "Accounts", list(by_account.columns), default=list(by_account.columns)
        )
        st.caption(f"Database: `{settings.db_path}`")

    filtered = costs[costs["sub_account_name"].isin(accounts)]
    total = filtered.groupby("charge_date")["billed_cost"].sum().asfreq("D", fill_value=0.0)
    if total.empty:
        st.warning("No data for the selected accounts.")
        return
    money = lambda v: fmt_money(v * fx, currency)  # noqa: E731

    risk, bands = simulate(total, budget, level)
    as_of = total.index[-1]
    mtd = total[total.index >= as_of.replace(day=1)].sum()
    c = st.columns(5)
    c[0].metric("Month to date", money(mtd))
    c[1].metric("Expected month-end", money(risk.expected))
    c[2].metric(
        f"Cost-at-Risk {level:.0%}",
        money(risk.car),
        f"+{money(risk.car_excess)} vs expected",
        delta_color="inverse",
    )
    c[3].metric(f"Expected Shortfall {level:.0%}", money(risk.expected_shortfall))
    bp = risk.breach_probability
    c[4].metric("P(over budget)", "–" if bp is None else f"{bp:.0%}")

    st.subheader("Daily spend and 30-day forecast")
    hist = (total.iloc[-120:] * fx).rename("cost").reset_index()
    hist.columns = ["date", "cost"]
    band = (bands * fx).reset_index()
    band.columns = ["date", "p5", "p50", "p95"]
    chart = (
        alt.Chart(band).mark_area(opacity=0.25).encode(x="date:T", y=alt.Y("p5:Q", title=currency),
                                                       y2="p95:Q")
        + alt.Chart(band).mark_line(strokeDash=[4, 3]).encode(x="date:T", y="p50:Q")
        + alt.Chart(hist).mark_line().encode(x="date:T", y="cost:Q")
    )  # fmt: skip
    st.altair_chart(chart, use_container_width=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Top services (last 30 days)")
        last30 = filtered[filtered["charge_date"] > as_of - timedelta(days=30)]
        top = last30.groupby("service_name")["billed_cost"].sum().sort_values(ascending=False)
        st.bar_chart(top.head(8) * fx, horizontal=True)
    with right:
        st.subheader("Spend by account (monthly)")
        monthly = by_account[accounts].resample("MS").sum() * fx
        monthly.index = pd.DatetimeIndex(monthly.index).strftime("%Y-%m")
        st.bar_chart(monthly, stack=True)

    st.subheader("Anomalies (last 30 days)")
    choice = st.segmented_control("Detector", ["baseline", "iforest", "stl"], default="baseline")
    method: Method = choice if choice in ("iforest", "stl") else "baseline"  # type: ignore[assignment]
    found = detect(filtered, method, DetectorConfig(),
                   since=(as_of - timedelta(days=29)).date())  # fmt: skip
    if found:
        st.dataframe(
            pd.DataFrame([{"start": i.start_date, "end": i.end_date, "account": i.sub_account_name,
                           "service": i.service_name, "x usual": round(i.peak_ratio, 1),
                           f"excess ({currency})": round(i.excess_cost * fx, 2)} for i in found]),
            hide_index=True, use_container_width=True,
        )  # fmt: skip
    else:
        st.success("No anomalies in the last 30 days.")


main()
