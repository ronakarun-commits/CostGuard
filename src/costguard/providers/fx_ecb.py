"""ECB reference exchange rates via the free, keyless Frankfurter API (https://frankfurter.dev).

ECB publishes one rate per business day around 16:00 CET; weekends/holidays are forward-filled
by the analytics. Uses only the standard library (urllib), so no extra dependency.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import date

import pandas as pd

from costguard.errors import CostGuardError

log = logging.getLogger(__name__)

API = "https://api.frankfurter.dev/v1"


class FxError(CostGuardError):
    exit_code = 6


def fetch_rates(start: date, end: date, base: str = "USD", quote: str = "INR",
                timeout: float = 20.0) -> pd.Series:  # fmt: skip
    url = f"{API}/{start.isoformat()}..{end.isoformat()}?base={base}&symbols={quote}"
    if not url.startswith("https://"):  # pragma: no cover - constant prefix
        msg = "refusing non-HTTPS URL"
        raise FxError(msg)
    req = urllib.request.Request(url, headers={"User-Agent": "costguard"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise FxError(
            f"Could not download {base}/{quote} rates: {exc}",
            "Check your internet connection, or use `--fx-source synthetic` for offline demos.",
        ) from exc
    rates = {d: v[quote] for d, v in payload.get("rates", {}).items() if quote in v}
    if not rates:
        raise FxError(f"No {base}/{quote} rates returned for {start}..{end}.")
    series = pd.Series(rates, dtype=float)
    series.index = pd.to_datetime(series.index)
    log.info("fetched fx rates", extra={"pair": f"{base}/{quote}", "days": len(series)})
    return series.sort_index()
