from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from costguard.core.models import DateRange
from costguard.core.ports import CostProvider
from costguard.errors import AwsAuthError, AwsPermissionError
from costguard.providers.aws.cost_explorer import (
    CostExplorerProvider,
    _region_from_usage_type,
)
from costguard.storage.duckdb_repo import DuckDBRepository

PERIOD = DateRange(date(2026, 1, 1), date(2026, 1, 3))
TODAY = date(2026, 6, 1)


def _metrics(cost: str, qty: str, unit: str = "N/A") -> dict[str, Any]:
    return {
        "UnblendedCost": {"Amount": cost, "Unit": "USD"},
        "UsageQuantity": {"Amount": qty, "Unit": unit},
    }


PAGE_1 = {
    "ResultsByTime": [
        {
            "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
            "Total": {},
            "Groups": [
                {"Keys": ["111111111111", "Amazon Elastic Compute Cloud - Compute"],
                 "Metrics": _metrics("12.5", "48")},
                {"Keys": ["111111111111", "Tax"], "Metrics": _metrics("0", "0")},
            ],
            "Estimated": False,
        }
    ],
    "DimensionValueAttributes": [
        {"Value": "111111111111", "Attributes": {"description": "acme-prod"}}
    ],
    "NextPageToken": "page-2",
}  # fmt: skip
PAGE_2 = {
    "ResultsByTime": [
        {
            "TimePeriod": {"Start": "2026-01-02", "End": "2026-01-03"},
            "Total": {},
            "Groups": [
                {"Keys": ["222222222222", "Amazon Simple Storage Service"],
                 "Metrics": _metrics("3.25", "100")},
            ],
            "Estimated": True,
        }
    ],
    "DimensionValueAttributes": [
        {"Value": "222222222222", "Attributes": {"description": "acme-dev"}}
    ],
}  # fmt: skip


def _expect_pages(stub: Stubber, *pages: dict[str, Any]) -> None:
    for page in pages:
        stub.add_response("get_cost_and_usage", page, {
            "TimePeriod": ANY, "Granularity": "DAILY", "Metrics": ANY, "GroupBy": ANY,
            **({"NextPageToken": ANY} if page is not pages[0] else {}),
        })  # fmt: skip


@pytest.fixture
def repo() -> Iterator[DuckDBRepository]:
    with DuckDBRepository() as r:
        yield r


def _provider(
    session: boto3.Session, repo: DuckDBRepository | None, **kw: Any
) -> CostExplorerProvider:
    return CostExplorerProvider(session, repo, today=TODAY, **kw)


def test_conforms_to_port(session: boto3.Session) -> None:
    assert isinstance(_provider(session, None), CostProvider)


def test_paginates_and_parses(session: boto3.Session, repo: DuckDBRepository) -> None:
    p = _provider(session, repo)
    with Stubber(p._ce) as stub:
        _expect_pages(stub, PAGE_1, PAGE_2)
        records = p.get_costs(PERIOD)
        stub.assert_no_pending_responses()
    assert len(records) == 2  # the all-zero Tax row is dropped
    ec2, s3 = records
    assert ec2.billed_cost == 12.5
    assert ec2.sub_account_name == "acme-prod"
    assert ec2.service_category == "Compute"
    assert s3.sub_account_name == "acme-dev"
    assert s3.charge_date == date(2026, 1, 2)
    assert p.paid_requests == 2
    assert p.estimated_cost_usd == pytest.approx(0.02)


def test_second_run_is_served_from_cache(session: boto3.Session, repo: DuckDBRepository) -> None:
    first = _provider(session, repo)
    with Stubber(first._ce) as stub:
        _expect_pages(stub, PAGE_1, PAGE_2)
        expected = first.get_costs(PERIOD)
    second = _provider(session, repo)
    with Stubber(second._ce):  # no responses queued: any real call would fail
        assert second.get_costs(PERIOD) == expected
    assert second.paid_requests == 0
    assert second.cached_requests == 2


def test_refresh_bypasses_cache(session: boto3.Session, repo: DuckDBRepository) -> None:
    for refresh in (False, True):
        p = _provider(session, repo, refresh=refresh)
        with Stubber(p._ce) as stub:
            _expect_pages(stub, PAGE_1, PAGE_2)
            p.get_costs(PERIOD)
        assert p.paid_requests == 2


def test_recent_periods_have_short_ttl(session: boto3.Session) -> None:
    p = CostExplorerProvider(session, None, today=date(2026, 1, 3))
    assert p._ttl(PERIOD) == 6 * 3600
    assert _provider(session, None)._ttl(PERIOD) is None


def test_usage_type_detail(session: boto3.Session) -> None:
    page = {
        "ResultsByTime": [{
            "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
            "Groups": [{"Keys": ["Amazon Elastic Compute Cloud - Compute",
                                  "APS3-BoxUsage:m6i.large"],
                        "Metrics": _metrics("2.304", "24", "Hrs")}],
        }]
    }  # fmt: skip
    p = _provider(session, None, detail="usage_type")
    with Stubber(p._ce) as stub:
        stub.add_response("get_cost_and_usage", page)
        (rec,) = p.get_costs(PERIOD)
    assert rec.region_id == "ap-south-1"
    assert rec.consumed_unit == "Hrs"
    assert rec.unit_price == pytest.approx(0.096)


@pytest.mark.parametrize(
    ("code", "error"),
    [("AccessDeniedException", AwsPermissionError), ("ExpiredTokenException", AwsAuthError)],
)
def test_errors_are_translated(session: boto3.Session, code: str, error: type[Exception]) -> None:
    p = _provider(session, None)
    with Stubber(p._ce) as stub:
        stub.add_client_error("get_cost_and_usage", service_error_code=code, service_message="x")
        with pytest.raises(error) as info:
            p.get_costs(PERIOD)
    assert info.value.hint  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("usage_type", "region"),
    [
        ("APS3-BoxUsage:m6i.large", "ap-south-1"),
        ("USW2-EBS:VolumeUsage.gp3", "us-west-2"),
        ("BoxUsage:t3.micro", "us-east-1"),
        ("DataTransfer-Out-Bytes", "us-east-1"),
        ("IN-DataTransfer-Out-Bytes", "global"),
    ],
)
def test_region_from_usage_type(usage_type: str, region: str) -> None:
    assert _region_from_usage_type(usage_type) == region
