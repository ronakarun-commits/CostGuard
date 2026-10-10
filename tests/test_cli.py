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
