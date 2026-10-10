from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from costguard.core.finance.commitments import (
    DEFAULT_OPTIONS,
    CoverageCurve,
    PlanOption,
    UsageModel,
    covered_fraction,
    evaluate_plan,
    recommend,
)
from costguard.core.finance.fx import (
    daily_rates,
    fx_vol,
    monthly_bridge,
    stress,
    synthetic_rates,
)
from costguard.core.finance.variance import price_volume
from costguard.core.models import DateRange

FEB = DateRange(date(2026, 2, 1), date(2026, 3, 1))
MAR = DateRange(date(2026, 3, 1), date(2026, 4, 1))


def _costs(rows: list[tuple[Any, ...]]) -> pd.DataFrame:
    """rows: (date, service, sku, unit, quantity, cost) — spread evenly over the month."""
    out = []
    for month, svc, sku, unit, qty, cost in rows:
        days = pd.date_range(month, periods=pd.Timestamp(month).days_in_month)
        for d in days:
            out.append({"charge_date": d, "service_name": svc, "sku_id": sku,
                        "consumed_unit": unit, "consumed_quantity": qty / len(days),
                        "billed_cost": cost / len(days)})  # fmt: skip
    return pd.DataFrame(out)


def test_price_volume_bridge_sums_exactly() -> None:
    costs = _costs([
        ("2026-02-01", "EC2", "box", "Hrs", 280, 28.0),     # 10/day at $0.10
        ("2026-03-01", "EC2", "box", "Hrs", 372, 44.64),    # 12/day at $0.12
        ("2026-02-01", "S3", "gb", "GB-Mo", 100, 2.3),
        ("2026-03-01", "Athena", "tb", "TB", 1, 5.0),       # new
        ("2026-02-01", "Tax", "ALL", "Mixed", 1, 1.0),      # mixed unit
        ("2026-03-01", "Tax", "ALL", "Mixed", 1, 3.0),
    ])  # fmt: skip
    v = price_volume(costs, FEB, MAR)
    t = v.totals
    effects = sum(
        t[k] for k in ("calendar", "volume", "price", "new", "discontinued", "unattributed")
    )
    assert effects == pytest.approx(t["change"])
    ec2 = v.lines.set_index("service_name").loc["EC2"]
    assert ec2["calendar"] == pytest.approx(28.0 * (31 / 28 - 1))
    assert ec2["volume"] == pytest.approx((372 - 280 * 31 / 28) * 0.10)
    assert ec2["price"] == pytest.approx((0.12 - 0.10) * 372)
    assert t["new"] == pytest.approx(5.0)
    assert t["discontinued"] == pytest.approx(-2.3)
    assert t["unattributed"] == pytest.approx(2.0)
    assert set(v.by_service["service_name"]) == {"EC2", "S3", "Athena", "Tax"}


def test_price_volume_on_synthetic_finds_price_anomaly(
    loaded_repo: Any,
) -> None:
    costs = loaded_repo.costs_frame()
    labels = [a for a in loaded_repo.get_anomalies() if a.kind.value == "price_change"]
    assert labels, "seed 7 contains a price change"
    a = labels[0]
    month = pd.Timestamp(a.start_date) + pd.offsets.MonthBegin(1)  # first full month inside
    cur = DateRange(month.date(), (month + pd.offsets.MonthBegin(1)).date())
    base = DateRange(
        (month - pd.offsets.MonthBegin(2)).date(), (month - pd.offsets.MonthBegin(1)).date()
    )
    v = price_volume(costs, base, cur)
    svc = v.by_service.set_index("service_name").loc[a.service_name]
    assert svc["price"] > 0
    others = v.by_service[v.by_service["service_name"] != a.service_name]["price"].abs().max()
    assert svc["price"] > others


def test_coverage_curve_matches_brute_force() -> None:
    r = np.random.default_rng(1).lognormal(0, 0.3, 300)
    curve = CoverageCurve(r)
    for x in (0.0, 0.3, 0.8, 1.0, 1.7, 6.0):
        assert float(curve(np.array(x))) == pytest.approx(
            np.minimum(r, x).mean() / r.mean(), abs=1e-3
        )
    frac = covered_fraction(np.array([50.0, 0.0]), np.array([100.0, 0.0]), r)
    assert frac[1] == 0.0
    assert 0 < frac[0] < 0.5


