# Roadmap

| # | Phase | Main deliverable | Status |
|---|---|---|---|
| 0 | Engineering foundations: uv, pyproject, src layout, ruff, mypy, pytest, pre-commit, CI | Repo + `costguard --version` + green CI | ✅ |
| 1 | Domain model + synthetic data: Pydantic, Protocols, DI, FOCUS, DuckDB | `costguard generate` / `show` with labelled injected anomalies | |
| 2 | AWS provider: IAM, STS, Cost Explorer, Budgets, EC2/EBS/EIP, paginators, retries, caching | Read-only AWS provider, tested offline | |
| 3 | CLI, config, logging, reports | `dashboard`, `trend`, `audit`; JSON/CSV/HTML | |
| 4 | Waste rules engine | Audit ranked by $/month saving | |
| 5 | Analytics + risk: STL, Isolation Forest, forecast intervals, Monte Carlo, CaR, ES, Kupiec | `anomalies`, `forecast`, `risk` | |
| 6 | Finance: price-volume variance, commitment NPV, FX exposure | `variance`, `commitments`, `fx` | |
| 7 | API, dashboard, Slack alerts | Demo dashboard | |
| 8 | Ship: Docker, releases, PyPI | `pipx install costguard` | |
| 9 | Docs + interview prep | Interview-ready repo | |
