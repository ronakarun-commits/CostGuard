# 5. Read-only AWS adapter: credential chain, caching, retries, offline tests

Date: 2026-10-10 · Status: Accepted

## Decision
- **Credentials:** only boto3's default provider chain (`--profile` / `AWS_PROFILE`, SSO, env vars,
  instance roles). costguard never stores keys and warns loudly if the identity is root.
- **Least privilege:** a read-only policy ([`../iam-policy.json`](../iam-policy.json)); no write APIs.
- **Cost Explorer:** `GetCostAndUsage`, DAILY, two GroupBy levels (`service` or `usage_type`)
  because the API allows at most two dimensions. Account names come from
  `DimensionValueAttributes`, so no Organizations permission is needed.
- **Caching:** every response is cached in DuckDB by a hash of the request. Periods ending in the
  last 3 days expire after 6 h (Cost Explorer restates recent data); older ones never expire.
- **Retries:** botocore `adaptive` mode, 10 attempts (exponential backoff + client-side rate
  limiting; Cost Explorer throttles hard).
- **Errors:** botocore exceptions are translated to `AwsAuthError` / `AwsPermissionError` /
  `AwsApiError` with a hint and a distinct exit code.
- **Tests:** botocore **Stubber** for Cost Explorer (moto's CE support is minimal, and Stubber
  validates requests against the real API model); **moto** for EC2, Budgets and STS. An autouse
  fixture fakes credentials and hides `~/.aws` so tests can never call real AWS.

## Alternatives
| Choice | Alternative | Why not |
|---|---|---|
| Cost Explorer API | CUR / Data Exports in S3 + Athena | Better for large orgs, but needs S3, Athena and setup cost; planned as a FOCUS-file provider |
| Stubber for CE | moto | moto returns empty CE data; no realistic pagination |
| Stubber | `unittest.mock` on the client | Mocks don't validate parameters against the AWS API model |
| Own cache | `requests-cache` / no cache | boto3 isn't requests-based; no cache = paying again on every run |
