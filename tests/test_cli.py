import subprocess
import sys

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
    result = runner.invoke(app, ["generate", "--db", db, "--days", "120", "--end", "2026-06-30"])
    assert result.exit_code == 0, result.output
    assert "labelled anomalies" in result.stdout

    for by in ("service", "account", "month"):
        result = runner.invoke(app, ["show", "--db", db, "--by", by])
        assert result.exit_code == 0, result.output
        assert "Total" in result.stdout

    result = runner.invoke(app, ["show", "--db", db, "--all"])
    assert "2026-03-03" in result.stdout

    result = runner.invoke(app, ["labels", "--db", db])
    assert result.exit_code == 0
    assert "A001" in result.stdout


def test_show_without_data_fails(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "empty.duckdb")
    assert runner.invoke(app, ["show", "--db", db]).exit_code == 1
    assert runner.invoke(app, ["labels", "--db", db]).exit_code == 1


def test_show_rejects_bad_dimension(tmp_path: object) -> None:
    result = runner.invoke(app, ["show", "--by", "nope"])
    assert result.exit_code == 2
