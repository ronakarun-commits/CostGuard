# 9. Finance layer: price-volume variance, commitment NPV, FX exposure

Date: 2026-10-10 · Status: Accepted

## Decision
- **Variance:** standard FP&A price-volume bridge per line item (service × usage type × unit), with
  a **calendar effect** split out of volume (31 vs 28 days). Effects sum exactly to the change.
  Mixed-unit rows (service-level AWS pulls) are reported as unattributed with a hint.
- **Savings Plans:** commitment as a hedge. Monthly usage simulated as a log-normal random walk
  (drift/vol from history) with the observed daily shape; coverage via a pre-tabulated curve
  E[min(r, x)]. Each term/payment option is optimised over commitment level for 5th-percentile
  NPV (robust) or mean NPV, at a chosen cost of capital. Outputs break-even utilisation, hedge
  ratio, P(loss).
- **FX:** USD bill in INR using ECB reference rates (Frankfurter API, cached in DuckDB; synthetic
  fallback offline). Month-on-month INR change = usage effect at last month's effective rate +
  FX effect. Stress: deterministic depreciation shocks and a joint spend × FX Monte Carlo CaR.

## Alternatives
| Choice | Alternative | Why not |
|---|---|---|
| Robust (p5) objective | Max expected NPV | Risk-neutral sizing over-commits; a CFO cares about the bad case |
| Own simulation | AWS SP purchase recommendations | AWS recommends from the last 7/30/60 days, with no growth or volatility view and no NPV of payment options |
| ECB via Frankfurter | RBI reference rate | RBI publishes no free API; ECB USD/INR tracks it closely enough for exposure analysis |
| Daily conversion | Month-average rate | Spend isn't uniform; daily conversion is what actually hits the INR books |
