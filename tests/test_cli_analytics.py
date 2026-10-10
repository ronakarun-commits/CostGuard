from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from costguard.cli import app

runner = CliRunner()


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = str(tmp_path_factory.mktemp("cli") / "cg.duckdb")
    result = runner.invoke(app, ["--db", path, "generate", "--days", "240", "--end", "2026-06-20",
                                 "--seed", "7"])  # fmt: skip
    assert result.exit_code == 0, result.output
    return path


def _json(db: str, *args: str) -> Any:
    result = runner.invoke(app, ["--db", db, *args, "-f", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_anomalies(db: str) -> None:
    data = _json(db, "anomalies", "--all")
    assert data["kpis"][0]["value"] >= 1
    assert data["sections"][0]["rows"][0]["excess_cost"] > 0
    assert runner.invoke(app, ["--db", db, "anomalies", "-m", "stl"]).exit_code == 0


def test_evaluate(db: str) -> None:
    data = _json(db, "evaluate", "-m", "baseline")
    row = data["sections"][0]["rows"][0]
    assert row["method"] == "baseline"
    assert row["recall"] > 0.5
    assert runner.invoke(app, ["--db", db, "evaluate", "-m", "magic"]).exit_code == 2


def test_forecast(db: str) -> None:
    data = _json(db, "forecast", "--days", "14", "--paths", "300")
    assert data["period"] == ["2026-06-21", "2026-07-04"]
    rows = data["sections"][0]["rows"]
    assert all(r["p5"] <= r["median"] <= r["p95"] for r in rows)
    assert sum(r["days"] for r in rows) == 14


def test_risk(db: str) -> None:
    data = _json(db, "risk", "--budget", "1", "--paths", "1000", "--no-calibrate")
    k = {x["label"]: x["value"] for x in data["kpis"]}
    assert k["P(over budget)"] == 1.0
    assert k["Cost-at-Risk (95%)"] >= k["Expected month-end"] >= k["Month to date"]


def test_backtest(db: str) -> None:
    data = _json(db, "backtest")
    rows = {r["bounds"]: r for r in data["sections"][0]["rows"]}
    assert rows["conformal"]["exceedances"] <= rows["raw model"]["exceedances"]
    assert data["sections"][1]["rows"]


def test_variance(db: str) -> None:
    data = _json(db, "variance")
    assert "May 2026 vs Apr 2026" in data["title"]
    bridge = data["sections"][0]["rows"]
    start, end = bridge[0]["amount"], bridge[-1]["amount"]
    assert sum(r["amount"] for r in bridge[1:-1]) == pytest.approx(end - start, abs=0.05)
    explicit = _json(db, "variance", "--current", "2026-03", "--base", "2026-01", "--budget", "100")
    assert any(k["label"] == "Budget variance" for k in explicit["kpis"])
    assert runner.invoke(app, ["--db", db, "variance", "--current", "March"]).exit_code == 2
    assert runner.invoke(app, ["--db", db, "variance", "--current", "2030-01"]).exit_code == 3


def test_commitments(db: str) -> None:
    data = _json(db, "commitments", "--paths", "300")
    assert len(data["sections"][0]["rows"]) == 6
    assert data["kpis"][0]["value"]


def test_fx_synthetic_and_inr(db: str) -> None:
    data = _json(db, "fx", "--fx-source", "synthetic", "--stress", "7")
    scenarios = [r["scenario"] for r in data["sections"][0]["rows"]]
    assert scenarios == ["base", "INR -7%"]
    bridge = data["sections"][1]["rows"]
    for r in bridge[1:]:
        assert r["usage_effect"] + r["fx_effect"] == pytest.approx(r["change_inr"], rel=1e-9)


def test_fx_ecb_uses_downloaded_rates(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    import costguard.providers.fx_ecb as ecb

    days = pd.bdate_range("2025-10-01", "2026-06-20")
    payload = {"rates": {d.strftime("%Y-%m-%d"): {"INR": 84.0} for d in days}}

    class FakeResponse(io.BytesIO):
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    calls: list[str] = []

    def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
        calls.append(req.full_url)
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(ecb.urllib.request, "urlopen", fake_urlopen)
    data = _json(db, "fx", "--fx-source", "ecb")
    assert calls and calls[0].startswith("https://api.frankfurter.dev/")
    assert data["kpis"][0]["value"] == "84.00"
    _json(db, "fx", "--fx-source", "ecb")  # cached now
    assert len(calls) == 1


def test_fx_download_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    import costguard.providers.fx_ecb as ecb

    def boom(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(ecb.urllib.request, "urlopen", boom)
    path = str(tmp_path / "x.duckdb")
    runner.invoke(app, ["--db", path, "generate", "--days", "60"])
    result = runner.invoke(app, ["--db", path, "fx"])
    assert result.exit_code == 6
    assert "synthetic" in result.output


def test_fx_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    import costguard.providers.fx_ecb as ecb

    class R(io.BytesIO):
        def __enter__(self) -> R:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr(ecb.urllib.request, "urlopen", lambda req, timeout: R(b'{"rates": {}}'))
    with pytest.raises(ecb.FxError, match="No USD/INR"):
        ecb.fetch_rates(date(2026, 1, 1), date(2026, 1, 5))


def test_short_history_errors(tmp_path: Path) -> None:
    path = str(tmp_path / "short.duckdb")
    runner.invoke(app, ["--db", path, "generate", "--days", "40"])
    assert runner.invoke(app, ["--db", path, "backtest"]).exit_code == 3
    assert runner.invoke(app, ["--db", path, "commitments"]).exit_code == 3
    # 40 days is inside the 60-day warm-up, so no anomalies were injected to evaluate against
    assert runner.invoke(app, ["--db", path, "evaluate"]).exit_code == 3
