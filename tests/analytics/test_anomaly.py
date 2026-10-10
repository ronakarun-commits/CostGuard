from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from costguard.core.analytics.anomaly import (
    METHODS,
    DetectorConfig,
    Incident,
    detect,
    evaluate,
    score_series,
    series_by_account_service,
)
from costguard.core.models import AnomalyKind, InjectedAnomaly
from costguard.providers.synthetic import SyntheticProvider


@pytest.mark.parametrize("method", METHODS)
def test_spike_is_flagged(smooth: pd.Series, method: str) -> None:
    s = smooth.copy()
    s.iloc[150] *= 5
    out = score_series(s, method)  # type: ignore[arg-type]
    assert out["flag"].iloc[150]
    assert out["flag"].sum() <= 4
    assert out["expected"].iloc[150] == pytest.approx(smooth.iloc[150], rel=0.1)


def test_no_flags_during_warmup_or_on_drops(smooth: pd.Series) -> None:
    s = smooth.copy()
    s.iloc[5] *= 10
    s.iloc[150] *= 0.1
    out = score_series(s, "baseline")
    assert not out["flag"].iloc[:28].any()
    assert not out["flag"].iloc[150]


def test_materiality_filter(smooth: pd.Series) -> None:
    s = smooth / 100  # ~$1/day service: a 5x spike is only ~$4
    s.iloc[150] *= 5
    assert not score_series(s, "baseline")["flag"].any()
    assert score_series(s, "baseline", DetectorConfig(min_excess=1))["flag"].iloc[150]


def test_short_series(smooth: pd.Series) -> None:
    assert not score_series(smooth.iloc[:10], "stl")["flag"].any()
    assert not score_series(smooth.iloc[:20], "iforest")["flag"].any()


def test_series_split_and_empty(world: tuple[SyntheticProvider, pd.DataFrame, pd.Series]) -> None:
    _, costs, _ = world
    series = series_by_account_service(costs)
    from costguard.providers.synthetic import ENV_SCALE

    assert len(series) == sum(len(v) for v in ENV_SCALE.values())
    assert all(len(s) == 365 for s in series.values())
    assert series_by_account_service(costs.iloc[0:0]) == {}


def test_baseline_meets_quality_bar(
    world: tuple[SyntheticProvider, pd.DataFrame, pd.Series],
) -> None:
    p, costs, _ = world
    labels = p.get_anomalies(p.world)
    ev = evaluate(detect(costs, "baseline"), labels, "baseline")
    # Regression guard for the detector: if a change makes it worse, this fails.
    assert ev.recall >= 0.75
    assert ev.precision >= 0.85
    assert ev.f1 >= 0.8
    assert ev.mean_delay_days is not None
    assert ev.mean_delay_days <= 2


def test_incidents_merge_consecutive_days(
    world: tuple[SyntheticProvider, pd.DataFrame, pd.Series],
) -> None:
    _, costs, _ = world
    incidents = detect(costs, "baseline")
    assert incidents == sorted(incidents, key=lambda i: (-i.excess_cost, i.start_date))
    multi = [i for i in incidents if i.end_date > i.start_date]
    assert multi  # level shifts produce multi-day incidents
    since = detect(costs, "baseline", since=date(2026, 9, 1))
    assert all(i.end_date >= date(2026, 9, 1) for i in since)


def _inc(start: int, end: int, svc: str = "s") -> Incident:
    return Incident("m", "a", "a", svc, date(2026, 1, start), date(2026, 1, end), 5, 10, 2)


def test_evaluate_counts() -> None:
    jan = lambda d: date(2026, 1, d)  # noqa: E731
    labels = [
        InjectedAnomaly("A1", AnomalyKind.SPIKE, "a", "s", jan(10), jan(10), 3, ""),
        InjectedAnomaly("A2", AnomalyKind.DRIFT, "a", "s", jan(20), jan(25), 0.05, ""),
    ]
    ev = evaluate([_inc(12, 13), _inc(1, 2), _inc(10, 10, "other")], labels, "m")
    assert (ev.detected, ev.incidents, ev.true_incidents) == (1, 3, 1)
    assert ev.recall == 0.5
    assert ev.precision == pytest.approx(1 / 3)
    assert ev.mean_delay_days == 2
    assert ev.recall_by_kind == {"drift": 0.0, "spike": 1.0}
    empty = evaluate([], [], "m")
    assert (empty.recall, empty.precision, empty.f1, empty.mean_delay_days) == (0, 0, 0, None)


def test_scores_are_finite_where_defined(smooth: pd.Series) -> None:
    out = score_series(smooth, "baseline")
    # 28 days for the same-weekday median + 28 for the rolling MAD of its residuals
    assert out["score"].iloc[:56].isna().all()
    assert np.isfinite(out["score"].iloc[62:]).all()
