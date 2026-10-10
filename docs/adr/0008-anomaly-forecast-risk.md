# 8. Anomaly detection, probabilistic forecasting and Cost-at-Risk

Date: 2026-10-10 · Status: Accepted · Evidence: [`../evaluation.md`](../evaluation.md)

## Decision
- **Detectors:** three, compared on labelled synthetic data — causal same-weekday baseline
  (default), STL (retrospective), Isolation Forest. All on log cost with robust statistics and a
  money materiality filter ($10/day, +20%). Flagged days merge into incidents.
- **Forecast:** Monte Carlo paths from ETS(A,Ad,A) on log cost (statsmodels), trained on the last
  180 days with outliers cleaned; seasonal-naive bootstrap as the benchmark.
- **Risk:** Cost-at-Risk, Expected Shortfall and breach probability are statistics of simulated
  month-end totals (MTD actual + simulated rest of month).
- **Calibration:** split-conformal widening of the bounds, learned from the model's past errors,
  strictly out-of-sample. Backtested daily with the **Kupiec POF** test and monthly with interval
  coverage.

## Alternatives
| Choice | Alternative | Why not |
|---|---|---|
| ETS | Prophet | Heavy dependency (cmdstan); weaker on short daily series; less transparent |
| ETS | ARIMA/SARIMAX | Similar accuracy here; ETS state-space simulation is simpler |
| ETS | Gradient boosting / LSTM | One year of daily data per org is too little; no native uncertainty |
| Monte Carlo | Analytic intervals | Month-end *sum* of log-normal days has no closed form; paths make CaR/ES/breach probability consistent |
| Conformal widening | Student-t / jump-diffusion errors | Tried a jump overlay: did not fix calibration and adds parameters to guess |
| Kupiec POF | Christoffersen independence test | Kupiec answers "how often"; Christoffersen ("are breaches clustered?") is a natural next step — they are clustered, around regime changes |
| Baseline as default | AWS Cost Anomaly Detection | Free and managed, but a black box with no labels to evaluate it; costguard's detector is explainable and measurable |
