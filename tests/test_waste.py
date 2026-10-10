from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from costguard.core import pricing
from costguard.core.resources import ElasticIp, Instance, Inventory, Snapshot, Volume
from costguard.core.waste import REGISTRY, Finding, RuleContext, rule, run_rules
from costguard.providers.synthetic_inventory import synthetic_inventory

NOW = datetime(2026, 10, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=400)


def _inv(**kw: object) -> Inventory:
    return Inventory("1", NOW, **kw)  # type: ignore[arg-type]


def test_pricing_helpers() -> None:
    assert pricing.ebs_monthly("gp3", 100, "us-east-1") == pytest.approx(8.0)
    assert pricing.ebs_monthly("unknown", 100, "us-east-1") == pytest.approx(8.0)
    assert pricing.public_ip_monthly("us-east-1") == pytest.approx(3.65)
    assert pricing.instance_monthly("nope.large", "us-east-1") is None
    assert pricing.one_size_down("m6i.xlarge") == "m6i.large"
    assert pricing.one_size_down("t3.nano") is None
    assert pricing.one_size_down("weird") is None


def test_unattached_volume() -> None:
    inv = _inv(volumes=(Volume("v1", "us-east-1", 500, "gp3", "available", OLD),))
    (f,) = run_rules(inv, now=NOW, only=["ebs-unattached"])
    assert f.monthly_saving_usd == pytest.approx(40.0)
    assert f.severity == "medium"
    assert "400 days" in f.detail


def test_gp2_to_gp3_only_in_use() -> None:
    inv = _inv(volumes=(
        Volume("a", "us-east-1", 1000, "gp2", "in-use", OLD, ("i-1",)),
        Volume("b", "us-east-1", 1000, "gp2", "available", OLD),
    ))  # fmt: skip
    (f,) = run_rules(inv, now=NOW, only=["ebs-gp2-to-gp3"])
    assert f.resource_id == "a"
    assert f.monthly_saving_usd == pytest.approx(20.0)


def test_idle_eip() -> None:
    inv = _inv(elastic_ips=(
        ElasticIp("e1", "1.1.1.1", "us-east-1", None),
        ElasticIp("e2", "1.1.1.2", "us-east-1", "assoc", "i-1"),
    ))  # fmt: skip
    assert [f.resource_id for f in run_rules(inv, now=NOW, only=["eip-idle"])] == ["e1"]


def test_stopped_instance_counts_attached_volumes() -> None:
    inv = _inv(
        instances=(Instance("i-1", "us-east-1", "m5.large", "stopped", OLD),
                   Instance("i-2", "us-east-1", "m5.large", "stopped", NOW - timedelta(days=3))),
        volumes=(Volume("v", "us-east-1", 100, "gp3", "in-use", OLD, ("i-1",)),),
    )  # fmt: skip
    (f,) = run_rules(inv, now=NOW, only=["ec2-stopped-long"])
    assert f.resource_id == "i-1"
    assert f.monthly_saving_usd == pytest.approx(8.0)


def test_low_cpu() -> None:
    inv = _inv(instances=(
        Instance("busy", "us-east-1", "m6i.xlarge", "running", OLD, avg_cpu_percent=50),
        Instance("idle", "us-east-1", "m6i.xlarge", "running", OLD, avg_cpu_percent=2),
        Instance("nodata", "us-east-1", "m6i.xlarge", "running", OLD),
        Instance("tiny", "us-east-1", "t3.nano", "running", OLD, avg_cpu_percent=1),
    ))  # fmt: skip
    (f,) = run_rules(inv, now=NOW, only=["ec2-low-cpu"])
    assert f.resource_id == "idle"
    assert f.monthly_saving_usd == pytest.approx((0.192 - 0.096) * 730)
    assert f.severity == "high"


def test_previous_gen() -> None:
    inv = _inv(instances=(
        Instance("old", "us-east-1", "m4.large", "running", OLD),
        Instance("new", "us-east-1", "m6i.large", "running", OLD),
        Instance("stopped", "us-east-1", "m4.large", "stopped", OLD),
        Instance("same-price", "us-east-1", "m5.large", "running", OLD),
    ))  # fmt: skip
    assert [f.resource_id for f in run_rules(inv, now=NOW, only=["ec2-previous-gen"])] == ["old"]


def test_orphaned_snapshots() -> None:
    inv = _inv(
        volumes=(Volume("live", "us-east-1", 10, "gp3", "in-use", OLD, ("i",)),),
        snapshots=(Snapshot("s-orphan", "us-east-1", 100, OLD, "gone"),
                   Snapshot("s-live", "us-east-1", 100, OLD, "live"),
                   Snapshot("s-young", "us-east-1", 100, NOW - timedelta(days=10), "gone")),
    )  # fmt: skip
    found = run_rules(inv, now=NOW, only=["snapshot-orphaned"])
    assert [f.resource_id for f in found] == ["s-orphan"]


def test_ranking_min_saving_and_unknown_rule() -> None:
    findings = run_rules(synthetic_inventory(), min_saving=5)
    savings = [f.monthly_saving_usd for f in findings]
    assert savings == sorted(savings, reverse=True)
    assert min(savings) >= 5
    with pytest.raises(ValueError, match="unknown rule"):
        run_rules(_inv(), only=["nope"])


def test_synthetic_inventory_triggers_every_rule() -> None:
    hit = {f.rule_id for f in run_rules(synthetic_inventory())}
    assert hit == set(REGISTRY)


def test_registry_rejects_duplicates_and_accepts_new_rules() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        rule("eip-idle", "x", "y")(lambda ctx: [])

    @rule("test-custom", "Custom", "test only")
    def custom(ctx: RuleContext) -> list[Finding]:
        return [Finding("test-custom", "r", "x", "t", "d", 1.0, "a", "low")]

    try:
        assert run_rules(_inv(), only=["test-custom"])[0].rule_id == "test-custom"
    finally:
        REGISTRY.pop("test-custom")
