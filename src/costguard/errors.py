"""User-facing errors. The CLI turns these into a message and a non-zero exit code."""

from __future__ import annotations


class CostGuardError(Exception):
    """Base class. ``hint`` tells the user what to do next."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ConfigError(CostGuardError):
    exit_code = 2


class NoDataError(CostGuardError):
    exit_code = 3


class AwsAuthError(CostGuardError):
    """No credentials, expired credentials, or wrong profile."""

    exit_code = 4


class AwsPermissionError(CostGuardError):
    """Credentials work but the IAM policy doesn't allow the call."""

    exit_code = 5


class AwsApiError(CostGuardError):
    exit_code = 6
