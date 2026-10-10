# Interview guide

How to talk about costguard, and a map of what to understand before you do.

## 30-second pitch
> "Cloud bills are volatile and usually reviewed after month-end. costguard treats AWS spend
> like a risk exposure. It pulls daily cost across accounts and detects anomalies. Then it
> simulates thousands of possible months to give Cost-at-Risk and the probability of breaching
> budget, and it explains changes with a finance-style price/volume bridge. The interesting
> part: my first risk model failed its own backtest, with 95% bounds breached 9–17% of days. I
> diagnosed why and fixed it with conformal calibration, measured out of sample."

## 2-minute architecture walkthrough
1. **Ports and adapters.** A pure `core` (models, analytics, finance, waste rules) with
   `Protocol` ports. Adapters: synthetic data, AWS Cost Explorer, ECB FX in; CLI, FastAPI,
   Streamlit, Slack out. Why: the core is testable without AWS, and a new cloud is a new adapter.
2. **Data.** FOCUS-aligned schema at day × account × service × usage-type grain, in DuckDB
   (columnar, embedded). Idempotent upserts and forward-only migrations.
3. **AWS.** Read-only IAM policy, credential chain/SSO only, adaptive retries, and every
   $0.01 Cost Explorer call cached. Tested offline with Stubber and moto.
4. **Analytics.** Anomaly detectors evaluated against labelled synthetic anomalies. ETS
   Monte Carlo forecast → CaR / ES / breach probability, with Kupiec backtesting and conformal
   calibration.
5. **Finance.** Price-volume-calendar variance, Savings Plans as a hedge with an NPV per
   payment option, USD/INR exposure.
6. **Engineering.** 190+ tests (~99% coverage), strict mypy, ruff, pre-commit + gitleaks,
   CI on Linux and Windows, non-root Docker, OIDC for AWS and PyPI.

## Numbers to remember
- Baseline detector: **~98% precision, ~80% recall**, ~1 day delay (5 seeds).
- Month-end forecast: **0.4–2.1% MAPE**.
- Raw 95% bounds breached **9–17%** → calibrated **2–9%**; Kupiec rejects raw on all seeds.
- Commitment optimiser 20× faster after replacing per-day simulation with a tabulated
  coverage curve E[min(r, x)] (verified equal to brute force).

## Likely questions (and the honest answers)

**Why not just use AWS Cost Anomaly Detection?**
It's free and managed, but it's a black box: you can't measure its precision/recall, tune it,
or get a probabilistic month-end view. costguard's detector is explainable ("3x the usual
Tuesday") and *measured*. In production I'd run both and compare.

**Why synthetic data?**
Real bills never come labelled, so you can't score a detector on them. Synthetic data with
injected, labelled anomalies gives ground truth, costs nothing, and is deterministic for tests.
The limitation: the anomaly shapes are my model of reality (spike, level shift, drift, price
change). Real data has credits, refunds and tax that the generator doesn't produce.

**Why log scale and median/MAD?**
Cost is multiplicative: a 2x jump matters at $10 and at $1,000. Robust statistics stop
yesterday's anomaly from inflating the baseline and hiding tomorrow's.

**Why is STL worse than the simple baseline?**
STL is fitted on the whole series, so it uses future data and absorbs level shifts into the
trend. It's great for investigating history, not for live alerting.

**What is Cost-at-Risk? Why Expected Shortfall too?**
CaR(95%) is the 95th percentile of simulated month-end spend. ES is the mean of the worst 5%:
it says how bad the tail is, not just where it starts. ES is sub-additive (coherent) and VaR
isn't, which is why Basel FRTB moved to ES.

**How do you know the risk numbers are right?**
Backtesting. If 95% bounds are right, actual spend exceeds them on about 5% of days. Kupiec's
proportion-of-failures likelihood-ratio test checks that. The raw model failed. Breaches
clustered inside regime changes, so Gaussian errors were the problem. I tried a jump overlay and
it didn't help. Conformal calibration did: it widens the bounds by the quantile of the model's
own past normalised errors, using only data before each forecast date, so the test stays
honest. Next step: Christoffersen's test for clustering of breaches.

**Explain conformal prediction simply.**
"Look at how wrong my bounds were in the past. Stretch them by exactly enough that they would
have covered 95% of past days." It's distribution-free, but it assumes the past errors look like
future ones, so it lags after a new regime starts.

**Price-volume variance?**
For each line item: volume effect = ΔQ × old price, price effect = Δprice × new quantity. These
sum exactly to the change. I also split out a calendar effect, so 31 vs 28 days isn't blamed
on engineers. It needs real units, so it needs usage-type detail from Cost Explorer.

**How do you size a Savings Plan?**
A commitment is a hedge on uncertain usage. I simulate usage paths (drift and volatility from
history, plus the real daily shape), then for each term and payment option pick the hourly
commitment that maximises the 5th-percentile NPV. That's the robust choice; maximising the mean
over-commits. Break-even utilisation = 1 − discount. All-upfront has the best discount but
can have lower NPV at a 10% cost of capital, because you pay earlier.

**FX?**
INR change = (ΔUSD × last month's effective rate) + (USD now × Δrate). This is exact by
construction. Effective rate = INR bill / USD bill with daily conversion, because spend isn't
uniform. The stress test covers deterministic shocks plus a joint Monte Carlo of spend and FX.

**Security?**
Read-only IAM, no root, SSO/credential chain only, gitleaks in pre-commit and CI. The API is
read-only with constant-time key comparison and refuses to start without keys. Containers run
as non-root with dropped capabilities. GitHub→AWS and GitHub→PyPI both use OIDC, so no
long-lived secrets are stored.

**What would you do next?**
Christoffersen test; an ensemble detector; CUR/FOCUS file ingestion via Athena for large orgs;
hourly data for Savings Plans; showback/chargeback by tags; a proper front end on the API.

**What was hardest / what did you learn?**
That a model that *looks* accurate (2% MAPE) can still have badly wrong risk bounds. Point
accuracy and calibration are different properties, and you only see the difference by
backtesting.

## Study map: learn it in this order
Ask in the Project chat "teach me X" for any of these:

| Topic | Where in the code |
|---|---|
| Packaging, uv, src layout, entry points | `pyproject.toml`, ADR 0002 |
| Protocols, dependency injection, ports & adapters | `core/ports.py`, ADR 0003 |
| DuckDB, columnar storage, migrations, idempotency | `storage/duckdb_repo.py` |
| IAM, STS, credential chain, OIDC | `providers/aws/session.py`, `docs/aws-setup.md`, `docs/deployment.md` |
| boto3 pagination, retries, Stubber vs moto | `providers/aws/cost_explorer.py`, `tests/aws/` |
| pydantic-settings, logging, exit codes | `config.py`, `logging_setup.py`, `cli.py` |
| Registry pattern | `core/waste/engine.py` |
| Robust statistics, STL, Isolation Forest, precision/recall | `core/analytics/anomaly.py` |
| ETS, Monte Carlo, VaR/ES, Kupiec, conformal | `core/analytics/forecast.py`, `risk.py` |
| Variance analysis, NPV, hedging, FX | `core/finance/` |
| FastAPI, auth, Streamlit | `interfaces/api.py`, `dashboard_app.py` |
| Docker multi-stage, CI/CD, Trusted Publishing | `Dockerfile`, `.github/workflows/` |
