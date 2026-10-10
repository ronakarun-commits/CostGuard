"""FX exposure: AWS bills in USD, the business reports in INR.

Month-on-month change in the INR bill splits exactly into a usage effect and an FX effect:

    INR1 - INR0 = (USD1 - USD0) * R0          usage effect (at last month's rate)
                + USD1 * (R1 - R0)            FX effect (the rate moved)

``R`` is the *effective* rate (INR bill / USD bill), i.e. each day's cost converted at that day's
rate, so it reflects when the money was spent.

Stress testing applies deterministic shocks (INR weakens 5%, 10%) and a stochastic one: FX daily
log-return volatility from history scales with sqrt(days) to the month end, combined with the
simulated USD spend (independent, which is a reasonable first approximation for cloud bills).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def daily_rates(rates: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Align published rates (business days only) to every calendar day by forward-filling."""
    r = rates.sort_index()
    full = r.reindex(r.index.union(index)).ffill().bfill()
    return full.reindex(index)


def synthetic_rates(start: pd.Timestamp, end: pd.Timestamp, seed: int = 0,
                    spot: float = 88.0, daily_vol: float = 0.003) -> pd.Series:  # fmt: skip
    """Seeded USD/INR path for offline demos (business days only, like real ECB data)."""
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng([seed, 77])
    steps = rng.normal(0.00012, daily_vol, size=len(idx))  # INR drifts weaker, ~3%/yr
    path = spot * np.exp(np.cumsum(steps) - steps.sum())  # ends at `spot`
    return pd.Series(path, index=idx, name="usd_inr")


@dataclass(frozen=True, slots=True)
class FxBridge:
    table: pd.DataFrame  # month, usd, inr, effective_rate, usage_effect, fx_effect, change_inr


def monthly_bridge(daily_usd: pd.Series, rates: pd.Series) -> FxBridge:
    """Bridge between consecutive *complete* months (a partial month would show a fake drop)."""
    s = daily_usd.astype(float).asfreq("D", fill_value=0.0)
    idx = pd.DatetimeIndex(s.index)
    if idx[-1] != idx[-1] + pd.offsets.MonthEnd(0):
        s = s[idx < idx[-1].replace(day=1)]
    if len(s) and idx[0].day != 1:
        s = s[pd.DatetimeIndex(s.index) >= idx[0] + pd.offsets.MonthBegin(1)]
    r = daily_rates(rates, pd.DatetimeIndex(s.index))
    month = pd.DatetimeIndex(s.index).to_period("M")
    df = pd.DataFrame({"usd": s.groupby(month).sum(), "inr": (s * r).groupby(month).sum()})
    df["effective_rate"] = df["inr"] / df["usd"].replace(0, np.nan)
    df["usage_effect"] = df["usd"].diff() * df["effective_rate"].shift(1)
    df["fx_effect"] = df["usd"] * df["effective_rate"].diff()
    df["change_inr"] = df["inr"].diff()
    df.index = df.index.astype(str)
    return FxBridge(df.rename_axis("month").reset_index())


def fx_vol(rates: pd.Series) -> float:
    """Daily log-return volatility of the rate (business days)."""
    lr = np.log(rates.sort_index()).diff().dropna()
    return float(lr.std(ddof=1)) if len(lr) > 5 else 0.003


@dataclass(frozen=True, slots=True)
class FxStress:
    spot: float
    expected_inr: float
    shocks: pd.DataFrame  # scenario, rate, inr_total, change_vs_expected
    car_inr: float  # level-quantile of INR total, USD spend and FX both uncertain
    level: float


def stress(
    mtd_inr: float,
    remaining_usd_paths: np.ndarray,
    spot: float,
    daily_vol: float,
    remaining_days: int,
    *,
    shocks: tuple[float, ...] = (0.05, 0.10),
    level: float = 0.95,
    seed: int = 0,
) -> FxStress:
    """INR month-end under FX shocks. ``remaining_usd_paths``: simulated USD for rest of month."""
    rng = np.random.default_rng(seed)
    exp_usd = float(remaining_usd_paths.mean())
    expected = mtd_inr + exp_usd * spot
    rows = [{"scenario": "base", "rate": spot, "inr_total": expected, "change_vs_expected": 0.0}]
    for s in shocks:
        rate = spot * (1 + s)
        total = mtd_inr + exp_usd * rate
        rows.append({"scenario": f"INR -{s:.0%}", "rate": rate, "inr_total": total,
                     "change_vs_expected": total - expected})  # fmt: skip
    # Spend made through the month is converted along the path; the average rate over the
    # remaining days has ~1/sqrt(3) of the end-point volatility (Brownian average).
    sigma = daily_vol * np.sqrt(max(remaining_days, 1)) / np.sqrt(3)
    rate_paths = spot * np.exp(rng.normal(-0.5 * sigma**2, sigma, size=len(remaining_usd_paths)))
    totals = mtd_inr + remaining_usd_paths * rate_paths
    return FxStress(spot, expected, pd.DataFrame(rows), float(np.quantile(totals, level)), level)
