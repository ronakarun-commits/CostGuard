"""Cost risk metrics from simulated forecasts, and backtests that keep them honest.

Market-risk ideas mapped to cloud spend
* **Cost-at-Risk (CaR)** at level α: the α-quantile of month-end spend. "With 95% confidence,
  this month's bill will not exceed CaR." Same construction as Value-at-Risk, applied to a cost
  instead of a P&L loss.
* **CaR excess**: CaR minus expected spend: the *unexpected* part, what a buffer must cover.
* **Expected Shortfall (ES)**: mean spend in the worst (1-α) of outcomes. VaR says where the tail
  starts; ES says how bad it is inside the tail. ES is coherent (sub-additive), VaR is not, which
  is why Basel's FRTB moved market risk from VaR to ES.
* **Breach probability**: share of paths ending above the budget.

Backtesting (Kupiec proportion-of-failures test)
If a 95% bound is calibrated, actual spend should exceed it on ~5% of days. Kupiec's likelihood
ratio compares the observed exceedance rate x/T with p = 1-α:

    LR = -2 ln[(1-p)^(T-x) p^x] + 2 ln[(1-x/T)^(T-x) (x/T)^x]  ~  chi-squared(1)

A small p-value (< 0.05) rejects calibration: too many exceedances = risk understated, too few =
bounds wastefully wide.

Conformal calibration
The ETS model assumes Gaussian noise around a smooth path. Real spend also jumps (incidents,
forgotten resources), so raw model bounds are breached too often. Rather than guess a fatter
distribution, costguard measures how wrong its own past bounds were. For every past day,

    score = (log actual - log median) / (log bound - log median)

score <= 1 means the day stayed inside the model's bound. The ``level`` quantile of past scores,
``c``, is the factor by which the bounds must be stretched (in log space, around the median) to
have achieved the target coverage. Only scores from *before* a forecast date are used, so
backtests stay out-of-sample. This is split-conformal prediction: distribution-free, and valid
whenever the past errors are representative of future ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from scipy import stats

from costguard.core.analytics.forecast import Model, forecast, month_end_horizon

MIN_CAL_SCORES = 28


@dataclass(frozen=True, slots=True)
class MonthEndRisk:
    as_of: date
    level: float
    month_to_date: float
    expected: float
    median: float
    car: float
    expected_shortfall: float
    p5: float
    budget: float | None
    breach_probability: float | None
    remaining_days: int

    @property
    def car_excess(self) -> float:
        return self.car - self.expected


def _month_end_paths(
    series: pd.Series,
    *,
    level: float,
    model: Model,
    n_paths: int,
    seed: int,
    calibrate: bool,
) -> tuple[date, float, int, pd.DatetimeIndex, np.ndarray]:
    """Cumulative simulated spend for the rest of the month: (as_of, mtd, remaining, index,
    cumulative paths of shape (n_paths, remaining), *excluding* month-to-date)."""
    if not 0.5 < level < 1:
        msg = "level must be between 0.5 and 1"
        raise ValueError(msg)
    s = series.astype(float).asfreq("D", fill_value=0.0)
    as_of = s.index[-1].date()
    mtd = float(s[s.index >= pd.Timestamp(as_of.replace(day=1))].sum())
    remaining = month_end_horizon(as_of)
    if remaining == 0:
        return as_of, mtd, 0, pd.DatetimeIndex([]), np.zeros((n_paths, 0))
    fc = forecast(s, remaining, model=model, n_paths=n_paths, seed=seed)
    factor = 1.0
    if calibrate:
        scores = daily_scores(s, level=level, model=model, seed=seed)
        factor = conformal_factor(scores, as_of, level)
    return as_of, mtd, remaining, fc.index, np.cumsum(fc.widened(factor).paths, axis=1)


def month_end_risk(
    series: pd.Series,
    *,
    budget: float | None = None,
    level: float = 0.95,
    model: Model = "ets",
    n_paths: int = 5000,
    seed: int = 0,
    calibrate: bool = True,
) -> MonthEndRisk:
    """Distribution of this month's total = actual month-to-date + simulated rest of month."""
    risk, _ = month_end_fan(series, budget=budget, level=level, model=model, n_paths=n_paths,
                            seed=seed, calibrate=calibrate)  # fmt: skip
    return risk


