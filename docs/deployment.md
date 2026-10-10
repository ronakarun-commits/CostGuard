# Deployment and operations

costguard runs in four ways. Pick the lightest one that does the job.

| Mode | Command | Good for |
|---|---|---|
| CLI on your machine | `uv run costguard ...` | Exploring, ad-hoc reports |
| Docker / Compose | `docker compose up api ui` | Demo, a small always-on API + dashboard |
| Scheduled GitHub Action | `.github/workflows/scheduled-alert.yml` | Daily anomaly/budget alerts with zero servers |
| `pipx install costguard` | after a PyPI release | Using it as a tool, not developing it |

## Docker

```powershell
docker build -t costguard .                          # API only (~250 MB)
docker build --build-arg EXTRAS=all -t costguard .   # + Streamlit dashboard

docker volume create cgdata
docker run --rm -v cgdata:/data costguard generate
docker run -d -p 127.0.0.1:8000:8000 -v cgdata:/data:ro `
  -e COSTGUARD_API_KEYS='["<long-random-key>"]' costguard
curl -H "X-API-Key: <long-random-key>" http://localhost:8000/v1/risk
```

Image design:
- **Multi-stage**: uv resolves and installs dependencies in a build stage; the runtime stage gets
  only the virtualenv (no uv, compilers or caches).
- **Layer caching**: `pyproject.toml` + `uv.lock` are copied before the source, so code changes
  don't reinstall dependencies.
- **Non-root** user (uid 10001), data in a `/data` volume, JSON logs, a `HEALTHCHECK` on `/health`.
- Compose adds `read_only: true`, `cap_drop: [ALL]` and `no-new-privileges`, and mounts the data
  volume read-only into the API and dashboard.

### Compose demo
```powershell
Set-Content .env 'COSTGUARD_API_KEYS=["change-me-to-a-long-random-string"]'
docker compose run --rm generate     # one year of synthetic data
docker compose up api ui             # http://localhost:8000/docs  and  http://localhost:8501
```

## Scheduled checks on GitHub (no servers, no stored AWS keys)

GitHub Actions can assume an AWS IAM role through **OIDC**: GitHub issues a short-lived signed
token for the workflow run, and AWS exchanges it for temporary credentials. No access keys are
stored in GitHub, and the role can be restricted to this repository and branch.

1. **IAM → Identity providers → Add provider**: OpenID Connect,
   URL `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. **Create a role** with the trust policy below and attach [`iam-policy.json`](iam-policy.json):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {"Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"},
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
         "StringLike": {"token.actions.githubusercontent.com:sub": "repo:<you>/costguard:ref:refs/heads/main"}
       }
     }]
   }
   ```
   The `sub` condition matters: without it, *any* GitHub repository could assume the role.
3. In the GitHub repo: **Settings → Secrets and variables → Actions**
   - variable `AWS_ROLE_ARN` = the role ARN
   - variable `COSTGUARD_BUDGET_MONTHLY_USD` = e.g. `25`
   - secret `COSTGUARD_SLACK_WEBHOOK_URL` (optional)
4. Run the workflow once manually (**Actions → Scheduled cost check → Run workflow**), then
   uncomment the `schedule:` trigger.

Cost: Cost Explorer responses are cached between runs (`actions/cache`), so a daily run is
usually 1–3 requests ≈ $0.01–0.03/day.

### Alternative: AWS Lambda + EventBridge
The same `costguard alert` can run as a container-image Lambda on an EventBridge schedule,
with the DuckDB file on EFS or synced to S3. It is more moving parts (EFS/VPC or S3 sync,
packaging, a 10 GB image limit) for the same result, so GitHub Actions is the default here.

## Releasing

Versioning is [SemVer](https://semver.org/); the version lives only in `pyproject.toml`.

One-time setup:
1. **PyPI → Your account → Publishing → Add a new pending publisher**: project `costguard`
   (check the name is free first, or rename the distribution), owner/repo, workflow
   `release.yml`, environment `pypi`. This is **Trusted Publishing**: PyPI trusts GitHub's OIDC
   token, so no PyPI API token is stored anywhere.
2. GitHub **Settings → Environments → New environment** `pypi` (optionally require approval).

Each release:
```powershell
uv version --bump minor          # or patch / major; updates pyproject.toml
# edit CHANGELOG.md
git commit -am "release: v$(uv version --short)"
git tag "v$(uv version --short)"
git push --follow-tags
```
The workflow refuses to publish if the tag doesn't match the package version, runs the tests,
publishes to PyPI, pushes `ghcr.io/<you>/costguard:<version>` (amd64 + arm64), and creates a
GitHub Release.
