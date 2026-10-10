"""Savings Plan / Reserved Instance economics, treated as a hedge.

A Compute Savings Plan is a commitment to spend ``h`` USD *per hour* (at discounted rates) for 1 or
3 years. Each hour it covers up to ``h / (1 - d)`` of on-demand-equivalent usage, where ``d`` is
the discount. Usage above that is billed on demand; commitment not used in an hour is lost.

* **Break-even utilisation** = 1 - d. A 28%-discount plan saves money only if, on average, more
  than 72% of the commitment is actually used.
* **Hedge ratio** = covered usage / expected usage. Like hedging a commodity exposure: hedge 100%
  of a *volatile* exposure and you over-hedge in bad months (paying for unused commitment).
  The optimum is usually below the mean, near a low percentile of usage.
* **NPV**: commitments trade cash now (upfront) for savings later, so options are compared on
  the present value of (on-demand cost avoided - commitment payments) at a cost of capital.

Usage uncertainty comes from Monte Carlo: monthly eligible spend follows a log-normal random walk
with the drift and volatility measured from history. Within a month, *daily* variation (weekend
dips) is applied from the observed distribution, because a commitment is consumed hour by hour,
not on the monthly average. Intra-day variation is ignored (daily data), which slightly flatters
utilisation; documented as a limitation.

Discounts are approximate published Compute Savings Plan rates (vary by instance family/region).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

HOURS_PER_MONTH = 730.0
Payment = Literal["no_upfront", "partial_upfront", "all_upfront"]


@dataclass(frozen=True, slots=True)
class PlanOption:
    name: str
    term_months: int
    payment: Payment
    discount: float  # vs on-demand

    @property
    def break_even_utilisation(self) -> float:
        return 1 - self.discount


DEFAULT_OPTIONS: tuple[PlanOption, ...] = (
    PlanOption("1y no upfront", 12, "no_upfront", 0.27),
    PlanOption("1y partial upfront", 12, "partial_upfront", 0.29),
    PlanOption("1y all upfront", 12, "all_upfront", 0.31),
    PlanOption("3y no upfront", 36, "no_upfront", 0.46),
    PlanOption("3y partial upfront", 36, "partial_upfront", 0.49),
    PlanOption("3y all upfront", 36, "all_upfront", 0.52),
)


@dataclass(frozen=True, slots=True)
class UsageModel:
    """Monthly eligible on-demand spend: log-normal random walk + empirical daily shape."""

    last_month: float  # on-demand USD in the latest full month (or 30-day run-rate)
    monthly_drift: float  # mean monthly log change
    monthly_vol: float  # std of monthly log change
    daily_ratios: np.ndarray  # day cost / month's mean day cost, from history

    @classmethod
    def from_daily(cls, daily: pd.Series) -> UsageModel:
        s = daily.astype(float).asfreq("D", fill_value=0.0)
        recent = s.iloc[-30:]
        monthly = s.resample("MS").mean()  # mean *daily* spend per month (fair for short months)
        full = monthly.iloc[1:-1] if len(monthly) > 3 else monthly
        positive = full[full > 0]
        changes = pd.Series(np.log(positive.to_numpy()), index=positive.index).diff().dropna()
        drift = float(changes.mean()) if len(changes) >= 2 else 0.0
        vol = float(changes.std(ddof=1)) if len(changes) >= 3 else 0.05
        mean_by_month = s.groupby(pd.DatetimeIndex(s.index).to_period("M")).transform("mean")
        ratios = (s / mean_by_month.replace(0, np.nan)).dropna().to_numpy()
        return cls(float(recent.mean()) * 30.4, drift, max(vol, 0.01), ratios[-180:])

    def simulate(self, months: int, n_paths: int, rng: np.random.Generator) -> np.ndarray:
        """(n_paths, months) monthly on-demand-equivalent spend."""
        shocks = rng.normal(self.monthly_drift, self.monthly_vol, size=(n_paths, months))
        return self.last_month * np.exp(np.cumsum(shocks, axis=1))


class CoverageCurve:
    """g(x) = E[min(r, x)] over the empirical daily ratios r = day cost / month mean.

    A plan absorbing ``capacity`` per day against a month averaging ``mean`` per day covers
    ``g(capacity / mean)`` of that month's usage. Because g depends only on the ratio, it is
    tabulated once and interpolated — ~1000x faster than evaluating every day of every path.
    """

    def __init__(self, ratios: np.ndarray, max_x: float = 5.0, points: int = 1001) -> None:
        r = np.sort(np.asarray(ratios, dtype=float))
        self.x = np.linspace(0.0, max_x, points)
        # E[min(r, x)] = mean(r[r < x]) * P(r < x) + x * P(r >= x), via cumulative sums
        idx = np.searchsorted(r, self.x)
        csum = np.concatenate([[0.0], np.cumsum(r)])
        n = len(r)
        mean_r = r.mean() if n else 1.0
        self.g = (csum[idx] + self.x * (n - idx)) / max(n, 1) / mean_r

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(np.interp(x, self.x, self.g), dtype=float)


def covered_fraction(capacity_per_day: np.ndarray, mean_per_day: np.ndarray,
                     ratios: np.ndarray | CoverageCurve) -> np.ndarray:  # fmt: skip
    """Share of usage a plan covers, given the empirical within-month daily shape."""
    curve = ratios if isinstance(ratios, CoverageCurve) else CoverageCurve(ratios)
    with np.errstate(divide="ignore", invalid="ignore"):
        x = np.where(mean_per_day > 0, capacity_per_day / mean_per_day, 0.0)
    return np.where(mean_per_day > 0, curve(x), 0.0)


@dataclass(frozen=True, slots=True)
class PlanEvaluation:
    option: PlanOption
    hourly_commitment: float
    npv_mean: float
    npv_p5: float
    prob_loss: float
    annual_saving_mean: float
    utilisation_mean: float
    coverage_mean: float  # share of on-demand usage covered by the plan
    hedge_ratio: float  # covered capacity / expected usage
    upfront: float


def evaluate_plan(
    option: PlanOption,
    hourly_commitment: float,
    usage_paths: np.ndarray,
    ratios: np.ndarray | CoverageCurve,
    annual_rate: float = 0.10,
) -> PlanEvaluation:
    """Cash-flow NPV distribution of one plan at one commitment level."""
    months = option.term_months
    paths = usage_paths[:, :months]
    h = hourly_commitment
    commit_month = h * HOURS_PER_MONTH  # discounted-rate USD per month
    capacity_month = commit_month / (1 - option.discount)  # on-demand USD it can absorb
    days = 30.4
    frac = covered_fraction(np.full(paths.shape, capacity_month / days), paths / days, ratios)
    covered = paths * frac
    uncovered = paths - covered
    upfront = {"no_upfront": 0.0, "partial_upfront": 0.5, "all_upfront": 1.0}[option.payment]
    upfront_cash = upfront * commit_month * months
    monthly_payment = (1 - upfront) * commit_month
    saving_flow = paths - (uncovered + monthly_payment)  # on-demand avoided - what we pay monthly
    r_m = (1 + annual_rate) ** (1 / 12) - 1
    disc = (1 + r_m) ** -np.arange(1, months + 1)
    npv = saving_flow @ disc - upfront_cash
    total_saving = paths.sum(axis=1) - (uncovered.sum(axis=1) + commit_month * months)
    util = np.where(commit_month > 0, covered * (1 - option.discount) / max(commit_month, 1e-9), 0)
    return PlanEvaluation(
        option=option,
        hourly_commitment=h,
        npv_mean=float(npv.mean()),
        npv_p5=float(np.quantile(npv, 0.05)),
        prob_loss=float((npv < 0).mean()),
        annual_saving_mean=float(total_saving.mean() * 12 / months),
        utilisation_mean=float(util.mean()) if h > 0 else 0.0,
        coverage_mean=float(frac.mean()),
        hedge_ratio=float(capacity_month / paths[:, 0].mean()) if paths[:, 0].mean() else 0.0,
        upfront=float(upfront_cash),
    )


Objective = Literal["mean", "p5"]


def recommend(
    daily_eligible: pd.Series,
    *,
    options: tuple[PlanOption, ...] = DEFAULT_OPTIONS,
    annual_rate: float = 0.10,
    objective: Objective = "p5",
    n_paths: int = 2000,
    seed: int = 0,
    grid_points: int = 41,
) -> list[PlanEvaluation]:
    """Best hourly commitment for each option.

    ``objective="p5"`` maximises the 5th-percentile NPV (robust: "good even in a bad scenario");
    ``"mean"`` maximises expected NPV (risk-neutral, commits more).
    """
    model = UsageModel.from_daily(daily_eligible)
    rng = np.random.default_rng(seed)
    paths = model.simulate(max(o.term_months for o in options), n_paths, rng)
    max_h = model.last_month * 1.2 / HOURS_PER_MONTH  # on-demand $/h at 120% of current usage
    curve = CoverageCurve(model.daily_ratios)
    out = []
    for opt in options:
        best: PlanEvaluation | None = None
        for h in np.linspace(0, max_h * (1 - opt.discount), grid_points):
            ev = evaluate_plan(opt, float(h), paths, curve, annual_rate)
            key = ev.npv_p5 if objective == "p5" else ev.npv_mean
            if best is None or key > (best.npv_p5 if objective == "p5" else best.npv_mean):
                best = ev
        assert best is not None  # noqa: S101 - grid_points >= 1
        out.append(best)
    return out
