import subprocess
import sys

import pytest
from typer.testing import CliRunner

from costguard import __version__
from costguard.cli import app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"costguard {__version__}"


def test_short_version_flag() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_info_command() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "python" in result.stdout


def test_version_is_not_placeholder() -> None:
    assert __version__ != "0.0.0+unknown"


def test_module_entrypoint() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "costguard", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in proc.stdout


def test_generate_show_labels(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "cg.duckdb")
    result = runner.invoke(app, ["--db", db, "generate", "--days", "120", "--end", "2026-06-30"])
    assert result.exit_code == 0, result.output
    assert "labelled anomalies" in result.stdout

    for by in ("service", "account", "month"):
        result = runner.invoke(app, ["--db", db, "show", "--by", by])
        assert result.exit_code == 0, result.output
        assert "Total" in result.stdout

    result = runner.invoke(app, ["--db", db, "show", "--all"])
    assert "2026-03-03" in result.stdout

    result = runner.invoke(app, ["--db", db, "labels"])
    assert result.exit_code == 0
    assert "A001" in result.stdout


def test_show_without_data_fails(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "empty.duckdb")
    assert runner.invoke(app, ["--db", db, "show"]).exit_code == 3
    assert runner.invoke(app, ["--db", db, "labels"]).exit_code == 3


def test_show_rejects_bad_dimension(tmp_path: object) -> None:
    result = runner.invoke(app, ["show", "--by", "nope"])
    assert result.exit_code == 2


def _gen(db: str) -> None:
    result = runner.invoke(app, ["--db", db, "generate", "--days", "120", "--end", "2026-06-30"])
    assert result.exit_code == 0, result.output


def test_dashboard_formats(tmp_path: object) -> None:
    import json
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "cg.duckdb")
    _gen(db)
    table = runner.invoke(app, ["--db", db, "dashboard", "--budget", "20000"])
    assert table.exit_code == 0, table.output
    assert "Month to date" in table.stdout
    data = json.loads(runner.invoke(app, ["--db", db, "dashboard", "-f", "json"]).stdout)
    assert data["period"] == ["2026-06-01", "2026-06-30"]
    out = Path(str(tmp_path)) / "r" / "dash.html"
    result = runner.invoke(app, ["--db", db, "dashboard", "-f", "html", "-o", str(out)])
    assert result.exit_code == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_currency_inr(tmp_path: object) -> None:
    import json
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "cg.duckdb")
    _gen(db)
    usd = json.loads(runner.invoke(app, ["--db", db, "dashboard", "-f", "json"]).stdout)
    inr = json.loads(
        runner.invoke(app, ["--db", db, "-c", "INR", "dashboard", "-f", "json"]).stdout
    )
    assert inr["currency"] == "INR"
    assert inr["kpis"][0]["value"] == pytest.approx(usd["kpis"][0]["value"] * 88.0)
    assert "₹" in runner.invoke(app, ["--db", db, "-c", "INR", "show"]).stdout


def test_trend_cli(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "cg.duckdb")
    _gen(db)
    result = runner.invoke(app, ["--db", db, "trend", "--by", "account", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert "acme-prod" in result.stdout
    assert runner.invoke(app, ["--db", db, "trend", "--by", "month"]).exit_code == 2
    assert runner.invoke(app, ["--db", db, "trend", "--freq", "week"]).exit_code == 0


def test_reports_without_data_exit_3(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "none.duckdb")
    for cmd in ("dashboard", "trend"):
        result = runner.invoke(app, ["--db", db, cmd])
        assert result.exit_code == 3
        assert "costguard generate" in result.output


def test_audit_synthetic() -> None:
    import json

    result = runner.invoke(app, ["audit", "-f", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["kpis"][0]["value"] > 0
    assert runner.invoke(app, ["audit", "--rule", "eip-idle"]).exit_code == 0
    assert runner.invoke(app, ["audit", "--rule", "bogus"]).exit_code == 2
    assert runner.invoke(app, ["audit", "--fail-over", "1"]).exit_code == 10
    assert runner.invoke(app, ["audit", "--fail-over", "1000000"]).exit_code == 0


def test_rules_and_info() -> None:
    assert "ebs-unattached" in runner.invoke(app, ["rules"]).stdout
    assert "currency" in runner.invoke(app, ["info"]).stdout


def test_bad_config_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSTGUARD_REPORTING_CURRENCY", "EUR")
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 2
    assert "reporting_currency" in result.output


def test_verbose_json_logging(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "cg.duckdb")
    result = runner.invoke(app, ["--db", db, "-v", "--log-json", "generate", "--days", "30"])
    assert result.exit_code == 0


def test_output_works_with_windows_legacy_encoding(tmp_path: object) -> None:
    """Windows pipes default to cp1252, which can't encode ✓ or →. The CLI must not crash."""
    import os
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "enc.duckdb")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    for args in (["generate", "--days", "40"], ["show"], ["-c", "INR", "dashboard"]):
        proc = subprocess.run(  # noqa: S603 - fixed, trusted arguments
            [sys.executable, "-m", "costguard", "--db", db, *args],
            capture_output=True, env=env, check=False,
        )  # fmt: skip
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-500:]
    assert "₹".encode() in proc.stdout
