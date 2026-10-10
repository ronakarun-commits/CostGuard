"""Probabilistic spend forecasting by Monte Carlo simulation.

Point forecasts ("you'll spend $25,400") hide the question finance actually asks: *how bad could
it get?* So costguard forecasts a **distribution**: thousands of simulated future daily-cost
paths. Every risk number (Cost-at-Risk, Expected Shortfall, breach probability) is then just a
statistic of those paths, and they stay mutually consistent.

Models
``ets``    Exponential smoothing state-space model (ETS(A,Ad,A)) on log cost: additive error,
           damped additive trend, additive weekly seasonality. Fitted by maximum likelihood
           (statsmodels); ``simulate`` draws future paths with the fitted error variance, so
           uncertainty grows with the horizon. Damping stops the trend from running away.
``naive``  Seasonal naive + bootstrap: next Monday = last Monday x a random historical
           week-over-week ratio. No parameters; the benchmark any model must beat.

Working in logs makes noise multiplicative (a $1000/day service varies by more dollars than a
$10/day one) and guarantees positive simulated costs.

History is **cleaned** before fitting: days whose cost is a robust outlier versus the same-weekday
median are replaced by that median. Otherwise a one-off spike inflates the fitted variance and
every future interval becomes too wide.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from costguard.core.analytics._series import expm1, log1p
from costguard.core.reports import days_in_month

Model = Literal["ets", "naive"]
TRAIN_DAYS = 180


@dataclass(frozen=True, slots=True)
class Forecast:
    model: str
    history: pd.Series  # actual daily cost up to as_of
    index: pd.DatetimeIndex  # forecast dates
    paths: np.ndarray  # shape (n_paths, horizon): simulated daily cost

    @property
    def as_of(self) -> date:
        return self.history.index[-1].date()  # type: ignore[no-any-return]

    def mean(self) -> pd.Series:
        return pd.Series(self.paths.mean(axis=0), index=self.index)

    def quantiles(self, qs: tuple[float, ...] = (0.05, 0.5, 0.95)) -> pd.DataFrame:
        return pd.DataFrame(
            np.quantile(self.paths, qs, axis=0).T,
            index=self.index,
            columns=[f"p{q * 100:g}" for q in qs],
        )

    def widened(self, factor: float) -> Forecast:
        """Scale each day's distribution around its median, in log space, by ``factor``.

        Used for conformal calibration: factor > 1 widens intervals that history showed were
        too narrow; the median (point forecast) is unchanged.
        """
        if factor == 1.0:
            return self
        logp = np.log1p(self.paths)
        med = np.median(logp, axis=0, keepdims=True)
        paths = np.clip(np.expm1(med + factor * (logp - med)), 0.0, None)
        return Forecast(self.model, self.history, self.index, paths)

    def totals(self, end: date | None = None) -> np.ndarray:
        """Simulated total cost from tomorrow up to and including ``end`` (default: horizon)."""
        n = len(self.index) if end is None else int((self.index <= pd.Timestamp(end)).sum())
        return self.paths[:, :n].sum(axis=1)


def clean_history(series: pd.Series, z: float = 5.0) -> pd.Series:
    """Replace robust outliers (vs same-weekday median of prior 4 weeks) with that median."""
    logx = log1p(series.astype(float))
    lags = pd.concat([logx.shift(7 * k) for k in range(1, 5)], axis=1)
    med = lags.median(axis=1)
    resid = logx - med
    mad = float((resid - resid.median()).abs().median()) * 1.4826
    outlier = (resid.abs() > z * max(mad, 0.02)) & med.notna()
    return expm1(logx.where(~outlier, med))


def forecast(
    series: pd.Series,
    horizon: int,
    *,
    model: Model = "ets",
    n_paths: int = 2000,
    seed: int = 0,
    train_days: int = TRAIN_DAYS,
    clean: bool = True,
) -> Forecast:
    """Simulate ``n_paths`` future daily-cost paths for the next ``horizon`` days."""
    if horizon < 1:
        msg = "horizon must be >= 1"
        raise ValueError(msg)
    s = series.astype(float).asfreq("D", fill_value=0.0)
    if len(s) < 28:
        msg = f"need at least 28 days of history to forecast, have {len(s)}"
        raise ValueError(msg)
    train = (clean_history(s) if clean else s).iloc[-train_days:]
    index = pd.date_range(s.index[-1] + pd.Timedelta(days=1), periods=horizon, freq="D")
    rng = np.random.default_rng(seed)
    if model == "ets":
        paths = _ets_paths(train, horizon, n_paths, rng)
    elif model == "naive":
        paths = _naive_paths(train, horizon, n_paths, rng)
    else:  # pragma: no cover
        msg = f"unknown model {model!r}"
        raise ValueError(msg)
    return Forecast(model, s, index, paths)


def _ets_paths(train: pd.Series, horizon: int, n: int, rng: np.random.Generator) -> np.ndarray:
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    logx = np.log1p(train).reset_index(drop=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # convergence chatter on short/flat series
        fit = ETSModel(
            logx, error="add", trend="add", damped_trend=True, seasonal="add", seasonal_periods=7
        ).fit(disp=False, maxiter=200)
        sims = fit.simulate(nsimulations=horizon, repetitions=n, anchor="end", rng=rng)
    arr = np.asarray(sims, dtype=float).reshape(horizon, n).T
    return np.clip(np.expm1(arr), 0.0, None)


def _naive_paths(train: pd.Series, horizon: int, n: int, rng: np.random.Generator) -> np.ndarray:
    logx = np.log1p(train.to_numpy())
    ratios = (logx[7:] - logx[:-7])[-90:]  # recent week-over-week log changes
    paths = np.empty((n, horizon))
    last_week = np.tile(logx[-7:], (n, 1))
    for h in range(horizon):
        base = last_week[:, h % 7]
        paths[:, h] = base + rng.choice(ratios, size=n)
        last_week[:, h % 7] = paths[:, h]
    return np.clip(np.expm1(paths), 0.0, None)


def month_end_horizon(as_of: date) -> int:
    """Days remaining in ``as_of``'s month after ``as_of``."""
    return days_in_month(as_of) - as_of.day
