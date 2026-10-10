"""Registry pattern: each rule is a plain function registered with ``@rule``.

Adding a rule = writing one function; the engine, CLI and reports pick it up automatically
(open/closed principle). Rules are pure: inventory in, findings out, no AWS calls.

Alternatives: a class hierarchy (``class Rule(ABC)``) adds boilerplate for no benefit here;
entry-point plugins (``importlib.metadata.entry_points``) would let *other packages* add rules and
is the natural next step if that is ever needed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from costguard.core.resources import Inventory

Severity = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    resource_id: str
    region: str
    title: str
    detail: str
    monthly_saving_usd: float
    action: str
    severity: Severity = "medium"


@dataclass(frozen=True, slots=True)
class RuleContext:
    inventory: Inventory
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def age_days(self, when: datetime) -> int:
        return (self.now - when).days


RuleFn = Callable[[RuleContext], Iterable[Finding]]


@dataclass(frozen=True, slots=True)
class RuleSpec:
    id: str
    title: str
    description: str
    fn: RuleFn


REGISTRY: dict[str, RuleSpec] = {}


def rule(rule_id: str, title: str, description: str) -> Callable[[RuleFn], RuleFn]:
    def register(fn: RuleFn) -> RuleFn:
        if rule_id in REGISTRY:
            msg = f"duplicate rule id {rule_id!r}"
            raise ValueError(msg)
        REGISTRY[rule_id] = RuleSpec(rule_id, title, description, fn)
        return fn

    return register


def severity_for(saving: float) -> Severity:
    return "high" if saving >= 50 else "medium" if saving >= 10 else "low"


def run_rules(
    inventory: Inventory,
    *,
    now: datetime | None = None,
    only: Iterable[str] | None = None,
    min_saving: float = 0.0,
) -> list[Finding]:
    """Run (a subset of) the rules; findings ranked by estimated monthly saving, largest first."""
    ctx = RuleContext(inventory, now or datetime.now(UTC))
    wanted = set(only) if only is not None else set(REGISTRY)
    unknown = wanted - set(REGISTRY)
    if unknown:
        msg = f"unknown rule(s): {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    findings = [
        f
        for rid in sorted(wanted)
        for f in REGISTRY[rid].fn(ctx)
        if f.monthly_saving_usd >= min_saving
    ]
    return sorted(findings, key=lambda f: (-f.monthly_saving_usd, f.rule_id, f.resource_id))
