"""Cost Explorer adapter: implements the ``CostProvider`` port with real AWS data.

Cost and safety notes
* ``GetCostAndUsage`` costs **$0.01 per request** (each page is a request). Every response is cached
  in DuckDB; ``refresh=True`` bypasses the cache. Recent days are cached for 6 h only, because
  Cost Explorer keeps restating them; older periods are cached permanently.
* Read-only: needs only ``ce:GetCostAndUsage``.
* Cost Explorer allows at most two ``GroupBy`` dimensions per request, hence two detail levels:

  ``service``     LINKED_ACCOUNT x SERVICE  (multi-account; usage quantity mixes units)
  ``usage_type``  SERVICE x USAGE_TYPE      (one account; usage quantity has a real unit, which
                                             price-volume variance analysis needs)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

import boto3

from costguard.core.models import CostRecord, DateRange
from costguard.providers.aws.session import aws_errors, client, paginate

if TYPE_CHECKING:
    from mypy_boto3_ce.client import CostExplorerClient

log = logging.getLogger(__name__)

Detail = Literal["service", "usage_type"]
Metric = Literal["UnblendedCost", "AmortizedCost", "NetUnblendedCost"]

PRICE_PER_REQUEST_USD = 0.01
RECENT_TTL_SECONDS = 6 * 3600
RESTATEMENT_DAYS = 3

SERVICE_CATEGORY = {
    "Amazon Elastic Compute Cloud - Compute": "Compute",
    "EC2 - Other": "Storage",
    "AWS Lambda": "Compute",
    "Amazon Elastic Container Service": "Compute",
    "Amazon Elastic Kubernetes Service": "Compute",
    "AWS Fargate": "Compute",
    "Amazon Simple Storage Service": "Storage",
    "Amazon Elastic File System": "Storage",
    "Amazon Relational Database Service": "Databases",
    "Amazon DynamoDB": "Databases",
    "Amazon ElastiCache": "Databases",
    "Amazon CloudFront": "Networking",
    "Amazon Virtual Private Cloud": "Networking",
    "AWS Data Transfer": "Networking",
    "Amazon Route 53": "Networking",
    "Elastic Load Balancing": "Networking",
    "AmazonCloudWatch": "Management and Governance",
    "AWS CloudTrail": "Management and Governance",
    "AWS Config": "Management and Governance",
    "Amazon Athena": "Analytics",
    "Amazon Redshift": "Analytics",
    "AWS Glue": "Analytics",
    "Amazon Kinesis": "Analytics",
    "Tax": "Other",
}


class ApiCache(Protocol):
    def cache_get(self, key: str, max_age_seconds: float | None = None) -> str | None: ...

    def cache_put(self, key: str, operation: str, response: str) -> None: ...


class CostExplorerProvider:
    """Implements ``CostProvider``. Cost Explorer's endpoint lives in us-east-1."""

    name = "aws"

    def __init__(
        self,
        session: boto3.Session,
        cache: ApiCache | None = None,
        *,
        detail: Detail = "service",
        metric: Metric = "UnblendedCost",
        refresh: bool = False,
        today: date | None = None,
    ) -> None:
        self._ce: CostExplorerClient = client(session, "ce", region="us-east-1")
        self.cache = cache
        self.detail = detail
        self.metric = metric
        self.refresh = refresh
        self.today = today or date.today()
        self.paid_requests = 0
        self.cached_requests = 0

    @property
    def estimated_cost_usd(self) -> float:
        return self.paid_requests * PRICE_PER_REQUEST_USD

    # ---- port ----------------------------------------------------------------------------------

    def get_costs(self, period: DateRange) -> list[CostRecord]:
        group_by = (
            [
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ]
            if self.detail == "service"
            else [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ]
        )
        params: dict[str, Any] = {
            "TimePeriod": {"Start": period.start.isoformat(), "End": period.end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": [self.metric, "UsageQuantity"],
            "GroupBy": group_by,
        }
        records: list[CostRecord] = []
        account_names: dict[str, str] = {}
        for page in paginate(lambda token: self._call(params, token, period), "NextPageToken"):
            for attr in page.get("DimensionValueAttributes", []):
                desc = attr.get("Attributes", {}).get("description")
                if desc:
                    account_names[attr["Value"]] = desc
            records.extend(self._parse(page))
        if account_names:
            records = [
                _with_account_name(r, account_names.get(r.sub_account_id, r.sub_account_name))
                for r in records
            ]
        log.info(
            "cost explorer: %d records, %d paid requests (~$%.2f), %d cached",
            len(records), self.paid_requests, self.estimated_cost_usd, self.cached_requests,
        )  # fmt: skip
        return records

    # ---- internals -----------------------------------------------------------------------------

    def _ttl(self, period: DateRange) -> float | None:
        recent = period.end > self.today - timedelta(days=RESTATEMENT_DAYS)
        return RECENT_TTL_SECONDS if recent else None

    def _call(self, params: dict[str, Any], token: str | None, period: DateRange) -> dict[str, Any]:
        request = {**params, **({"NextPageToken": token} if token else {})}
        key = (
            "GetCostAndUsage:"
            + hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        )
        if self.cache is not None and not self.refresh:
            hit = self.cache.cache_get(key, self._ttl(period))
            if hit is not None:
                self.cached_requests += 1
                return dict(json.loads(hit))
        with aws_errors("ce:GetCostAndUsage", permission="ce:GetCostAndUsage"):
            resp = self._ce.get_cost_and_usage(**request)
        self.paid_requests += 1
        data = {k: v for k, v in resp.items() if k != "ResponseMetadata"}
        if self.cache is not None:
            self.cache.cache_put(key, "GetCostAndUsage", json.dumps(data, default=str))
        return data

    def _parse(self, page: dict[str, Any]) -> list[CostRecord]:
        out: list[CostRecord] = []
        for result in page.get("ResultsByTime", []):
            day = date.fromisoformat(result["TimePeriod"]["Start"])
            for group in result.get("Groups", []):
                keys: list[str] = group["Keys"]
                cost = group["Metrics"][self.metric]
                usage = group["Metrics"]["UsageQuantity"]
                if self.detail == "service":
                    account, service, sku, unit = keys[0], keys[1], "ALL", "Mixed"
                else:
                    account, service, sku = "self", keys[0], keys[1]
                    unit = usage.get("Unit") or "N/A"
                amount = float(cost["Amount"])
                if amount == 0 and float(usage["Amount"]) == 0:
                    continue
                out.append(
                    CostRecord(
                        charge_date=day,
                        sub_account_id=account,
                        sub_account_name=account,
                        service_name=service,
                        service_category=SERVICE_CATEGORY.get(service, "Other"),
                        region_id=_region_from_usage_type(sku) if sku != "ALL" else "global",
                        sku_id=sku,
                        consumed_quantity=float(usage["Amount"]),
                        consumed_unit=unit,
                        billed_cost=amount,
                        billing_currency=cost.get("Unit", "USD"),
                    )
                )
        return out


def _with_account_name(r: CostRecord, name: str) -> CostRecord:
    from dataclasses import replace

    return replace(r, sub_account_name=name)


# Usage types are prefixed with a region code, e.g. "APS3-BoxUsage:m6i.large" (ap-south-1).
_REGION_PREFIX = {
    "USE1": "us-east-1", "USE2": "us-east-2", "USW1": "us-west-1", "USW2": "us-west-2",
    "APS1": "ap-southeast-1", "APS2": "ap-southeast-2", "APS3": "ap-south-1",
    "APS5": "ap-south-2", "APN1": "ap-northeast-1", "APN2": "ap-northeast-2",
    "EUC1": "eu-central-1", "EUW1": "eu-west-1", "EUW2": "eu-west-2",
    "EUW3": "eu-west-3", "EUN1": "eu-north-1", "SAE1": "sa-east-1", "CAN1": "ca-central-1",
}  # fmt: skip


def _region_from_usage_type(usage_type: str) -> str:
    """'APS3-BoxUsage:m6i.large' -> 'ap-south-1'. Un-prefixed usage types are us-east-1 by AWS
    convention; unknown upper-case prefixes (CloudFront edges such as 'IN', 'EU') are 'global'."""
    prefix, sep, _ = usage_type.partition("-")
    if sep and prefix in _REGION_PREFIX:
        return _REGION_PREFIX[prefix]
    if sep and prefix.isupper() and prefix.isalnum() and len(prefix) <= 4:
        return "global"
    return "us-east-1"