def test_plan_economics_flat_usage() -> None:
    opt = PlanOption("1y", 12, "no_upfront", 0.30)
    flat = np.full((10, 12), 7300.0)  # $10/h on-demand, every month
    ratios = np.ones(30)
    # commit $7/h buys exactly $10/h of on-demand capacity: full coverage, 100% utilisation
    ev = evaluate_plan(opt, 7.0, flat, ratios, annual_rate=0.0)
    assert ev.coverage_mean == pytest.approx(1.0)
    assert ev.utilisation_mean == pytest.approx(1.0)
    assert ev.annual_saving_mean == pytest.approx(0.30 * 7300 * 12)
    assert ev.npv_mean == pytest.approx(0.30 * 7300 * 12)
    assert ev.prob_loss == 0.0
    # utilisation exactly at break-even (1 - discount): no gain, no loss
    even = evaluate_plan(opt, 10.0, flat, ratios, annual_rate=0.0)
    assert even.utilisation_mean == pytest.approx(opt.break_even_utilisation)
    assert even.npv_mean == pytest.approx(0.0, abs=1e-6)
    # beyond it, over-committing loses money
    over = evaluate_plan(opt, 12.0, flat, ratios, annual_rate=0.0)
    assert over.npv_mean == pytest.approx(-(12 - 10) * 730 * 12)
    assert over.prob_loss == 1.0
    upfront = evaluate_plan(PlanOption("au", 12, "all_upfront", 0.30), 7.0, flat, ratios, 0.10)
    no_upfront = evaluate_plan(opt, 7.0, flat, ratios, 0.10)
    assert upfront.upfront == pytest.approx(7.0 * 730 * 12)
    assert upfront.npv_mean < no_upfront.npv_mean  # same discount, paying earlier costs NPV


def test_usage_model_and_recommend() -> None:
    idx = pd.date_range("2025-06-01", periods=300)
    daily = pd.Series(
        np.where(idx.dayofweek >= 5, 200.0, 300.0) * 1.02 ** (np.arange(300) / 30), idx
    )
    m = UsageModel.from_daily(daily)
    assert m.monthly_drift == pytest.approx(np.log(1.02), abs=0.01)
    assert m.daily_ratios.min() < 0.8
    plans = recommend(daily, n_paths=200, grid_points=11)
    assert [p.option.name for p in plans] == [o.name for o in DEFAULT_OPTIONS]
    assert all(0 < p.hedge_ratio <= 1.2 for p in plans)
    assert all(p.npv_p5 > 0 for p in plans)
    robust = {p.option.name: p.hourly_commitment for p in plans}
    greedy = {p.option.name: p.hourly_commitment
              for p in recommend(daily, n_paths=200, grid_points=11, objective="mean")}  # fmt: skip
    assert all(robust[k] <= greedy[k] + 1e-9 for k in robust)


def test_fx_bridge_decomposition() -> None:
    idx = pd.date_range("2026-01-01", "2026-03-31")
    usd = pd.Series(100.0, index=idx)
    usd["2026-03-01":] = 120.0
    rates = pd.Series(80.0, index=pd.bdate_range("2025-12-25", "2026-03-31"))
    rates["2026-03-01":] = 84.0
    b = monthly_bridge(usd, rates).table.set_index("month")
    mar = b.loc["2026-03"]
    assert mar["usage_effect"] == pytest.approx((120 - 100) * 31 * 80 + 100 * 3 * 80)
    assert mar["usage_effect"] + mar["fx_effect"] == pytest.approx(mar["change_inr"])
    # 1 Mar 2026 is a Sunday: it uses Friday's rate (80), so 30 days are at 84
    assert mar["fx_effect"] == pytest.approx(120 * 30 * 4)
    # partial first/last months are dropped
    partial = monthly_bridge(usd["2026-01-15":"2026-03-20"], rates).table
    assert list(partial["month"]) == ["2026-02"]


def test_rates_helpers() -> None:
    r = synthetic_rates(pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-31"), spot=88)
    assert r.iloc[-1] == pytest.approx(88.0)
    assert 0.001 < fx_vol(r) < 0.006
    assert fx_vol(r.iloc[:3]) == 0.003
    full = daily_rates(r, pd.date_range("2026-01-01", "2026-03-31"))
    assert full.isna().sum() == 0
    sat = full[pd.Timestamp("2026-01-03")]  # Saturday -> Friday's rate
    assert sat == pytest.approx(r[pd.Timestamp("2026-01-02")])


def test_fx_stress() -> None:
    paths = np.full(1000, 1000.0)
    s = stress(50_000, paths, 88.0, 0.004, 15, shocks=(0.05,))
    assert s.expected_inr == pytest.approx(50_000 + 88_000)
    shocked = s.shocks.set_index("scenario").loc["INR -5%"]
    assert shocked["change_vs_expected"] == pytest.approx(1000 * 88 * 0.05)
    assert s.car_inr > s.expected_inr
