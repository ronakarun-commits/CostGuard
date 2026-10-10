# 6. Configuration, logging and report rendering

Date: 2026-10-10 · Status: Accepted

## Decision
- **Config:** `pydantic-settings`. Precedence: flags > `COSTGUARD_*` env > `.env` > `costguard.toml`
  > defaults. Validated once at start-up (bad values exit 2 with the field name). Secrets are
  `SecretStr`, so they never appear in reprs or logs.
- **Logging:** stdlib `logging` to **stderr**; `--log-json` emits one JSON object per line for
  cron/containers. stdout stays clean for `--format json/csv`.
- **Reports:** core builds a format-neutral `Report` (KPIs + table sections); renderers produce Rich,
  JSON, CSV or a self-contained HTML page (inline CSS + SVG, light/dark, no JavaScript).
- **Exit codes:** 0 ok · 2 usage/config · 3 no data · 4 auth · 5 permission · 6 AWS API ·
  10 audit threshold exceeded — so scripts can branch on *why* it failed.
- **Partial periods** (current month/week) are flagged and their % change suppressed.

## Alternatives
| Choice | Alternative | Why not |
|---|---|---|
| pydantic-settings | dynaconf, plain `os.environ` | Already using Pydantic types; typed, validated, documented in one class |
| stdlib logging | structlog, loguru | Extra dependency; boto3 logs via stdlib anyway |
| HTML via f-strings | Jinja2 templates | One small page; Jinja adds a dependency for little gain. Revisit if reports grow |
| Global `--db/--currency` | Per-command options | One place to set them; matches `git -C`, `aws --profile` conventions |
