"""Session creation and error translation.

Credentials are resolved by boto3's standard **credential provider chain** (in order): explicit
args -> environment variables -> shared config/credentials files (``--profile``, incl. SSO and
assume-role profiles) -> container / EC2 instance metadata. costguard never reads or stores keys
itself; pass ``--profile`` or set ``AWS_PROFILE``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    NoRegionError,
    ProfileNotFound,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

from costguard.errors import AwsApiError, AwsAuthError, AwsPermissionError

if TYPE_CHECKING:
    from mypy_boto3_sts.client import STSClient

# Adaptive mode: exponential backoff with jitter + client-side rate limiting on throttles.
# Cost Explorer allows only a few requests per second, so throttling is realistic.
RETRY_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 10},
    connect_timeout=10,
    read_timeout=60,
    user_agent_extra="costguard",
)

AUTH_CODES = {
    "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "UnrecognizedClientException",
    "SignatureDoesNotMatch", "AuthFailure",
}  # fmt: skip
PERMISSION_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}

LOGIN_HINT = (
    "Configure a read-only profile (`aws configure sso` or `aws configure --profile costguard`) "
    "and pass --profile, or set AWS_PROFILE. Never use root access keys."
)


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    account_id: str
    arn: str
    user_id: str

    @property
    def is_root(self) -> bool:
        return self.arn.endswith(":root")


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as exc:
        raise AwsAuthError(f"AWS profile {profile!r} not found.", LOGIN_HINT) from exc


@contextmanager
def aws_errors(operation: str, permission: str | None = None) -> Iterator[None]:
    """Translate botocore exceptions into costguard errors with actionable hints."""
    try:
        yield
    except (NoCredentialsError, SSOTokenLoadError, UnauthorizedSSOTokenError,
            TokenRetrievalError) as exc:  # fmt: skip
        raise AwsAuthError(f"No valid AWS credentials for {operation}.", LOGIN_HINT) from exc
    except NoRegionError as exc:
        raise AwsAuthError("No AWS region configured.", "Pass --region or set AWS_REGION.") from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        if code in AUTH_CODES:
            raise AwsAuthError(f"{operation}: {msg}", LOGIN_HINT) from exc
        if code in PERMISSION_CODES:
            need = f"Grant `{permission}`" if permission else "Check the IAM policy"
            raise AwsPermissionError(
                f"{operation} was denied: {msg}",
                f"{need}. See docs/iam-policy.json for the full read-only policy.",
            ) from exc
        raise AwsApiError(f"{operation} failed ({code}): {msg}") from exc
    except BotoCoreError as exc:
        raise AwsApiError(f"{operation} failed: {exc}") from exc


def client(session: boto3.Session, service: str, region: str | None = None) -> Any:
    """Create a client with costguard's retry config. (Typed at call sites via TYPE_CHECKING.)"""
    return session.client(service, region_name=region, config=RETRY_CONFIG)  # type: ignore[call-overload]


def whoami(session: boto3.Session) -> CallerIdentity:
    sts: STSClient = client(session, "sts", region=session.region_name or "us-east-1")
    with aws_errors("sts:GetCallerIdentity"):
        resp = sts.get_caller_identity()
    return CallerIdentity(resp["Account"], resp["Arn"], resp["UserId"])


def paginate(
    fetch: Callable[[str | None], dict[str, Any]], token_key: str
) -> Iterator[dict[str, Any]]:
    """Generic NextPageToken loop for APIs boto3 has no paginator for (e.g. GetCostAndUsage)."""
    token: str | None = None
    while True:
        page = fetch(token)
        yield page
        token = page.get(token_key)
        if not token:
            return
