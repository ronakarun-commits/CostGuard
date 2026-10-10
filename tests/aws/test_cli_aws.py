from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from moto import mock_aws
from typer.testing import CliRunner

from costguard.cli import app
from costguard.core.models import CostRecord, DateRange
from costguard.providers.aws.cost_explorer import CostExplorerProvider

runner = CliRunner()


@mock_aws
def test_whoami_cli() -> None:
    result = runner.invoke(app, ["aws", "whoami"])
    assert result.exit_code == 0, result.output
    assert "123456789012" in result.stdout


@mock_aws
def test_budgets_cli_empty() -> None:
    result = runner.invoke(app, ["aws", "budgets"])
    assert result.exit_code == 0, result.output
    assert "No cost budgets" in result.stdout


@mock_aws
def test_budgets_cli_lists() -> None:
    import boto3

    boto3.client("budgets", region_name="us-east-1").create_budget(
        AccountId="123456789012",
        Budget={"BudgetName": "b", "BudgetLimit": {"Amount": "40", "Unit": "USD"},
                "TimeUnit": "MONTHLY", "BudgetType": "COST"},
    )  # fmt: skip
    assert "40.00 USD" in runner.invoke(app, ["aws", "budgets"]).stdout


def test_bad_profile_gives_auth_exit_code() -> None:
    result = runner.invoke(app, ["aws", "whoami", "--profile", "nope"])
    assert result.exit_code == 4
    assert "Hint" in result.output


def test_pull_stores_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = CostRecord(date(2026, 1, 1), "1", "acme", "AWS Lambda", "Compute", "global", "ALL",
                        5.0, "Mixed", 1.5)  # fmt: skip

    def fake_get_costs(self: CostExplorerProvider, period: DateRange) -> list[CostRecord]:
        self.paid_requests = 1
        return [record]

    monkeypatch.setattr(CostExplorerProvider, "get_costs", fake_get_costs)
    db = str(tmp_path / "aws.duckdb")
    result = runner.invoke(app, ["pull", "--db", db, "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "Requests: 1 paid" in " ".join(result.stdout.split())
    show = runner.invoke(app, ["show", "--db", db, "--all"])
    assert "AWS Lambda" in show.stdout


def test_pull_rejects_bad_detail() -> None:
    assert runner.invoke(app, ["pull", "--detail", "nope"]).exit_code == 2
