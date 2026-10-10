# 10. Service layer, API, dashboard, alerts and delivery

Date: 2026-10-10 · Status: Accepted

## Decision
- **Application services** (`services.py`): one function per use case returning a `Report`.
  The CLI, the API and the dashboard all call them, so there is one implementation per feature.
- **API**: FastAPI, read-only (DuckDB opened `read_only=True` per request), `X-API-Key` auth with
  `hmac.compare_digest` over every configured key (rotation without downtime). It refuses to start
  without keys; `--no-auth` is only allowed on localhost. Errors are JSON `{error, hint}`.
  Optional extra (`costguard[api]`), so the CLI stays light.
- **Dashboard**: a single self-contained web page served by the API at `/` (`costguard ui`),
  with hand-drawn SVG charts (no JS dependencies, works offline), one `/v1/ui/overview` call
  cached until the database changes, light/dark themes and USD/INR with Indian digit grouping.
  The hero is a **fan chart** of cumulative month spend (as central banks use for inflation
  forecasts). The earlier Streamlit dashboard remains as `costguard ui --streamlit`.
- **Alerts**: Slack incoming webhook via stdlib `urllib`; `build_alerts` is a pure function;
  exit code 11 = alerts raised (so cron/CI can tell "found something" from "crashed").
- **Delivery**: multi-stage Docker (uv build stage, slim non-root runtime, healthcheck);
  scheduled GitHub Action with **AWS OIDC** instead of stored keys; tag-driven release to PyPI
  with **Trusted Publishing** and to GHCR.

## Alternatives
| Choice | Alternative | Why not |
|---|---|---|
| Service layer | API calls the CLI functions | CLI functions print and exit; mixing transport with logic |
| API keys | OAuth2/JWT | No users or browser login; machine-to-machine read access. Put it behind an IdP proxy if needed |
| FastAPI | Flask | Type-hint validation and OpenAPI docs for free |
| Plain HTML + SVG page on the API | Streamlit, React + chart library | Streamlit was the first version: fast to build, but generic-looking and hard to control. React adds a build step and node toolchain; one static page keeps install = `pip install` |
| GitHub Actions + OIDC | Lambda + EventBridge | Same result with fewer parts (no EFS/S3 state sync, no image limits) for a daily job |
| Slack webhook | Slack SDK / bot token | Webhook can only post to one channel: smallest possible privilege |
