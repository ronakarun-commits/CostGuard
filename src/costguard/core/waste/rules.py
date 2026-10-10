"""Built-in waste rules. Each yields ``Finding``s with an *estimated* monthly saving in USD."""

from __future__ import annotations

from collections.abc import Iterator

from costguard.core import pricing
from costguard.core.waste.engine import Finding, RuleContext, rule, severity_for

STOPPED_DAYS = 30
SNAPSHOT_DAYS = 180
LOW_CPU_PERCENT = 10.0


def _finding(rule_id: str, resource_id: str, region: str, title: str, detail: str,
             saving: float, action: str) -> Finding:  # fmt: skip
    return Finding(rule_id, resource_id, region, title, detail, round(saving, 2), action,
                   severity_for(saving))  # fmt: skip


@rule("ebs-unattached", "Unattached EBS volume",
      "Volumes in the 'available' state are billed in full but used by nothing.")  # fmt: skip
def ebs_unattached(ctx: RuleContext) -> Iterator[Finding]:
    for v in ctx.inventory.volumes:
        if v.state == "available":
            cost = pricing.ebs_monthly(v.volume_type, v.size_gib, v.region)
            yield _finding(
                "ebs-unattached", v.volume_id, v.region, "Unattached EBS volume",
                f"{v.size_gib} GiB {v.volume_type}, unattached, "
                f"created {ctx.age_days(v.created_at)} days ago",
                cost, "Snapshot it if needed, then delete the volume.",
            )  # fmt: skip


@rule("ebs-gp2-to-gp3", "gp2 volume can move to gp3",
      "gp3 is ~20% cheaper than gp2 with equal or better baseline performance.")  # fmt: skip
def ebs_gp2(ctx: RuleContext) -> Iterator[Finding]:
    for v in ctx.inventory.volumes:
        if v.volume_type == "gp2" and v.state == "in-use":
            saving = pricing.ebs_monthly("gp2", v.size_gib, v.region) - pricing.ebs_monthly(
                "gp3", v.size_gib, v.region
            )
            yield _finding(
                "ebs-gp2-to-gp3", v.volume_id, v.region, "gp2 volume → gp3",
                f"{v.size_gib} GiB gp2 attached to {', '.join(v.attached_instance_ids) or '-'}",
                saving, "Modify the volume type to gp3 (online, no downtime).",
            )  # fmt: skip


@rule("eip-idle", "Idle Elastic IP",
      "Every public IPv4 address costs $0.005/h (since Feb 2024); unassociated ones do "
      "nothing.")  # fmt: skip
def eip_idle(ctx: RuleContext) -> Iterator[Finding]:
    for ip in ctx.inventory.elastic_ips:
        if ip.association_id is None:
            yield _finding(
                "eip-idle", ip.allocation_id, ip.region, "Idle Elastic IP",
                f"{ip.public_ip} is not associated with any resource",
                pricing.public_ip_monthly(ip.region), "Release the address.",
            )  # fmt: skip


@rule("ec2-stopped-long", "Long-stopped instance",
      f"Stopped instances stop compute billing, but their EBS volumes are still billed. "
      f"Flags instances stopped and launched over {STOPPED_DAYS} days ago.")  # fmt: skip
def ec2_stopped(ctx: RuleContext) -> Iterator[Finding]:
    vols_by_instance: dict[str, list[tuple[str, int, str]]] = {}
    for v in ctx.inventory.volumes:
        for iid in v.attached_instance_ids:
            vols_by_instance.setdefault(iid, []).append((v.volume_type, v.size_gib, v.region))
    for i in ctx.inventory.instances:
        if i.state == "stopped" and ctx.age_days(i.launched_at) > STOPPED_DAYS:
            vols = vols_by_instance.get(i.instance_id, [])
            cost = sum(pricing.ebs_monthly(t, s, r) for t, s, r in vols)
            yield _finding(
                "ec2-stopped-long", i.instance_id, i.region, "Long-stopped instance",
                f"{i.instance_type} stopped; {len(vols)} volume(s), "
                f"{sum(s for _, s, _ in vols)} GiB still billed",
                cost, "Create an AMI/snapshot and terminate, or delete the volumes.",
            )  # fmt: skip


@rule("ec2-low-cpu", "Under-used instance",
      f"Running instances averaging under {LOW_CPU_PERCENT:.0f}% CPU over 14 days can usually "
      "drop one size (half the price). Needs CPU metrics (audit --with-cpu).")  # fmt: skip
def ec2_low_cpu(ctx: RuleContext) -> Iterator[Finding]:
    for i in ctx.inventory.instances:
        if i.state != "running" or i.avg_cpu_percent is None:
            continue
        if i.avg_cpu_percent >= LOW_CPU_PERCENT:
            continue
        smaller = pricing.one_size_down(i.instance_type)
        now_cost = pricing.instance_monthly(i.instance_type, i.region)
        new_cost = pricing.instance_monthly(smaller, i.region) if smaller else None
        if now_cost is None or new_cost is None:
            continue
        yield _finding(
            "ec2-low-cpu", i.instance_id, i.region, "Under-used instance",
            f"{i.instance_type} averaging {i.avg_cpu_percent:.1f}% CPU",
            now_cost - new_cost, f"Right-size to {smaller} (check memory first).",
        )  # fmt: skip


@rule("ec2-previous-gen", "Previous-generation instance",
      "Older families (t2, m4, m5, c4, r4) cost the same or more than current ones for less "
      "performance.")  # fmt: skip
def ec2_previous_gen(ctx: RuleContext) -> Iterator[Finding]:
    for i in ctx.inventory.instances:
        if i.state != "running":
            continue
        family, _, size = i.instance_type.partition(".")
        target_family = pricing.NEXT_GEN.get(family)
        if target_family is None:
            continue
        target = f"{target_family}.{size}"
        old = pricing.instance_monthly(i.instance_type, i.region)
        new = pricing.instance_monthly(target, i.region)
        if old is None or new is None or old <= new:
            continue
        yield _finding(
            "ec2-previous-gen", i.instance_id, i.region, "Previous-generation instance",
            f"{i.instance_type} → {target}", old - new,
            f"Change instance type to {target} at the next maintenance window.",
        )  # fmt: skip


@rule("snapshot-orphaned", "Old orphaned snapshot",
      f"Snapshots older than {SNAPSHOT_DAYS} days whose source volume no longer "
      "exists.")  # fmt: skip
def snapshot_orphaned(ctx: RuleContext) -> Iterator[Finding]:
    live = {v.volume_id for v in ctx.inventory.volumes}
    for s in ctx.inventory.snapshots:
        age = ctx.age_days(s.started_at)
        if age > SNAPSHOT_DAYS and (s.source_volume_id is None or s.source_volume_id not in live):
            yield _finding(
                "snapshot-orphaned", s.snapshot_id, s.region, "Old orphaned snapshot",
                f"{s.size_gib} GiB, {age} days old, source volume gone",
                pricing.snapshot_monthly(s.size_gib, s.region),
                "Confirm it is not a backup you must keep (retention policy), then delete.",
            )  # fmt: skip
