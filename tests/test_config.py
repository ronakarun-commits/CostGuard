from __future__ import annotations

from pathlib import Path

import pytest

from costguard.config import load_settings
from costguard.errors import ConfigError


def test_defaults() -> None:
    s = load_settings()
    assert s.db_path == Path(".costguard") / "costguard.duckdb"
    assert s.reporting_currency == "USD"
    assert s.budget_monthly_usd is None


def test_precedence_flags_over_env_over_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    Path("costguard.toml").write_text(
        'budget_monthly_usd = 100\nreporting_currency = "INR"\naws_regions = ["eu-west-1"]\n'
    )
    s = load_settings()
    assert (s.budget_monthly_usd, s.reporting_currency, s.aws_regions) == (
        100,
        "INR",
        ["eu-west-1"],
    )

    monkeypatch.setenv("COSTGUARD_BUDGET_MONTHLY_USD", "200")
    monkeypatch.setenv("COSTGUARD_AWS_REGIONS", "us-east-1, ap-south-1")
    s = load_settings()
    assert s.budget_monthly_usd == 200
    assert s.aws_regions == ["us-east-1", "ap-south-1"]

    assert load_settings(reporting_currency="USD").reporting_currency == "USD"


def test_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text("COSTGUARD_LOG_LEVEL=DEBUG\n")
    assert load_settings().log_level == "DEBUG"


def test_secrets_are_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSTGUARD_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/SECRET")
    s = load_settings()
    assert "SECRET" not in repr(s)
    assert s.slack_webhook_url is not None
    assert s.slack_webhook_url.get_secret_value().endswith("SECRET")


def test_invalid_config_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSTGUARD_BUDGET_MONTHLY_USD", "lots")
    with pytest.raises(ConfigError, match="budget_monthly_usd"):
        load_settings()


def test_none_overrides_are_ignored() -> None:
    assert load_settings(db_path=None).db_path.name == "costguard.duckdb"


def test_unparseable_list_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSTGUARD_API_KEYS", "not-json")
    with pytest.raises(ConfigError):
        load_settings()
