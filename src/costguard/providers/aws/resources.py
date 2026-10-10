"""EC2 inventory adapter for the waste rules. Read-only ``ec2:Describe*`` (+ optional CloudWatch).

All four Describe APIs are free. CloudWatch ``GetMetricStatistics`` is billed per 1,000 metrics
requested beyond the free tier, so CPU metrics are opt-in (``with_cpu=True``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import boto3

from costguard.core.resources import ElasticIp, Instance, Inventory, Snapshot, Volume
from costguard.providers.aws.session import aws_errors, client

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch.client import CloudWatchClient
    from mypy_boto3_ec2.client import EC2Client
    from mypy_boto3_ec2.type_defs import TagTypeDef


def _tags(tags: Iterable[TagTypeDef] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((t.get("Key", ""), t.get("Value", "")) for t in tags or []))


class Ec2InventoryProvider:
    def __init__(
        self, session: boto3.Session, regions: list[str], *, with_cpu: bool = False
    ) -> None:
        self.session = session
        self.regions = regions
        self.with_cpu = with_cpu

    def collect(self, account_id: str) -> Inventory:
        volumes: list[Volume] = []
        eips: list[ElasticIp] = []
        instances: list[Instance] = []
        snapshots: list[Snapshot] = []
        for region in self.regions:
            ec2: EC2Client = client(self.session, "ec2", region=region)
            with aws_errors(f"ec2:Describe* in {region}", permission="ec2:Describe*"):
                volumes += self._volumes(ec2, region)
                eips += self._addresses(ec2, region)
                instances += self._instances(ec2, region)
                snapshots += self._snapshots(ec2, region)
        return Inventory(
            account_id=account_id,
            collected_at=datetime.now(UTC),
            volumes=tuple(volumes),
            elastic_ips=tuple(eips),
            instances=tuple(instances),
            snapshots=tuple(snapshots),
        )

    @staticmethod
    def _volumes(ec2: EC2Client, region: str) -> list[Volume]:
        out = []
        for page in ec2.get_paginator("describe_volumes").paginate():
            for v in page["Volumes"]:
                out.append(
                    Volume(
                        volume_id=v["VolumeId"],
                        region=region,
                        size_gib=v["Size"],
                        volume_type=v["VolumeType"],
                        state=v["State"],
                        created_at=v["CreateTime"],
                        attached_instance_ids=tuple(
                            a["InstanceId"] for a in v.get("Attachments", []) if "InstanceId" in a
                        ),
                        iops=v.get("Iops"),
                        tags=_tags(v.get("Tags")),
                    )
                )
        return out

    @staticmethod
    def _addresses(ec2: EC2Client, region: str) -> list[ElasticIp]:
        return [
            ElasticIp(
                allocation_id=a.get("AllocationId", a.get("PublicIp", "")),
                public_ip=a.get("PublicIp", ""),
                region=region,
                association_id=a.get("AssociationId"),
                instance_id=a.get("InstanceId"),
            )
            for a in ec2.describe_addresses()["Addresses"]
        ]

    def _instances(self, ec2: EC2Client, region: str) -> list[Instance]:
        out = []
        for page in ec2.get_paginator("describe_instances").paginate():
            for res in page["Reservations"]:
                for i in res["Instances"]:
                    state = i["State"]["Name"]
                    cpu = (
                        self._avg_cpu(region, i["InstanceId"])
                        if self.with_cpu and state == "running"
                        else None
                    )
                    out.append(
                        Instance(
                            instance_id=i["InstanceId"],
                            region=region,
                            instance_type=i["InstanceType"],
                            state=state,
                            launched_at=i["LaunchTime"],
                            avg_cpu_percent=cpu,
                            tags=_tags(i.get("Tags")),
                        )
                    )
        return out

    @staticmethod
    def _snapshots(ec2: EC2Client, region: str) -> list[Snapshot]:
        out = []
        for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
            for s in page["Snapshots"]:
                out.append(
                    Snapshot(
                        snapshot_id=s["SnapshotId"],
                        region=region,
                        size_gib=s["VolumeSize"],
                        started_at=s["StartTime"],
                        source_volume_id=s.get("VolumeId"),
                    )
                )
        return out

    def _avg_cpu(self, region: str, instance_id: str, days: int = 14) -> float | None:
        cw: CloudWatchClient = client(self.session, "cloudwatch", region=region)
        end = datetime.now(UTC)
        with aws_errors(
            "cloudwatch:GetMetricStatistics", permission="cloudwatch:GetMetricStatistics"
        ):
            resp = cw.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=end - timedelta(days=days),
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
        points = [p["Average"] for p in resp.get("Datapoints", []) if "Average" in p]
        return sum(points) / len(points) if points else None
