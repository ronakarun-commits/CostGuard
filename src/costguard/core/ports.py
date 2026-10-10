"""Ports: the interfaces the core needs from the outside world.

``typing.Protocol`` is used instead of an abstract base class so adapters don't have to import or
inherit from anything in the core (structural typing: "if it has these methods, it fits"). mypy
still checks conformance wherever an adapter is passed where a port is expected.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from costguard.core.models import CostRecord, DateRange, InjectedAnomaly


@runtime_checkable
class CostProvider(Protocol):
    """Anything that can return cost records for a date range."""

    name: str

    def get_costs(self, period: DateRange) -> list[CostRecord]:
        """Return all records with ``period.start <= charge_date < period.end``."""
        ...


@runtime_checkable
class LabelledCostProvider(CostProvider, Protocol):
    """A provider that also knows the ground-truth anomalies in its data (synthetic only)."""

    def get_anomalies(self, period: DateRange) -> list[InjectedAnomaly]: ...


class CostRepository(Protocol):
    """Persistence for cost history."""

    def upsert_costs(self, records: Iterable[CostRecord], source: str) -> int: ...

    def upsert_anomalies(self, anomalies: Iterable[InjectedAnomaly]) -> int: ...

    def get_costs(self, period: DateRange | None = None) -> Sequence[CostRecord]: ...

    def get_anomalies(self) -> Sequence[InjectedAnomaly]: ...
