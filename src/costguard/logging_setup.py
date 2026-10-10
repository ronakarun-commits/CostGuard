"""Logging: human-readable by default, one JSON object per line with ``--log-json``.

JSON logs matter when costguard runs unattended (cron, Lambda, containers): log platforms such as
CloudWatch Logs Insights can then filter on fields instead of grepping text. Logs go to **stderr**
so they never corrupt machine-readable output (``--format json``) on stdout.

stdlib ``logging`` + a small formatter is used instead of structlog/loguru: zero extra dependency,
and libraries (boto3, botocore) already log through stdlib.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via logger.info("...", extra={...}) becomes a top-level field.
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "WARNING", fmt: str = "console") -> None:
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party libraries are noisy at INFO/DEBUG; keep them quiet unless asked.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
