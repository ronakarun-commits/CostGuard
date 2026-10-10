"""Waste rules engine: a registry of small, independent rules over an ``Inventory``."""

from costguard.core.waste import rules as _rules  # noqa: F401  (registers the built-in rules)
from costguard.core.waste.engine import REGISTRY, Finding, RuleContext, rule, run_rules

__all__ = ["REGISTRY", "Finding", "RuleContext", "rule", "run_rules"]
