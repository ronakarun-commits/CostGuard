# Connecting costguard to AWS safely

Phases 0–1 and every test run need **no AWS account**. Do this only when you want real data.

## One-time account hardening (AWS console)
1. Sign in as root **once**: enable MFA on root, and make sure root has **no access keys**.
2. **Billing → Budgets → Create budget**: a $1 monthly cost budget with an email alert.
3. **Billing → Cost Explorer**: enable it (data appears after ~24 h).
4. **IAM Identity Center** (recommended) or IAM: create a user for yourself, plus a permission set
   / policy for costguard using [`iam-policy.json`](iam-policy.json) (read-only — costguard can
   never create, change or delete anything).

## Local profile (Windows PowerShell)
```powershell
winget install Amazon.AWSCLI
aws configure sso --profile costguard      # IAM Identity Center (short-lived credentials)
# or, for an IAM user with access keys:
aws configure --profile costguard

uv run costguard aws whoami --profile costguard   # free; warns if these are root credentials
uv run costguard pull --profile costguard --days 30
```
Set `$env:AWS_PROFILE = "costguard"` to skip `--profile`.

## What costs money
| Call | Price | costguard behaviour |
|---|---|---|
| `ce:GetCostAndUsage` | $0.01 / request (each page) | Cached in DuckDB; recent days re-fetched after 6 h; `--refresh` bypasses |
| `ce:GetCostForecast` | $0.01 / request | Not used — costguard forecasts locally |
| `ec2:Describe*`, `budgets:ViewBudget`, `sts:GetCallerIdentity` | Free | — |
| `cloudwatch:GetMetricStatistics` | Free tier, then per 1,000 metrics | Opt-in only |

A 90-day `pull` is typically 1–5 requests (≈ $0.01–0.05).

## Never
- Use root credentials or root access keys.
- Commit `~/.aws/` or any key (gitleaks blocks it in pre-commit and CI).
