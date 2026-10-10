from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from costguard.core.models import DateRange
from costguard.providers.synthetic import SyntheticProvider
from costguard.storage.duckdb_repo import DuckDBRepository


def test_migrations_applied(repo: DuckDBRepository) -> None:
    assert repo.schema_version == 2


def test_migrations_idempotent_on_reopen(db_path: Path) -> None:
    with DuckDBRepository(db_path) as r:
        assert r.schema_version == 2
    with DuckDBRepository(db_path) as r:
        assert r.schema_version == 2


def test_roundtrip(loaded_repo: DuckDBRepository, provider: SyntheticProvider) -> None:
    period = DateRange(date(2025, 2, 1), date(2025, 2, 3))
    original = provider.get_costs(period)
    stored = loaded_repo.get_costs(period)
    assert len(stored) == len(original)
    assert stored[0].billed_cost == pytest.approx(original[0].billed_cost)
    assert stored[0].tags == original[0].tags


def test_upsert_is_idempotent(loaded_repo: DuckDBRepository, provider: SyntheticProvider) -> None:
    before = len(loaded_repo.get_costs())
    loaded_repo.upsert_costs(provider.get_costs(provider.world), source=provider.name)
    assert len(loaded_repo.get_costs()) == before


def test_upsert_empty(repo: DuckDBRepository) -> None:
    assert repo.upsert_costs([], source="x") == 0
    assert repo.date_bounds() is None


def test_anomalies_roundtrip(loaded_repo: DuckDBRepository, provider: SyntheticProvider) -> None:
    assert list(loaded_repo.get_anomalies()) == provider.get_anomalies(provider.world)


def test_delete_source(loaded_repo: DuckDBRepository) -> None:
    loaded_repo.delete_source("synthetic")
    assert loaded_repo.get_costs() == []
    assert loaded_repo.get_anomalies() == []


def test_summary_matches_python(loaded_repo: DuckDBRepository, provider: SyntheticProvider) -> None:
    df = loaded_repo.summary("account")
    assert df["cost"].sum() == pytest.approx(
        sum(r.billed_cost for r in provider.get_costs(provider.world))
    )
    assert list(df["cost"]) == sorted(df["cost"], reverse=True)


def test_summary_month_ordered(loaded_repo: DuckDBRepository) -> None:
    keys = list(loaded_repo.summary("month")["key"])
    assert keys == sorted(keys)
    assert len(keys) == 12


def test_summary_rejects_unknown_dimension(loaded_repo: DuckDBRepository) -> None:
    with pytest.raises(ValueError, match="cannot group"):
        loaded_repo.summary("1; DROP TABLE cost_records")  # type: ignore[arg-type]


def test_daily_series(loaded_repo: DuckDBRepository) -> None:
    total = loaded_repo.daily_series()
    assert len(total) == 365
    wide = loaded_repo.daily_series(by="account")
    assert wide.shape == (365, 4)
    assert wide.sum(axis=1).to_numpy() == pytest.approx(total["cost"].to_numpy())
    with pytest.raises(ValueError, match="cannot group"):
        loaded_repo.daily_series(by="bogus")  # type: ignore[arg-type]


def test_costs_frame(loaded_repo: DuckDBRepository) -> None:
    df = loaded_repo.costs_frame(DateRange(date(2025, 1, 1), date(2025, 1, 2)))
    assert "billed_cost" in df.columns
    assert df["charge_date"].nunique() == 1


def test_date_bounds(loaded_repo: DuckDBRepository) -> None:
    assert loaded_repo.date_bounds() == (date(2025, 1, 1), date(2025, 12, 31))


def test_cache(repo: DuckDBRepository) -> None:
    assert repo.cache_get("k") is None
    repo.cache_put("k", "GetCostAndUsage", '{"a": 1}')
    assert repo.cache_get("k") == '{"a": 1}'
