from __future__ import annotations

import io
import json
from datetime import date
from typing import Any

import pytest
from typer.testing import CliRunner

from costguard.cli import app
from costguard.core.analytics.anomaly import Incident
from costguard.core.analytics.risk import MonthEndRisk
from costguard.interfaces import notify
from costguard.interfaces.notify import NotifyError, build_alerts, post_slack, slack_payload

runner = CliRunner()


def _inc(n: int, excess: float = 100.0) -> list[Incident]:
    return [Incident("baseline", "1", "acme-prod", f"svc{i}", date(2026, 5, 1),
                     date(2026, 5, 1 + i % 2), 6.0, excess, 3.2) for i in range(n)]  # fmt: skip


def _risk(p: float | None) -> MonthEndRisk:
    return MonthEndRisk(date(2026, 5, 10), 0.95, 8000, 25000, 24900, 27000, 27500, 23000,
                        None if p is None else 24000, p, 21)  # fmt: skip


def test_no_alerts_when_quiet() -> None:
    assert build_alerts([], _risk(0.1)) == []
    assert build_alerts([], None) == []
    assert build_alerts([], _risk(None)) == []


def test_anomaly_alert() -> None:
    (a,) = build_alerts(_inc(1), None)
    assert "1 cost anomaly" in a.title
    assert "3.2x usual" in a.text
    assert a.severity == "warning"
    (big,) = build_alerts(_inc(12, 100), None, currency="INR", fx=88)
    assert "12 cost anomalies" in big.title
    assert "…and 2 more" in big.text
    assert "₹" in big.text
    assert big.severity == "critical"


def test_budget_alert() -> None:
    (a,) = build_alerts([], _risk(0.85))
    assert "85% chance" in a.title
    assert a.severity == "critical"
    assert len(build_alerts(_inc(2), _risk(0.6))) == 2


def test_slack_payload_shape() -> None:
    payload = slack_payload(build_alerts(_inc(1), _risk(0.6)))
    assert payload["text"]
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["section", "divider", "section"]


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def test_post_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout: sent.append(req) or _Resp())  # fmt: skip
    post_slack("https://hooks.slack.com/services/T/B/X", build_alerts(_inc(1), None))
    assert json.loads(sent[0].data)["blocks"]
    with pytest.raises(NotifyError, match=r"hooks\.slack\.com"):
        post_slack("https://evil.example.com/x", [])

    bad = _Resp()
    bad.status = 500
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: bad)
    with pytest.raises(NotifyError, match="500"):
        post_slack("https://hooks.slack.com/services/T/B/X", [])

    import urllib.error

    def offline(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", offline)
    with pytest.raises(NotifyError, match="Could not post"):
        post_slack("https://hooks.slack.com/services/T/B/X", [])


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = str(tmp_path_factory.mktemp("alert") / "cg.duckdb")
    # 2026-10-09 data ends inside seed 42's injected level shift on acme-dev EC2
    result = runner.invoke(app, ["--db", path, "generate", "--end", "2026-10-09"])
    assert result.exit_code == 0
    return path


def test_alert_cli_dry_run(db: str) -> None:
    result = runner.invoke(app, ["--db", db, "alert", "--days", "5", "--dry-run"])
    assert result.exit_code == 11, result.output
    assert "acme-dev" in result.stdout


def test_alert_cli_all_clear(db: str) -> None:
    result = runner.invoke(app, ["--db", db, "alert", "--days", "1", "--budget", "1000000"])
    assert result.exit_code in (0, 11)
    quiet = runner.invoke(app, ["--db", db, "alert", "--days", "1"])
    if quiet.exit_code == 0:
        assert "All clear" in quiet.stdout


def test_alert_cli_posts(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout: sent.append(req) or _Resp())  # fmt: skip
    monkeypatch.setenv("COSTGUARD_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    result = runner.invoke(app, ["--db", db, "alert", "--days", "5", "--budget", "1"])
    assert result.exit_code == 11
    assert len(sent) == 1
    assert "Posted 2 alert(s)" in " ".join(result.stdout.split())


def test_alert_without_webhook_prints(db: str) -> None:
    result = runner.invoke(app, ["--db", db, "alert", "--days", "5"])
    assert result.exit_code == 11
    assert "not set" in result.output


def test_serve_refuses_public_no_auth(db: str) -> None:
    result = runner.invoke(app, ["--db", db, "serve", "--host", "0.0.0.0", "--no-auth"])  # noqa: S104
    assert result.exit_code == 2
    assert "localhost" in result.output


def test_serve_runs_uvicorn(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app_, **kw: calls.append(kw))
    result = runner.invoke(app, ["--db", db, "serve", "--no-auth", "--port", "9999"])
    assert result.exit_code == 0, result.output
    assert calls[0]["port"] == 9999


def test_ui_launches_streamlit(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    calls: list[Any] = []
    monkeypatch.setattr(subprocess, "call", lambda args, env: calls.append((args, env)) or 0)
    result = runner.invoke(app, ["--db", db, "ui"])
    assert result.exit_code == 0
    args, env = calls[0]
    assert args[1] == "run"
    assert args[2].endswith("dashboard_app.py")
    assert env["COSTGUARD_DB_PATH"].endswith("cg.duckdb")


def test_ui_without_streamlit(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = runner.invoke(app, ["--db", db, "ui"])
    assert result.exit_code == 2
    assert "costguard[dashboard]" in result.output
