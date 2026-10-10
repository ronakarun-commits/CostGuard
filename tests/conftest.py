from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from costguard.providers.synthetic import SyntheticProvider
from costguard.storage.duckdb_repo import DuckDBRepository

START = date(2025, 1, 1)


@pytest.fixture(scope="session")
def provider() -> SyntheticProvider:
    """One year of synthetic data. Session-scoped: generation is deterministic and read-only."""
    return SyntheticProvider(start=START, days=365, seed=7)


@pytest.fixture
def repo() -> Iterator[DuckDBRepository]:
    with DuckDBRepository(":memory:") as r:
        yield r


@pytest.fixture
def loaded_repo(repo: DuckDBRepository, provider: SyntheticProvider) -> DuckDBRepository:
    repo.upsert_costs(provider.get_costs(provider.world), source=provider.name)
    repo.upsert_anomalies(provider.get_anomalies(provider.world))
    return repo


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cg.duckdb"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never pick up the developer's costguard.toml / .env / COSTGUARD_* variables."""
    import os

    for var in list(os.environ):
        if var.startswith("COSTGUARD_"):
            monkeypatch.delenv(var)
    monkeypatch.chdir(tmp_path)


def flat(text: str) -> str:
    """Collapse Rich line-wrapping so assertions don't depend on terminal width."""
    return " ".join(text.split())
