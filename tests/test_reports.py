from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from costguard.core.reports import _same_day_last_month, dashboard, days_in_month, trend


def _wide(start: str = "2026-01-01", end: str = "2026-03-10") -> pd.DataFrame:
    idx = pd.date_range(start, end)
    return pd.DataFrame({"EC2": 100.0, "S3": 10.0}, index=idx)


def test_dashboard_numbers() -> None:
    r = dashboard(_wide(), budget=3000)
    k = {kpi.label: kpi for kpi in r.kpis}
    assert k["Month to date"].value == pytest.approx(10 * 110)
    assert k["Month to date"].delta_pct == pytest.approx(0.0)  # Feb 1-10 had the same spend
    assert k["Run-rate forecast"].value == pytest.approx(31 * 110)
    assert k["Budget used (forecast)"].value == pytest.approx(31 * 110 / 3000)
    assert "OVER" in k["Budget used (forecast)"].note
    top = r.sections[0].frame
    assert list(top["service"]) == ["EC2", "S3"]
    assert top["share"].sum() == pytest.approx(1.0)
    assert r.period is not None
    assert str(r.period[0]) == "2026-03-01"


def test_dashboard_movers_rank_by_absolute_change() -> None:
    w = _wide()
    w.loc["2026-03-04":, "S3"] = 60.0  # S3 jumped
    movers = dashboard(w).sections[1].frame
    assert movers.iloc[0]["service"] == "S3"
    assert movers.iloc[0]["change"] > 0


def test_dashboard_without_budget_or_history() -> None:
    r = dashboard(_wide("2026-03-01", "2026-03-03"))
    labels = [k.label for k in r.kpis]
    assert "Budget used (forecast)" not in labels
    assert r.kpis[2].delta_pct is None


def test_dashboard_empty_raises() -> None:
    with pytest.raises(ValueError, match="no cost data"):
        dashboard(pd.DataFrame())


def test_trend_month_folds_others_and_flags_partial() -> None:
    w = _wide()
    w["Lambda"] = 1.0
    r = trend(w, freq="month", top=1)
    t = r.sections[0].frame
    assert list(t.columns) == ["period", "EC2", "Other", "Total", "change_pct"]
    assert list(t["period"]) == ["2026-01", "2026-02", "2026-03"]
    assert t["Total"].iloc[1] == pytest.approx(28 * 111)
    assert math.isnan(t["change_pct"].iloc[-1])
    assert "partial" in r.sections[0].note


def test_trend_complete_period_has_change() -> None:
    r = trend(_wide("2026-01-01", "2026-02-28"), freq="month")
    t = r.sections[0].frame
    assert t["change_pct"].iloc[-1] == pytest.approx(28 / 31 - 1)
    assert r.sections[0].note == ""


def test_trend_day_and_limits() -> None:
    w = _wide()
    w.iloc[:, :] = np.arange(len(w))[:, None]
    t = trend(w, freq="day", periods=5).sections[0].frame
    assert len(t) == 5
    with pytest.raises(ValueError, match="no cost data"):
        trend(pd.DataFrame())


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2026-03-31", "2026-02-28"), ("2026-03-15", "2026-02-15"), ("2026-01-10", "2025-12-10")],
)
def test_same_day_last_month(day: str, expected: str) -> None:
    assert str(_same_day_last_month(pd.Timestamp(day).date())) == expected


def test_days_in_month() -> None:
    assert days_in_month(pd.Timestamp("2028-02-10").date()) == 29
    assert days_in_month(pd.Timestamp("2026-12-31").date()) == 31
