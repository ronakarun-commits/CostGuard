"""Cost anomaly detection on daily (account x service) series.

Three detectors with deliberately different trade-offs:

``baseline``  Same-weekday median of the previous 4 weeks, robust z-score of the log ratio.
              **Causal** (uses only past data), so it is what you would run every morning.
              Simple, explainable ("3.2x the usual Tuesday"), but slow to adapt after a
              level shift and blind to slow drift until it is large.
``stl``       STL decomposition (trend + weekly seasonality + residual, robust LOESS) on log cost;
              flags large residuals. **Retrospective**: STL is fitted on the whole series, so it
              uses future data. Excellent for investigating history, not for live alerting.
``iforest``   Isolation Forest on causal features (log ratio vs same-weekday median, vs 7-day mean,
              day-over-day change, weekday). Multivariate and catches odd *shapes*, but scores
              are not interpretable and it needs a contamination guess.

All work on ``log(cost)``: spend is multiplicative (a 2x jump matters equally at $10 and $1000),
and logs make weekly patterns additive. Robust statistics (median, MAD) are used everywhere, so
past anomalies don't inflate the baseline and hide future ones.

A flagged day must also matter in money: ``min_excess`` USD/day above expected (default $10) and
at least 20% above expected — otherwise tiny services produce endless noise alerts.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from costguard.core.analytics._series import dt_index, expm1, log1p
from costguard.core.models import InjectedAnomaly

Method = Literal["baseline", "stl", "iforest"]
METHODS: tuple[Method, ...] = ("baseline", "stl", "iforest")

WARMUP = 28
MAD_TO_SIGMA = 1.4826  # MAD * 1.4826 ~= standard deviation for normal data


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    threshold: float = 4.0  # robust z-score
    min_excess: float = 10.0  # USD/day above expected
    min_ratio: float = 1.2  # actual / expected
    contamination: float = 0.03  # Isolation Forest only
    seed: int = 0


def _same_weekday_median(x: pd.Series) -> pd.Series:
    lags = pd.concat([x.shift(7 * k) for k in range(1, 5)], axis=1)
    return lags.median(axis=1)


def _robust_z(resid: pd.Series, window: int | None = 56) -> pd.Series:
    """z = resid / (1.4826 * MAD). Rolling (causal, past-only) or global."""
    if window is None:
        center = float(resid.median())
        spread = float((resid - center).abs().median())
        return (resid - center) / max(MAD_TO_SIGMA * spread, 0.02)
    past = resid.shift(1)
    med = past.rolling(window, min_periods=WARMUP).median()
    mad = (past - med).abs().rolling(window, min_periods=WARMUP).median()
    return (resid - med) / (MAD_TO_SIGMA * mad).clip(lower=0.02)


def score_series(x: pd.Series, method: Method, cfg: DetectorConfig | None = None) -> pd.DataFrame:
    """Score one daily cost series. Returns columns: actual, expected, score, flag."""
    cfg = cfg or DetectorConfig()
    x = x.astype(float).asfreq("D", fill_value=0.0)
    logx = log1p(x)
    expected = expm1(_same_weekday_median(logx))

    if method == "baseline":
        score = _robust_z(logx - log1p(expected))
    elif method == "stl":
        from statsmodels.tsa.seasonal import STL

        if len(x) < 2 * 7 + 1:
            score = pd.Series(np.nan, index=x.index)
        else:
            res = STL(logx, period=7, robust=True).fit()
            expected = expm1(pd.Series(res.trend + res.seasonal, index=x.index))
            score = _robust_z(pd.Series(res.resid, index=x.index), window=None)
            score.iloc[:WARMUP] = np.nan
    elif method == "iforest":
        score = _iforest_scores(logx, expected, cfg)
    else:  # pragma: no cover - guarded by Literal
        msg = f"unknown method {method!r}"
        raise ValueError(msg)

    out = pd.DataFrame({"actual": x, "expected": expected, "score": score})
    out["excess"] = out["actual"] - out["expected"]
    out["flag"] = (
        (out["score"] > cfg.threshold)
        & (out["excess"] >= cfg.min_excess)
        & (out["actual"] >= cfg.min_ratio * out["expected"])
    ).fillna(False)
    out.loc[out.index[:WARMUP], "flag"] = False
    return out


def _iforest_scores(logx: pd.Series, expected: pd.Series, cfg: DetectorConfig) -> pd.Series:
    from sklearn.ensemble import IsolationForest

    feats = pd.DataFrame(
        {
            "vs_weekday": logx - log1p(expected),
            "vs_week": logx - logx.shift(1).rolling(7).mean(),
            "dod": logx.diff(),
            "dow_sin": np.sin(2 * np.pi * dt_index(logx).dayofweek / 7),
            "dow_cos": np.cos(2 * np.pi * dt_index(logx).dayofweek / 7),
        }
    )
    valid = feats.dropna()
    score = pd.Series(np.nan, index=logx.index)
    if len(valid) < WARMUP:
        return score
    model = IsolationForest(
        n_estimators=200, contamination=cfg.contamination, random_state=cfg.seed
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        model.fit(valid)
    raw = -model.score_samples(valid)  # higher = more anomalous
    threshold = -model.offset_
    # Rescale so the model's own decision boundary lands on cfg.threshold, and only increases
    # count (a sudden *drop* in spend is good news for a cost tool).
    scaled = raw / threshold * cfg.threshold
    scaled = np.where(valid["vs_weekday"] > 0, scaled, 0.0)
    score.loc[valid.index] = scaled
    return score


# ---- whole-organisation detection ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Incident:
    """Consecutive flagged days for one series, merged."""

    method: str
    sub_account_id: str
    sub_account_name: str
    service_name: str
    start_date: date
    end_date: date
    peak_score: float
    excess_cost: float  # sum of (actual - expected) over flagged days
    peak_ratio: float  # max actual / expected


def series_by_account_service(costs: pd.DataFrame) -> dict[tuple[str, str, str], pd.Series]:
    """Long cost frame -> {(account_id, account_name, service): daily series}."""
    if costs.empty:
        return {}
    grouped = costs.groupby(["sub_account_id", "sub_account_name", "service_name", "charge_date"])[
        "billed_cost"
    ].sum()
    start, end = costs["charge_date"].min(), costs["charge_date"].max()
    full = pd.date_range(start, end, freq="D")
    out: dict[tuple[str, str, str], pd.Series] = {}
    for key, s in grouped.groupby(level=[0, 1, 2]):
        acct_id, acct_name, service = (str(k) for k in tuple(key))  # type: ignore[arg-type]
        series = s.droplevel([0, 1, 2]).reindex(full, fill_value=0.0)
        series.index.name = "date"
        out[(acct_id, acct_name, service)] = series
    return out


def detect(
    costs: pd.DataFrame,
    method: Method = "baseline",
    cfg: DetectorConfig | None = None,
    *,
    since: date | None = None,
    max_gap_days: int = 1,
) -> list[Incident]:
    """Run a detector over every account x service series and merge flagged days into incidents."""
    incidents: list[Incident] = []
    for (acct_id, acct_name, service), series in series_by_account_service(costs).items():
        scored = score_series(series, method, cfg)
        flagged = scored[scored["flag"]]
        if since is not None:
            flagged = flagged[flagged.index >= pd.Timestamp(since)]
        for chunk in _runs(dt_index(flagged), max_gap_days):
            rows = scored.loc[chunk]
            rows = rows[rows["flag"]]
            incidents.append(
                Incident(
                    method=method,
                    sub_account_id=acct_id,
                    sub_account_name=acct_name,
                    service_name=service,
                    start_date=chunk[0].date(),
                    end_date=chunk[-1].date(),
                    peak_score=float(rows["score"].max()),
                    excess_cost=float(rows["excess"].sum()),
                    peak_ratio=float((rows["actual"] / rows["expected"].clip(lower=1e-9)).max()),
                )
            )
    return sorted(incidents, key=lambda i: (-i.excess_cost, i.start_date))


def _runs(days: pd.DatetimeIndex, max_gap: int) -> Iterable[pd.DatetimeIndex]:
    if len(days) == 0:
        return
    start = 0
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days > max_gap + 1:
            yield pd.date_range(days[start], days[i - 1])
            start = i
    yield pd.date_range(days[start], days[-1])


# ---- evaluation against ground truth -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evaluation:
    method: str
    labels: int
    detected: int
    incidents: int
    true_incidents: int
    mean_delay_days: float | None
    recall_by_kind: dict[str, float]

    @property
    def recall(self) -> float:
        return self.detected / self.labels if self.labels else 0.0

    @property
    def precision(self) -> float:
        return self.true_incidents / self.incidents if self.incidents else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def evaluate(
    incidents: Sequence[Incident],
    labels: Sequence[InjectedAnomaly],
    method: str,
    *,
    tolerance_days: int = 3,
) -> Evaluation:
    """Event-level scoring: a label is detected if an incident on the same series overlaps
    [start, end + tolerance]; an incident is a true positive if it overlaps any label window."""

    def window(a: InjectedAnomaly) -> tuple[date, date]:
        return a.start_date, a.end_date + timedelta(days=tolerance_days)

    def overlaps(i: Incident, a: InjectedAnomaly) -> bool:
        lo, hi = window(a)
        same = (i.sub_account_id, i.service_name) == (a.sub_account_id, a.service_name)
        return same and i.start_date <= hi and i.end_date >= lo

    detected, delays = 0, []
    by_kind: dict[str, list[bool]] = {}
    for a in labels:
        hits = [i for i in incidents if overlaps(i, a)]
        found = bool(hits)
        detected += found
        by_kind.setdefault(a.kind.value, []).append(found)
        if found:
            first = min(max(i.start_date, a.start_date) for i in hits)
            delays.append((first - a.start_date).days)
    true_inc = sum(any(overlaps(i, a) for a in labels) for i in incidents)
    return Evaluation(
        method=method,
        labels=len(labels),
        detected=detected,
        incidents=len(incidents),
        true_incidents=true_inc,
        mean_delay_days=float(np.mean(delays)) if delays else None,
        recall_by_kind={k: sum(v) / len(v) for k, v in sorted(by_kind.items())},
    )
