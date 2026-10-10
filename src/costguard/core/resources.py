"""Inventory models for the waste rules (Phase 4). Pure data: no boto3 types leak in here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Volume:
    volume_id: str
    region: str
    size_gib: int
    volume_type: str  # gp2, gp3, io1, st1, ...
    state: str  # available = not attached to anything
    created_at: datetime
    attached_instance_ids: tuple[str, ...] = ()
    iops: int | None = None
    tags: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ElasticIp:
    allocation_id: str
    public_ip: str
    region: str
    association_id: str | None  # None = idle, still billed
    instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class Instance:
    instance_id: str
    region: str
    instance_type: str
    state: str  # running, stopped, ...
    launched_at: datetime
    avg_cpu_percent: float | None = None  # 14-day average, if CloudWatch data was fetched
    tags: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    region: str
    size_gib: int
    started_at: datetime
    source_volume_id: str | None = None


@dataclass(frozen=True, slots=True)
class Inventory:
    """Everything the waste rules look at, for one account."""

    account_id: str
    collected_at: datetime
    volumes: tuple[Volume, ...] = ()
    elastic_ips: tuple[ElasticIp, ...] = ()
    instances: tuple[Instance, ...] = ()
    snapshots: tuple[Snapshot, ...] = ()
