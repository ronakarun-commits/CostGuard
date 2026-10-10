"""Synthetic multi-account AWS cost data with labelled, injected anomalies.

Why synthetic data first:
* zero AWS cost and no credentials needed;
* deterministic (seeded), so tests are reproducible;
* the anomalies are *known*, so detectors can be scored (precision / recall) — real bills
  never come with labels.

Model of one (account, service) series::

    usage(t) = base * env_scale * growth(t) * weekly(t) * noise(t) * anomaly_q(t)
    price(t) = list_price * anomaly_p(t)
    cost(t)  = usage(t) * price(t)

Usage and price are generated separately so the Phase 6 price-volume variance analysis has
something real to decompose.

The whole "world" (``start`` .. ``start + days``) is generated once; ``get_costs`` filters it.
That way the same seed always gives the same numbers whatever period is requested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import cached_property

import numpy as np

from costguard.core.models import (
    Account,
    AnomalyKind,
    CostRecord,
    DateRange,
    InjectedAnomaly,
)


@dataclass(frozen=True, slots=True)
class ServiceProfile:
    """Baseline behaviour of one AWS service in a fully-scaled production account."""

    service_name: str
    category: str
    sku_id: str
    unit: str
    list_price: float  # USD per unit
    base_quantity: float  # units per day at env_scale 1.0
    weekend_dip: float  # 0.2 -> weekends are 20% lower
    annual_growth: float  # 0.3 -> +30% per year
    noise: float  # sigma of log-normal day-to-day noise
    region_id: str = "ap-south-1"


SERVICES: tuple[ServiceProfile, ...] = (
    ServiceProfile(
        "Amazon Elastic Compute Cloud - Compute",
        "Compute",
        "APS3-BoxUsage:m6i.xlarge",
        "Hrs",
        0.192,
        720,
        0.15,
        0.20,
        0.06,
    ),
    ServiceProfile(
        "EC2 - Other",
        "Storage",
        "APS3-EBS:VolumeUsage.gp3",
        "GB-Mo",
        0.0912,
        20000 / 30,
        0.0,
        0.25,
        0.02,
    ),
    ServiceProfile(
        "Amazon Relational Database Service",
        "Databases",
        "APS3-InstanceUsage:db.r6g.large",
        "Hrs",
        0.227,
        72,
        0.0,
        0.10,
        0.01,
    ),
    ServiceProfile(
        "Amazon Simple Storage Service",
        "Storage",
        "APS3-TimedStorage-ByteHrs",
        "GB-Mo",
        0.025,
        50000 / 30,
        0.0,
        0.40,
        0.02,
    ),
    ServiceProfile(
        "AWS Lambda",
        "Compute",
        "APS3-Lambda-GB-Second",
        "Second",
        0.0000166667,
        3_000_000,
        0.30,
        0.35,
        0.12,
    ),
    ServiceProfile(
        "Amazon CloudFront",
        "Networking",
        "IN-DataTransfer-Out-Bytes",
        "GB",
        0.109,
        400,
        0.25,
        0.30,
        0.10,
        region_id="global",
    ),
    ServiceProfile(
        "Amazon DynamoDB",
        "Databases",
        "APS3-WriteRequestUnits",
        "WriteRequestUnits",
        0.00000142,
        18_000_000,
        0.20,
        0.25,
        0.08,
    ),
    ServiceProfile(
        "AmazonCloudWatch",
        "Management and Governance",
        "APS3-DataProcessing-Bytes",
        "GB",
        0.67,
        30,
        0.10,
        0.20,
        0.07,
    ),
    ServiceProfile(
        "Amazon Athena",
        "Analytics",
        "APS3-DataScannedInTB",
        "Terabytes",
        5.0,
        2.0,
        0.50,
        0.50,
        0.25,
    ),
)

ACCOUNTS: tuple[Account, ...] = (
    Account("111111111111", "acme-prod", "prod"),
    Account("222222222222", "acme-staging", "staging"),
    Account("333333333333", "acme-dev", "dev"),
    Account("444444444444", "acme-data", "data"),
)

# How big each service is in each account, relative to production. Missing -> not used.
ENV_SCALE: dict[str, dict[str, float]] = {
    "prod": {s.service_name: 1.0 for s in SERVICES if s.service_name != "Amazon Athena"},
    "staging": {
        "Amazon Elastic Compute Cloud - Compute": 0.25,
        "EC2 - Other": 0.20,
        "Amazon Relational Database Service": 0.5,
        "Amazon Simple Storage Service": 0.05,
        "AWS Lambda": 0.10,
        "Amazon DynamoDB": 0.05,
        "AmazonCloudWatch": 0.3,
    },
    "dev": {
        "Amazon Elastic Compute Cloud - Compute": 0.15,
        "EC2 - Other": 0.10,
        "Amazon Relational Database Service": 0.5,
        "Amazon Simple Storage Service": 0.02,
        "AWS Lambda": 0.05,
        "AmazonCloudWatch": 0.2,
    },
    "data": {
        "Amazon Elastic Compute Cloud - Compute": 0.40,
        "EC2 - Other": 0.30,
        "Amazon Simple Storage Service": 1.5,
        "Amazon Athena": 1.0,
        "AmazonCloudWatch": 0.2,
    },
}

TEAM_BY_ENV = {"prod": "platform", "staging": "platform", "dev": "engineering", "data": "analytics"}

WARMUP_DAYS = 60  # no anomalies before this, so detectors have a clean baseline


class SyntheticProvider:
    """Deterministic fake AWS organisation. Implements ``LabelledCostProvider``."""

    name = "synthetic"

    def __init__(
        self,
        start: date,
        days: int = 365,
        seed: int = 42,
        anomaly_rate: float = 1 / 30,
    ) -> None:
        if days < 1:
            msg = "days must be >= 1"
            raise ValueError(msg)
        self.world = DateRange(start, start + timedelta(days=days))
        self.seed = seed
        self.anomaly_rate = anomaly_rate  # expected anomalies per day, across the whole org

    # ---- public API (the port) --------------------------------------------------------------

    def get_costs(self, period: DateRange) -> list[CostRecord]:
        return [r for r in self._records if period.start <= r.charge_date < period.end]

    def get_anomalies(self, period: DateRange) -> list[InjectedAnomaly]:
        return [
            a for a in self._anomalies if a.start_date < period.end and a.end_date >= period.start
        ]

    # ---- generation ---------------------------------------------------------------------------

    @cached_property
    def _series(self) -> list[tuple[Account, ServiceProfile, float]]:
        return [
            (acct, svc, ENV_SCALE[acct.environment][svc.service_name])
            for acct in ACCOUNTS
            for svc in SERVICES
            if svc.service_name in ENV_SCALE[acct.environment]
        ]

    @cached_property
    def _anomalies(self) -> list[InjectedAnomaly]:
        rng = np.random.default_rng([self.seed, 1])
        days = self.world.days
        usable = days - WARMUP_DAYS - 3
        if usable <= 0:
            return []
        n = int(rng.poisson(self.anomaly_rate * usable))
        kinds = list(AnomalyKind)
        weights = np.array([0.45, 0.25, 0.15, 0.15])  # spikes are most common in practice
        busy: dict[tuple[str, str], list[tuple[int, int]]] = {}
        out: list[InjectedAnomaly] = []

        for _ in range(n * 4):  # retries for overlaps
            if len(out) >= n:
                break
            acct, svc, _scale = self._series[int(rng.integers(len(self._series)))]
            kind = kinds[int(rng.choice(len(kinds), p=weights))]
            start = WARMUP_DAYS + int(rng.integers(usable))
            match kind:
                case AnomalyKind.SPIKE:
                    length = int(rng.integers(1, 4))
                    magnitude = float(rng.uniform(3.0, 8.0))
                    desc = f"{magnitude:.1f}x usage spike for {length} day(s)"
                case AnomalyKind.LEVEL_SHIFT:
                    length = int(rng.integers(20, 75))
                    magnitude = float(rng.uniform(1.6, 3.0))
                    desc = f"usage stepped up {magnitude:.1f}x (forgotten resources)"
                case AnomalyKind.DRIFT:
                    length = int(rng.integers(14, 35))
                    magnitude = float(rng.uniform(0.03, 0.07))
                    desc = f"usage creeping up {magnitude:.0%} per day"
                case AnomalyKind.PRICE_CHANGE:
                    length = int(rng.integers(30, 90))
                    magnitude = float(rng.uniform(1.25, 1.6))
                    desc = f"unit price up {magnitude - 1:.0%}, usage unchanged"
            end = min(start + length - 1, days - 1)
            key = (acct.id, svc.service_name)
            if any(s <= end + 7 and start <= e + 7 for s, e in busy.get(key, [])):
                continue
            busy.setdefault(key, []).append((start, end))
            out.append(
                InjectedAnomaly(
                    id="",
                    kind=kind,
                    sub_account_id=acct.id,
                    service_name=svc.service_name,
                    start_date=self.world.start + timedelta(days=start),
                    end_date=self.world.start + timedelta(days=end),
                    magnitude=round(magnitude, 4),
                    description=f"{acct.name}: {svc.service_name} — {desc}",
                )
            )

        out.sort(key=lambda a: (a.start_date, a.sub_account_id, a.service_name))
        return [
            InjectedAnomaly(
                f"A{i:03d}",
                a.kind,
                a.sub_account_id,
                a.service_name,
                a.start_date,
                a.end_date,
                a.magnitude,
                a.description,
            )
            for i, a in enumerate(out, start=1)
        ]

    def _effects(
        self, acct: Account, svc: ServiceProfile, n_days: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Multipliers on quantity and on price for one series, from its anomalies."""
        q = np.ones(n_days)
        p = np.ones(n_days)
        for a in self._anomalies:
            if a.sub_account_id != acct.id or a.service_name != svc.service_name:
                continue
            s = (a.start_date - self.world.start).days
            e = (a.end_date - self.world.start).days + 1
            match a.kind:
                case AnomalyKind.SPIKE | AnomalyKind.LEVEL_SHIFT:
                    q[s:e] *= a.magnitude
                case AnomalyKind.DRIFT:
                    q[s:e] *= (1 + a.magnitude) ** np.arange(1, e - s + 1)
                case AnomalyKind.PRICE_CHANGE:
                    p[s:e] *= a.magnitude
        return q, p

    @cached_property
    def _records(self) -> list[CostRecord]:
        n = self.world.days
        t = np.arange(n)
        dates = [self.world.start + timedelta(days=int(i)) for i in t]
        weekend = np.array([d.weekday() >= 5 for d in dates])
        records: list[CostRecord] = []

        for idx, (acct, svc, scale) in enumerate(self._series):
            rng = np.random.default_rng([self.seed, 2, idx])
            growth = (1 + svc.annual_growth) ** (t / 365)
            weekly = np.where(weekend, 1 - svc.weekend_dip, 1.0)
            noise = rng.lognormal(mean=-(svc.noise**2) / 2, sigma=svc.noise, size=n)
            q_eff, p_eff = self._effects(acct, svc, n)
            qty = svc.base_quantity * scale * growth * weekly * noise * q_eff
            price = svc.list_price * p_eff
            cost = qty * price
            tags = {
                "env": acct.environment,
                "team": TEAM_BY_ENV[acct.environment],
                "cost-center": f"CC-{acct.environment.upper()}",
            }
            for i in range(n):
                records.append(
                    CostRecord(
                        charge_date=dates[i],
                        sub_account_id=acct.id,
                        sub_account_name=acct.name,
                        service_name=svc.service_name,
                        service_category=svc.category,
                        region_id=svc.region_id,
                        sku_id=svc.sku_id,
                        consumed_quantity=round(float(qty[i]), 6),
                        consumed_unit=svc.unit,
                        billed_cost=round(float(cost[i]), 6),
                        tags=tags,
                    )
                )
        records.sort(key=lambda r: (r.charge_date, r.sub_account_id, r.service_name))
        return records
