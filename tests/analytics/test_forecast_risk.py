from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from costguard.core.analytics.forecast import clean_history, forecast, month_end_horizon
from costguard.core.analytics.risk import (
    conformal_factor,
    daily_scores,
    kupiec_pof,
    month_end_backtest,
    month_end_risk,
    summarise,
    var_backtest,
)


@pytest.mark.parametrize("model", ["ets", "naive"])
def test_forecast_shape_and_level(smooth: pd.Series, model: str) -> None:
    fc = forecast(smooth, 14, model=model, n_paths=500)  # type: ignore[arg-type]
    assert fc.paths.shape == (500, 14)
    assert (fc.paths >= 0).all()
    assert fc.index[0] == smooth.index[-1] + pd.Timedelta(days=1)
    # weekday/weekend pattern carried forward
    q = fc.quantiles()
    assert list(q.columns) == ["p5", "p50", "p95"]
    weekend = q["p50"][pd.DatetimeIndex(q.index).dayofweek >= 5].mean()
    weekday = q["p50"][pd.DatetimeIndex(q.index).dayofweek < 5].mean()
    assert weekend == pytest.approx(80, rel=0.05)
    assert weekday == pytest.approx(100, rel=0.05)
    assert fc.as_of == smooth.index[-1].date()
    assert fc.totals().shape == (500,)
    assert fc.totals(fc.index[2].date()).mean() == pytest.approx(fc.mean().iloc[:3].sum(), rel=1e-9)


def test_forecast_validation(smooth: pd.Series) -> None:
    with pytest.raises(ValueError, match="horizon"):
        forecast(smooth, 0)
    with pytest.raises(ValueError, match="28 days"):
        forecast(smooth.iloc[:10], 5)


def test_clean_history_removes_spikes(smooth: pd.Series) -> None:
    s = smooth.copy()
    s.iloc[100] *= 6
    cleaned = clean_history(s)
    assert cleaned.iloc[100] == pytest.approx(smooth.iloc[100], rel=0.05)
    assert cleaned.iloc[99] == pytest.approx(s.iloc[99])


def test_widened_keeps_median(smooth: pd.Series) -> None:
    fc = forecast(smooth, 7, n_paths=2000)
    wide = fc.widened(2.0)
    assert fc.widened(1.0) is fc
    assert np.median(wide.paths, axis=0) == pytest.approx(np.median(fc.paths, axis=0), rel=1e-6)
    spread = lambda f: np.quantile(f.paths, 0.95, axis=0) / np.median(f.paths, axis=0)  # noqa: E731
    assert (spread(wide) > spread(fc)).all()


def test_month_end_horizon() -> None:
    assert month_end_horizon(date(2026, 2, 10)) == 18
    assert month_end_horizon(date(2026, 3, 31)) == 0


def test_kupiec() -> None:
    ok = kupiec_pof(5, 100, 0.05)
    assert ok.lr_statistic == pytest.approx(0.0)
    assert ok.calibrated
    bad = kupiec_pof(20, 100, 0.05)
    assert not bad.calibrated
    assert bad.observed_rate == 0.2
    # hand-computed: x=10, T=100, p=0.05 -> LR = 69.147 - 65.017 = 4.13
    assert kupiec_pof(10, 100, 0.05).lr_statistic == pytest.approx(4.13, abs=0.01)
    assert kupiec_pof(0, 100, 0.05).p_value < 0.05  # too conservative also rejects
    with pytest.raises(ValueError):
        kupiec_pof(5, 0, 0.05)


def test_summarise_risk_metrics() -> None:
    totals = np.arange(1, 101, dtype=float)
    r = summarise(totals, as_of=date(2026, 1, 10), mtd=0, level=0.95, budget=90, remaining=21)
    assert r.car == pytest.approx(95.05)
    assert r.expected_shortfall == pytest.approx(98.0)
    assert r.expected_shortfall >= r.car >= r.median
    assert r.breach_probability == pytest.approx(0.10)
    assert r.car_excess == pytest.approx(95.05 - 50.5)


def test_month_end_risk(smooth: pd.Series) -> None:
    r = month_end_risk(smooth, budget=1e9, n_paths=1000, calibrate=False)
    assert r.as_of == date(2026, 7, 19)
    assert r.remaining_days == 12
    assert r.month_to_date == pytest.approx(smooth["2026-07-01":].sum())
    assert r.p5 < r.median < r.car < r.expected_shortfall
    assert r.breach_probability == 0.0
    with pytest.raises(ValueError, match="level"):
        month_end_risk(smooth, level=1.5)


def test_month_end_risk_last_day_of_month(smooth: pd.Series) -> None:
    r = month_end_risk(smooth.loc[:"2026-06-30"], n_paths=500)
    assert r.remaining_days == 0
    assert r.car == pytest.approx(r.month_to_date)


def test_conformal_factor_out_of_sample() -> None:
    scores = pd.DataFrame(
        {"score": np.r_[np.full(50, 0.5), np.full(50, 3.0)]},
        index=pd.date_range("2026-01-01", periods=100),
    )
    assert conformal_factor(scores, pd.Timestamp("2026-01-10"), 0.95) == 1.0  # too few scores
    assert conformal_factor(scores, pd.Timestamp("2026-02-20"), 0.95) == 1.0  # all <= 1
    assert conformal_factor(scores, pd.Timestamp("2026-12-31"), 0.95) == pytest.approx(3.0)


def test_calibration_improves_backtest(
    world: tuple[object, pd.DataFrame, pd.Series],
) -> None:
    _, _, series = world
    _, raw = var_backtest(series, calibrate=False, n_paths=300)
    cal_df, cal = var_backtest(series, calibrate=True, n_paths=300)
    assert raw.observations == cal.observations > 200
    assert cal.exceedances < raw.exceedances
    assert (cal_df["bound"] >= cal_df["raw_bound"] - 1e-6).all()


def test_daily_scores_and_month_backtest(smooth: pd.Series) -> None:
    scores = daily_scores(smooth, n_paths=200, start=150)
    assert {"median", "raw_bound", "score", "origin"} <= set(scores.columns)
    assert (scores["origin"] < scores.index).all()  # strictly out of sample
    mb = month_end_backtest(smooth, n_paths=200, days=(10,))
    assert len(mb.rows) >= 2
    assert mb.mape < 0.02
    assert 0 <= mb.coverage <= 1
