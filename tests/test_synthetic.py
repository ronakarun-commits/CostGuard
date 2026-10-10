from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pytest

from costguard.core.models import AnomalyKind, CostRecord, DateRange
from costguard.core.ports import CostProvider, LabelledCostProvider
from costguard.providers.synthetic import ACCOUNTS, WARMUP_DAYS, SyntheticProvider


def _series(records: list[CostRecord], acct: str, svc: str) -> dict[date, CostRecord]:
    return {r.charge_date: r for r in records if r.sub_account_id == acct and r.service_name == svc}


def test_conforms_to_ports(provider: SyntheticProvider) -> None:
    assert isinstance(provider, CostProvider)
    assert isinstance(provider, LabelledCostProvider)


def test_same_seed_same_data() -> None:
    a = SyntheticProvider(date(2025, 1, 1), days=120, seed=1)
    b = SyntheticProvider(date(2025, 1, 1), days=120, seed=1)
    assert a.get_costs(a.world) == b.get_costs(b.world)
    assert a.get_anomalies(a.world) == b.get_anomalies(b.world)


def test_different_seed_different_data() -> None:
    a = SyntheticProvider(date(2025, 1, 1), days=60, seed=1)
    b = SyntheticProvider(date(2025, 1, 1), days=60, seed=2)
    assert a.get_costs(a.world) != b.get_costs(b.world)


def test_period_filter_is_half_open(provider: SyntheticProvider) -> None:
    period = DateRange(date(2025, 3, 1), date(2025, 3, 8))
    days = {r.charge_date for r in provider.get_costs(period)}
    assert min(days) == date(2025, 3, 1)
    assert max(days) == date(2025, 3, 7)
    assert len(days) == 7


def test_one_record_per_grain(provider: SyntheticProvider) -> None:
    records = provider.get_costs(provider.world)
    keys = [(r.charge_date, r.sub_account_id, r.service_name, r.region_id) for r in records]
    assert len(keys) == len(set(keys))
    assert {r.sub_account_id for r in records} == {a.id for a in ACCOUNTS}


def test_costs_positive_and_consistent(provider: SyntheticProvider) -> None:
    for r in provider.get_costs(provider.world):
        assert r.billed_cost > 0
        assert r.consumed_quantity > 0
        assert r.billing_currency == "USD"
        assert r.tags["env"] in {"prod", "staging", "dev", "data"}


def test_weekends_are_cheaper_for_compute(provider: SyntheticProvider) -> None:
    ec2 = _series(provider.get_costs(provider.world), "111111111111",
                  "Amazon Elastic Compute Cloud - Compute")  # fmt: skip
    weekday = [r.billed_cost for d, r in ec2.items() if d.weekday() < 5 and d.month < 3]
    weekend = [r.billed_cost for d, r in ec2.items() if d.weekday() >= 5 and d.month < 3]
    assert sum(weekend) / len(weekend) < 0.92 * sum(weekday) / len(weekday)


def test_anomalies_after_warmup_and_labelled(provider: SyntheticProvider) -> None:
    anomalies = provider.get_anomalies(provider.world)
    assert len(anomalies) >= 5
    assert [a.id for a in anomalies] == [f"A{i:03d}" for i in range(1, len(anomalies) + 1)]
    for a in anomalies:
        assert a.start_date >= provider.world.start + timedelta(days=WARMUP_DAYS)
        assert a.start_date <= a.end_date


def test_anomalies_do_not_overlap_per_series(provider: SyntheticProvider) -> None:
    spans: dict[tuple[str, str], list[tuple[date, date]]] = defaultdict(list)
    for a in provider.get_anomalies(provider.world):
        for s, e in spans[(a.sub_account_id, a.service_name)]:
            assert a.end_date < s or a.start_date > e
        spans[(a.sub_account_id, a.service_name)].append((a.start_date, a.end_date))


def test_injected_effects_visible_in_data(provider: SyntheticProvider) -> None:
    records = provider.get_costs(provider.world)
    seen = set()
    for a in provider.get_anomalies(provider.world):
        series = _series(records, a.sub_account_id, a.service_name)
        before = [series[a.start_date - timedelta(days=k)] for k in range(8, 22)]
        inside = series[a.end_date]
        base_q = sum(r.consumed_quantity for r in before) / len(before)
        base_p = sum(r.unit_price for r in before) / len(before)
        match a.kind:
            case AnomalyKind.SPIKE | AnomalyKind.LEVEL_SHIFT:
                assert inside.consumed_quantity > 1.25 * base_q, a
            case AnomalyKind.DRIFT:
                assert inside.consumed_quantity > 1.25 * base_q, a
            case AnomalyKind.PRICE_CHANGE:
                assert inside.unit_price == pytest.approx(base_p * a.magnitude, rel=0.01)
        seen.add(a.kind)
    assert AnomalyKind.SPIKE in seen


def test_anomaly_period_filter(provider: SyntheticProvider) -> None:
    every = provider.get_anomalies(provider.world)
    first = every[0]
    hits = provider.get_anomalies(DateRange(first.end_date, first.end_date + timedelta(days=1)))
    assert first in hits


def test_short_world_has_no_anomalies() -> None:
    p = SyntheticProvider(date(2025, 1, 1), days=30)
    assert p.get_anomalies(p.world) == []


def test_zero_days_rejected() -> None:
    with pytest.raises(ValueError, match="days"):
        SyntheticProvider(date(2025, 1, 1), days=0)