FAN_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def month_end_fan(
    series: pd.Series,
    *,
    budget: float | None = None,
    level: float = 0.95,
    model: Model = "ets",
    n_paths: int = 5000,
    seed: int = 0,
    calibrate: bool = True,
) -> tuple[MonthEndRisk, pd.DataFrame]:
    """Month-end risk plus a fan chart: quantiles of *cumulative* month spend for each remaining
    day (columns p5, p25, p50, p75, p95; index = date). Empty frame on the last day of a month."""
    as_of, mtd, remaining, index, cum = _month_end_paths(
        series, level=level, model=model, n_paths=n_paths, seed=seed, calibrate=calibrate
    )
    totals = mtd + cum[:, -1] if remaining else np.full(n_paths, mtd)
    risk = summarise(totals, as_of=as_of, mtd=mtd, level=level, budget=budget,
                     remaining=remaining)  # fmt: skip
    cols = [f"p{round(q * 100)}" for q in FAN_QUANTILES]
    if not remaining:
        return risk, pd.DataFrame(columns=cols)
    fan = pd.DataFrame(mtd + np.quantile(cum, FAN_QUANTILES, axis=0).T, index=index, columns=cols)
    return risk, fan


def summarise(
    totals: np.ndarray, *, as_of: date, mtd: float, level: float, budget: float | None,
    remaining: int,
) -> MonthEndRisk:  # fmt: skip
    car = float(np.quantile(totals, level))
    tail = totals[totals >= car]
    return MonthEndRisk(
        as_of=as_of,
        level=level,
        month_to_date=mtd,
        expected=float(totals.mean()),
        median=float(np.median(totals)),
        car=car,
        expected_shortfall=float(tail.mean()) if len(tail) else car,
        p5=float(np.quantile(totals, 0.05)),
        budget=budget,
        breach_probability=float((totals > budget).mean()) if budget else None,
        remaining_days=remaining,
    )


@dataclass(frozen=True, slots=True)
class KupiecResult:
    observations: int
    exceedances: int
    expected_rate: float
    lr_statistic: float
    p_value: float

    @property
    def observed_rate(self) -> float:
        return self.exceedances / self.observations if self.observations else 0.0

    @property
    def calibrated(self) -> bool:
        return self.p_value >= 0.05


def kupiec_pof(exceedances: int, observations: int, expected_rate: float) -> KupiecResult:
    if observations <= 0 or not 0 <= exceedances <= observations:
        msg = "need 0 <= exceedances <= observations and observations > 0"
        raise ValueError(msg)
    p, x, t = expected_rate, exceedances, observations
    phat = x / t

    def loglik(prob: float) -> float:
        # 0 * log(0) is taken as 0
        a = (t - x) * np.log1p(-prob) if t - x else 0.0
        b = x * np.log(prob) if x else 0.0
        return float(a + b)

    lr = max(0.0, -2 * (loglik(p) - loglik(phat)))
    return KupiecResult(t, x, p, lr, float(stats.chi2.sf(lr, df=1)))


def daily_scores(
    series: pd.Series,
    *,
    level: float = 0.95,
    start: int = 90,
    refit_every: int = 7,
    model: Model = "ets",
    n_paths: int = 1000,
    seed: int = 0,
) -> pd.DataFrame:
    """Rolling-origin forecasts of each day, using only data before it.

    Every ``refit_every`` days the model is re-fitted on data up to that day, and for each of the
    next ``refit_every`` days (1- to 7-day-ahead) the median, the raw ``level`` bound and the
    conformal score are recorded.
    """
    s = series.astype(float).asfreq("D", fill_value=0.0)
    rows = []
    for origin in range(min(start, len(s) - 2), len(s) - 1, refit_every):
        horizon = min(refit_every, len(s) - 1 - origin)
        fc = forecast(
            s.iloc[: origin + 1], horizon, model=model, n_paths=n_paths, seed=seed + origin
        )
        med = np.median(fc.paths, axis=0)
        bound = np.quantile(fc.paths, level, axis=0)
        for h in range(horizon):
            actual = float(s.loc[fc.index[h]])
            width = np.log1p(bound[h]) - np.log1p(med[h])
            score = (np.log1p(actual) - np.log1p(med[h])) / max(width, 1e-6)
            rows.append({"date": fc.index[h], "origin": s.index[origin], "actual": actual,
                         "median": float(med[h]), "raw_bound": float(bound[h]),
                         "score": float(score)})  # fmt: skip
    return pd.DataFrame(rows).set_index("date")


