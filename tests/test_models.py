from datetime import date

import pytest

from costguard.core.models import AnomalyKind, CostRecord, DateRange, InjectedAnomaly


def _record(cost: float, qty: float) -> CostRecord:
    return CostRecord(
        charge_date=date(2026, 1, 1),
        sub_account_id="1",
        sub_account_name="a",
        service_name="s",
        service_category="Compute",
        region_id="ap-south-1",
        sku_id="sku",
        consumed_quantity=qty,
        consumed_unit="Hrs",
        billed_cost=cost,
    )


def test_unit_price() -> None:
    assert _record(10.0, 4.0).unit_price == 2.5


def test_unit_price_zero_usage() -> None:
    assert _record(1.0, 0.0).unit_price == 0.0


def test_records_are_immutable() -> None:
    r = _record(1.0, 1.0)
    with pytest.raises(AttributeError):
        r.billed_cost = 2.0  # type: ignore[misc]


def test_date_range_days_and_validation() -> None:
    assert DateRange(date(2026, 1, 1), date(2026, 2, 1)).days == 31
    with pytest.raises(ValueError, match="must be after"):
        DateRange(date(2026, 1, 2), date(2026, 1, 1))


def test_anomaly_covers_is_inclusive() -> None:
    a = InjectedAnomaly(
        "A1", AnomalyKind.SPIKE, "1", "s", date(2026, 1, 5), date(2026, 1, 6), 3.0, "x"
    )
    assert a.covers(date(2026, 1, 5))
    assert a.covers(date(2026, 1, 6))
    assert not a.covers(date(2026, 1, 7))
