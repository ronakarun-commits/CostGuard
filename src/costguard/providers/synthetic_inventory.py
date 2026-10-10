"""A deterministic fake EC2 inventory with realistic waste, so ``audit`` works offline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from costguard.core.resources import ElasticIp, Instance, Inventory, Snapshot, Volume

TYPES = ["t3.medium", "m6i.large", "m6i.xlarge", "c6i.large", "r6i.large", "t2.medium",
         "m4.large", "m5.xlarge"]  # fmt: skip
REGIONS = ["ap-south-1", "us-east-1"]


def synthetic_inventory(seed: int = 42, now: datetime | None = None) -> Inventory:
    now = now or datetime.now(UTC)
    rng = np.random.default_rng([seed, 99])
    instances: list[Instance] = []
    volumes: list[Volume] = []
    for n in range(24):
        region = REGIONS[0] if n < 18 else REGIONS[1]
        itype = TYPES[int(rng.integers(len(TYPES)))]
        state = "stopped" if n in (5, 17) else "running"
        cpu = float(rng.uniform(2, 9)) if n % 6 == 0 else float(rng.uniform(15, 70))
        iid = f"i-{seed:04x}{n:013x}"
        instances.append(
            Instance(iid, region, itype, state, now - timedelta(days=int(rng.integers(20, 700))),
                     avg_cpu_percent=cpu if state == "running" else None,
                     tags=(("team", "platform" if n % 2 else "analytics"),))
        )  # fmt: skip
        vtype = "gp2" if n % 3 == 0 else "gp3"
        volumes.append(
            Volume(f"vol-{seed:04x}{n:013x}", region, int(rng.choice([30, 50, 100, 200])), vtype,
                   "in-use", now - timedelta(days=200), (iid,))
        )  # fmt: skip
    for n in range(4):  # forgotten, unattached volumes
        volumes.append(
            Volume(f"vol-{seed:04x}f{n:012x}", REGIONS[n % 2], [500, 120, 1000, 64][n],
                   ["gp3", "gp2", "st1", "io1"][n], "available",
                   now - timedelta(days=[45, 300, 120, 15][n]))
        )  # fmt: skip
    eips = [
        ElasticIp(f"eipalloc-{seed:04x}{n:013x}", f"13.233.10.{n + 10}", REGIONS[0],
                  None if n < 3 else f"eipassoc-{n}", None if n < 3 else instances[n].instance_id)
        for n in range(6)
    ]  # fmt: skip
    snapshots = [
        Snapshot(f"snap-{seed:04x}{n:013x}", REGIONS[0], [100, 500, 50, 200, 30][n],
                 now - timedelta(days=[400, 250, 90, 700, 30][n]),
                 source_volume_id=None if n % 2 == 0 else f"vol-gone{n}")
        for n in range(5)
    ]  # fmt: skip
    return Inventory("111111111111", now, tuple(volumes), tuple(eips), tuple(instances),
                     tuple(snapshots))  # fmt: skip
