# 3. Ports-and-adapters architecture

Date: 2026-10-10 · Status: Accepted

## Context
costguard reads from AWS (paid, slow, needs credentials), synthetic data, and FOCUS files, and outputs to
a CLI, an API, a dashboard and Slack. The analytics must be testable without any of these.

## Decision
`core/` is pure Python and defines `Protocol` ports (`CostProvider`, `ResourceProvider`, repository).
`providers/`, `storage/`, `interfaces/` and `cli.py` are adapters that depend on `core`, never the reverse.

## Alternatives
- **Layered / MVC.** Simpler, but AWS calls tend to leak into business logic.
- **One module per feature.** Fast at first, hard to test offline.

## Consequences
More files and an indirection step, but the whole analytics stack is unit-testable with synthetic data
and zero AWS cost, and adding a provider (Azure, GCP) means adding an adapter only.
