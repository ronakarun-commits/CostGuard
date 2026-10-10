from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from costguard.config import load_settings
from costguard.errors import ConfigError
from costguard.interfaces.api import create_app
from costguard.providers.synthetic import SyntheticProvider
from costguard.storage.duckdb_repo import DuckDBRepository

KEY = "test-key-0123456789"
H = {"X-API-Key": KEY}


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from datetime import date

    path = tmp_path_factory.mktemp("api") / "cg.duckdb"
    p = SyntheticProvider(date(2026, 1, 1), 160, seed=3)
    with DuckDBRepository(path) as r:
        r.upsert_costs(p.get_costs(p.world), "synthetic")
    return path


@pytest.fixture
def client(db: Path) -> Iterator[TestClient]:
    settings = load_settings(db_path=db)
    settings.api_keys = [SecretStr("old-key"), SecretStr(KEY)]
    with TestClient(create_app(settings)) as c:
        yield c


def test_health_needs_no_key(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_auth_required(client: TestClient, headers: dict[str, str]) -> None:
    r = client.get("/v1/summary", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "APIKey"


def test_any_configured_key_works(client: TestClient) -> None:
    assert client.get("/v1/summary", headers={"X-API-Key": "old-key"}).status_code == 200


def test_summary(client: TestClient) -> None:
    data = client.get("/v1/summary?by=account&days=7", headers=H).json()
    assert data["by"] == "account"
    assert len(data["rows"]) == 4
    inr = client.get("/v1/summary?by=account&days=7&currency=INR", headers=H).json()
    assert inr["rows"][0]["cost"] == pytest.approx(data["rows"][0]["cost"] * 88.0)
    assert client.get("/v1/summary?by=nope", headers=H).status_code == 422


@pytest.mark.parametrize(
    "path",
    ["/v1/dashboard", "/v1/trend?freq=week", "/v1/anomalies?days=60", "/v1/forecast?days=7",
     "/v1/risk?budget=20000", "/v1/variance", "/v1/audit?min_saving=5"],
)  # fmt: skip
def test_reports_json(client: TestClient, path: str) -> None:
    r = client.get(path, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"]
    assert "sections" in body


def test_formats(client: TestClient) -> None:
    html = client.get("/v1/dashboard?format=html", headers=H)
    assert html.headers["content-type"].startswith("text/html")
    assert html.text.startswith("<!doctype html>")
    csv = client.get("/v1/audit?format=csv", headers=H)
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.startswith("section,")


def test_errors_are_json_not_tracebacks(client: TestClient) -> None:
    r = client.get("/v1/variance?current=2030-01", headers=H)
    assert r.status_code == 404
    assert r.json()["error"].startswith("No cost data")
    bad = client.get("/v1/variance?current=Jan", headers=H)
    assert bad.status_code == 400
    assert "YYYY-MM" in bad.json()["error"]


def test_missing_database(tmp_path: Path) -> None:
    settings = load_settings(db_path=tmp_path / "none.duckdb")
    with TestClient(create_app(settings, require_auth=False)) as c:
        r = c.get("/v1/dashboard")
    assert r.status_code == 404
    assert "generate" in r.json()["hint"]


def test_refuses_to_start_without_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No API keys"):
        create_app(load_settings(db_path=tmp_path / "x.duckdb"))


def test_api_is_read_only(client: TestClient, db: Path) -> None:
    with DuckDBRepository(db) as r:
        before = len(r.get_costs())
    client.get("/v1/risk", headers=H)
    with DuckDBRepository(db) as r:
        assert len(r.get_costs()) == before
