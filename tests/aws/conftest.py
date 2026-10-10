from __future__ import annotations

import boto3
import pytest


@pytest.fixture(autouse=True)
def fake_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Make it impossible for any test to reach real AWS or read the developer's ~/.aws."""
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", f"{tmp_path}/none-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", f"{tmp_path}/none-credentials")


@pytest.fixture
def session() -> boto3.Session:
    return boto3.Session(region_name="ap-south-1")
