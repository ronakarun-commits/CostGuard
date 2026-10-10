from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from costguard.providers.synthetic import SyntheticProvider
from costguard.storage.duckdb_repo import DuckDBRepository


@pytest.fixture(scope="session")
def world() -> tuple[SyntheticProvider, pd.DataFrame, pd.Series]:
    p = SyntheticProvider(date(2025, 10, 10), 365, seed=42)
    with DuckDBRepository() as r:
        r.upsert_costs(p.get_costs(p.world), "synthetic")
        return p, r.costs_frame(), r.daily_series()["cost"].asfreq("D")


@pytest.fixture
def smooth() -> pd.Series:
    """Clean weekly-seasonal series: 100/day weekdays, 80 weekends, 0.5% noise."""
    import numpy as np

    idx = pd.date_range("2026-01-01", periods=200, freq="D")
    rng = np.random.default_rng(0)
    base = np.where(idx.dayofweek >= 5, 80.0, 100.0)
    return pd.Series(base * rng.lognormal(0, 0.005, len(idx)), index=idx)
