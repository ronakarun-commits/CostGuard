# costguard

[![CI](https://github.com/<your-github-username>/costguard/actions/workflows/ci.yml/badge.svg)](https://github.com/<your-github-username>/costguard/actions/workflows/ci.yml)

AWS FinOps and cost-risk toolkit. costguard pulls AWS cost data across accounts, keeps its history,
detects anomalies, forecasts spend with uncertainty, quantifies budget risk (Cost-at-Risk,
Expected Shortfall, breach probability), finds waste, and runs finance analytics
(price-volume variance, commitment NPV, USD/INR FX exposure).

> **Status:** Phase 0 (engineering foundations). See [`docs/roadmap.md`](docs/roadmap.md).

## Quick start (Windows PowerShell)

```powershell
git clone https://github.com/<your-github-username>/costguard.git
cd costguard
uv sync                       # creates .venv and installs costguard + dev tools
uv run costguard --version
uv run pre-commit install     # run lint/type/secret checks on every commit
```

## Development

| Task | Command |
|---|---|
| Run tests (with coverage) | `uv run pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type-check | `uv run mypy` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |

## Architecture

Ports and adapters. `core/` is pure Python (no boto3, no I/O). Everything that talks to the outside
world (AWS, DuckDB, the terminal, HTTP) is an adapter around it.

```
src/costguard/
├── core/         # domain models, rules, analytics, risk, finance (pure)
├── providers/    # Synthetic, AWS Cost Explorer, FOCUS file → CostProvider protocol
├── storage/      # DuckDB repository + API-call cache
├── interfaces/   # API, dashboard, notifiers
└── cli.py        # Typer entry point
```

Design decisions are recorded in [`docs/adr/`](docs/adr/).

## Cost safety

Phases 0–1 need no AWS account. costguard only ever needs **read-only** IAM permissions, Cost
Explorer calls (≈ $0.01 each) are cached, and secrets are blocked from git by gitleaks.

## License

MIT
