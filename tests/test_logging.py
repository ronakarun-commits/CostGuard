from __future__ import annotations

import json
import logging

import pytest

from costguard.logging_setup import setup_logging


def test_json_logs(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", "json")
    logging.getLogger("costguard.test").info("hello", extra={"records": 3})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["msg"] == "hello"
    assert data["records"] == 3
    assert data["level"] == "info"


def test_json_logs_include_exceptions(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", "json")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("x").exception("failed")
    assert "boom" in json.loads(capsys.readouterr().err.strip().splitlines()[-1])["exc"]


def test_console_logs_and_quiet_libraries(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("DEBUG", "console")
    logging.getLogger("costguard").debug("dbg")
    logging.getLogger("botocore").info("noise")
    err = capsys.readouterr().err
    assert "dbg" in err
    assert "noise" not in err
