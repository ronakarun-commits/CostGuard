"""EC2 / Budgets / STS against moto's in-memory AWS. No network, no cost."""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import NoCredentialsError
from moto import mock_aws

from costguard.errors import AwsAuthError
from costguard.providers.aws.budgets import get_budgets
from costguard.providers.aws.resources import Ec2InventoryProvider
from costguard.providers.aws.session import aws_errors, make_session, whoami


@mock_aws
def test_whoami(session: boto3.Session) -> None:
    ident = whoami(session)
    assert ident.account_id == "123456789012"
    assert not ident.is_root


@mock_aws
def test_inventory(session: boto3.Session) -> None:
    ec2 = session.client("ec2", region_name="ap-south-1")
    idle = ec2.create_volume(AvailabilityZone="ap-south-1a", Size=100, VolumeType="gp2")
    ec2.allocate_address(Domain="vpc")
    image = ec2.describe_images()["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="m5.large")
    ec2.create_snapshot(VolumeId=idle["VolumeId"])

    inv = Ec2InventoryProvider(session, ["ap-south-1"]).collect("123456789012")

    by_id = {v.volume_id: v for v in inv.volumes}
    assert by_id[idle["VolumeId"]].state == "available"
    assert by_id[idle["VolumeId"]].size_gib == 100
    assert len(inv.elastic_ips) == 1
    assert inv.elastic_ips[0].association_id is None
    assert [i.instance_type for i in inv.instances] == ["m5.large"]
    assert any(s.source_volume_id == idle["VolumeId"] for s in inv.snapshots)


@mock_aws
def test_inventory_with_cpu(session: boto3.Session) -> None:
    ec2 = session.client("ec2", region_name="ap-south-1")
    image = ec2.describe_images()["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro")
    inv = Ec2InventoryProvider(session, ["ap-south-1"], with_cpu=True).collect("123456789012")
    assert inv.instances[0].avg_cpu_percent is None  # no datapoints in moto


@mock_aws
def test_budgets(session: boto3.Session) -> None:
    session.client("budgets", region_name="us-east-1").create_budget(
        AccountId="123456789012",
        Budget={
            "BudgetName": "monthly",
            "BudgetLimit": {"Amount": "500", "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
        },
    )
    (b,) = get_budgets(session, "123456789012")
    assert (b.name, b.amount, b.currency, b.period) == ("monthly", 500.0, "USD", "MONTHLY")


def test_missing_profile() -> None:
    with pytest.raises(AwsAuthError, match="not found"):
        make_session("does-not-exist")


def test_no_credentials_translated() -> None:
    with pytest.raises(AwsAuthError) as info, aws_errors("sts:GetCallerIdentity"):
        raise NoCredentialsError
    assert "root" in (info.value.hint or "")