def conformal_factor(scores: pd.DataFrame, as_of: date | pd.Timestamp, level: float) -> float:
    """Widening factor from scores of days strictly before ``as_of`` (never below 1)."""
    past = scores.loc[scores.index < pd.Timestamp(as_of), "score"]
    if len(past) < MIN_CAL_SCORES:
        return 1.0
    return max(1.0, float(np.quantile(past, level)))


def var_backtest(
    series: pd.Series,
    *,
    level: float = 0.95,
    start: int = 120,
    refit_every: int = 7,
    model: Model = "ets",
    n_paths: int = 1000,
    seed: int = 0,
    calibrate: bool = True,
) -> tuple[pd.DataFrame, KupiecResult]:
    """Backtest daily ``level`` upper bounds, raw or conformally calibrated (out-of-sample)."""
    scores = daily_scores(series, level=level, start=min(60, start), refit_every=refit_every,
                          model=model, n_paths=n_paths, seed=seed)  # fmt: skip
    df = scores[scores.index >= series.index[0] + pd.Timedelta(days=start)].copy()
    if calibrate:
        factors = [conformal_factor(scores, origin, level) for origin in df["origin"]]
        df["factor"] = factors
    else:
        df["factor"] = 1.0
    width = np.log1p(df["raw_bound"]) - np.log1p(df["median"])
    df["bound"] = np.expm1(np.log1p(df["median"]) + df["factor"] * width)
    df["exceeded"] = df["actual"] > df["bound"]
    result = kupiec_pof(int(df["exceeded"].sum()), len(df), 1 - level)
    return df, result


@dataclass(frozen=True, slots=True)
class MonthEndBacktest:
    rows: pd.DataFrame  # one row per (month, forecast day)

    @property
    def mape(self) -> float:
        return float(self.rows["abs_pct_error"].mean())

    @property
    def coverage(self) -> float:
        return float(self.rows["inside_90"].mean())


def month_end_backtest(
    series: pd.Series,
    *,
    days: tuple[int, ...] = (7, 14, 21),
    model: Model = "ets",
    n_paths: int = 1000,
    seed: int = 0,
    calibrate: bool = True,
    level: float = 0.95,
) -> MonthEndBacktest:
    """For each complete month (after 60 days of history), forecast its total on day 7, 14 and 21
    using only data available then; compare to what was actually billed."""
    s = series.astype(float).asfreq("D", fill_value=0.0)
    scores = daily_scores(s, level=level, start=60, model=model, n_paths=n_paths, seed=seed)
    rows = []
    months = pd.period_range(s.index[0], s.index[-1], freq="M")
    for m in months:
        first, last = m.start_time, m.end_time.normalize()
        if last > s.index[-1] or (first - s.index[0]).days < 60:
            continue
        actual_total = float(s[first:last].sum())
        for d in days:
            as_of = first + pd.Timedelta(days=d - 1)
            hist = s[:as_of]
            remaining = (last - as_of).days
            fc = forecast(hist, remaining, model=model, n_paths=n_paths, seed=seed + d)
            factor = conformal_factor(scores, as_of, level) if calibrate else 1.0
            totals = float(hist[first:].sum()) + fc.widened(factor).totals()
            p5, p50, p95 = np.quantile(totals, [0.05, 0.5, 0.95])
            rows.append({
                "month": str(m), "day": d, "actual": actual_total, "forecast": float(p50),
                "p5": float(p5), "p95": float(p95),
                "abs_pct_error": abs(p50 / actual_total - 1) if actual_total else np.nan,
                "inside_90": bool(p5 <= actual_total <= p95),
            })  # fmt: skip
    return MonthEndBacktest(pd.DataFrame(rows))
