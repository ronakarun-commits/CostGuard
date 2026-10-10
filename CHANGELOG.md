# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): MAJOR for breaking CLI/API changes, MINOR for new
features, PATCH for fixes.

## [1.0.0] - 2026-10-10

### Added
- Synthetic multi-account AWS cost generator with labelled anomalies; DuckDB history store.
- Read-only AWS provider: Cost Explorer (cached), Budgets, EC2 inventory.
- Reports: dashboard, trend, show — as Rich tables, JSON, CSV or self-contained HTML.
- Waste audit with 7 rules ranked by estimated monthly saving.
- Anomaly detection (baseline / STL / Isolation Forest) with evaluation against ground truth.
- Monte Carlo forecasting; month-end Cost-at-Risk, Expected Shortfall and breach probability,
  conformally calibrated and backtested (Kupiec).
- Finance: price-volume-calendar variance, Savings Plan NPV/hedge optimiser, USD→INR FX bridge
  and stress test with ECB rates.
- Read-only FastAPI service with API-key auth; Streamlit dashboard; Slack alerts.
- Docker image (non-root), CI on Linux + Windows, release workflow (PyPI + GHCR).

[1.0.0]: https://github.com/your-github-username/costguard/releases/tag/v1.0.0
