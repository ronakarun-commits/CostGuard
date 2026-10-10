"""Static, approximate AWS on-demand list prices (Linux, USD) used to *estimate* savings.

Why static: the AWS Pricing API returns huge nested JSON per SKU and needs network access; for
ranking waste, ±10% accuracy is enough. Prices are us-east-1 list prices (late 2025) with a
per-region multiplier. Replace with a Pricing-API-backed table if exact figures matter.
"""

from __future__ import annotations

from typing import Final

HOURS_PER_MONTH: Final = 730
PRICES_AS_OF: Final = "2025-11"

# USD per GB-month
EBS_GB_MONTH: Final[dict[str, float]] = {
    "gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015,
    "standard": 0.05,
}  # fmt: skip
SNAPSHOT_GB_MONTH: Final = 0.05
PUBLIC_IPV4_HOUR: Final = 0.005  # charged for every public IPv4 address, idle or not

# USD per hour, us-east-1, Linux on-demand
INSTANCE_HOUR: Final[dict[str, float]] = {
    "t2.micro": 0.0116, "t2.small": 0.023, "t2.medium": 0.0464, "t2.large": 0.0928,
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832,
    "m4.large": 0.10, "m4.xlarge": 0.20, "m4.2xlarge": 0.40,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384, "m6i.4xlarge": 0.768,
    "m7i.large": 0.1008, "m7i.xlarge": 0.2016,
    "c4.large": 0.10, "c4.xlarge": 0.199, "c5.large": 0.085, "c5.xlarge": 0.17,
    "c6i.large": 0.085, "c6i.xlarge": 0.17,
    "r4.large": 0.133, "r4.xlarge": 0.266, "r5.large": 0.126, "r5.xlarge": 0.252,
    "r6i.large": 0.126, "r6i.xlarge": 0.252,
}  # fmt: skip

REGION_MULTIPLIER: Final[dict[str, float]] = {
    "us-east-1": 1.0, "us-east-2": 1.0, "us-west-2": 1.0, "us-west-1": 1.17,
    "ap-south-1": 1.05, "ap-southeast-1": 1.2, "ap-northeast-1": 1.25,
    "eu-west-1": 1.1, "eu-central-1": 1.15,
}  # fmt: skip

# Previous generation -> current-generation equivalent (same size)
NEXT_GEN: Final[dict[str, str]] = {"t2": "t3", "m4": "m6i", "m5": "m6i", "c4": "c6i", "r4": "r6i"}

SIZES: Final = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge"]


def _mult(region: str) -> float:
    return REGION_MULTIPLIER.get(region, 1.1)


def ebs_monthly(volume_type: str, size_gib: int, region: str) -> float:
    return EBS_GB_MONTH.get(volume_type, EBS_GB_MONTH["gp3"]) * size_gib * _mult(region)


def snapshot_monthly(size_gib: int, region: str) -> float:
    # Snapshots are incremental, so the full volume size over-states cost; 50% is a fair estimate.
    return SNAPSHOT_GB_MONTH * size_gib * 0.5 * _mult(region)


def public_ip_monthly(region: str) -> float:
    return PUBLIC_IPV4_HOUR * HOURS_PER_MONTH * _mult(region)


def instance_monthly(instance_type: str, region: str) -> float | None:
    hourly = INSTANCE_HOUR.get(instance_type)
    return None if hourly is None else hourly * HOURS_PER_MONTH * _mult(region)


def one_size_down(instance_type: str) -> str | None:
    family, _, size = instance_type.partition(".")
    if size not in SIZES or SIZES.index(size) == 0:
        return None
    smaller = f"{family}.{SIZES[SIZES.index(size) - 1]}"
    return smaller if smaller in INSTANCE_HOUR else None
