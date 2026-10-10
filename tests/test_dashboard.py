from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import costguard.interfaces.dashboard_app as dash_module

SCRIPT = str(Path(dash_module.__file__))


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> str:
    from datetime import date

    from costguard.providers.synthetic import SyntheticProvider
    from costguard.storage.duckdb_repo import DuckDBRepository

    path = tmp_path_factory.mktemp("dash") / "cg.duckdb"
    p = SyntheticProvider(date(2026, 1, 1), 150, seed=5)
    with DuckDBRepository(path) as r:
        r.upsert_costs(p.get_costs(p.world), "synthetic")
    return str(path)


def test_dashboard_renders(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSTGUARD_DB_PATH", db)
    at = AppTest.from_file(SCRIPT, default_timeout=60).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Month to date" in labels
    assert any(lbl.startswith("Cost-at-Risk") for lbl in labels)

    at.sidebar.radio[0].set_value("INR").run()
    assert not at.exception
    assert "₹" in at.metric[0].value


def test_dashboard_missing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSTGUARD_DB_PATH", str(tmp_path / "none.duckdb"))
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert at.error
    assert "not found" in at.error[0].value
