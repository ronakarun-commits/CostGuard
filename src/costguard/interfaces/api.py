"""Read-only HTTP API (FastAPI). Install with ``pip install "costguard[api]"``.

Security model
* Every ``/v1`` route needs an ``X-API-Key`` header matching one of ``COSTGUARD_API_KEYS``.
  Keys are compared with ``hmac.compare_digest`` (constant time, so response timing does not leak
  how many characters matched). Multiple keys allow rotation without downtime.
* The app refuses to start without keys unless auth is explicitly disabled, and the CLI only
  allows disabling it on localhost.
* The database is opened **read-only** per request: the API can never modify cost history.
* Errors return JSON ``{"error", "hint"}``, never stack traces.

Every endpoint returns the same ``Report`` the CLI renders, as JSON (default), HTML or CSV.
"""

import hmac
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.security import APIKeyHeader

from costguard import __version__, services
from costguard.config import Settings, load_settings
from costguard.core.reports import Report
from costguard.errors import ConfigError, CostGuardError, NoDataError
from costguard.interfaces.render import to_csv, to_dict, to_html
from costguard.storage.duckdb_repo import DuckDBRepository, GroupBy

OutFormat = Literal["json", "html", "csv"]
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="costguard API key")


def create_app(settings: Settings | None = None, *, require_auth: bool = True) -> FastAPI:
    settings = settings or load_settings()
    keys = [k.get_secret_value().encode() for k in settings.api_keys if k.get_secret_value()]
    if require_auth and not keys:
        raise ConfigError(
            "No API keys configured.",
            "Set COSTGUARD_API_KEYS='[\"<long random key>\"]' (e.g. from "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"`).',
        )

    app = FastAPI(
        title="costguard",
        version=__version__,
        description="AWS FinOps and cost-risk API (read-only).",
    )

    def authorise(key: Annotated[str | None, Security(_key_header)]) -> None:
        if not require_auth:
            return
        supplied = (key or "").encode()
        # Compare against every key (no early exit) so timing doesn't reveal which matched.
        ok = False
        for k in keys:
            ok |= hmac.compare_digest(supplied, k)
        if not ok:
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key",
                                headers={"WWW-Authenticate": "APIKey"})  # fmt: skip

    def repo() -> Iterator[DuckDBRepository]:
        r = DuckDBRepository(settings.db_path, read_only=True)
        try:
            yield r
        finally:
            r.close()

    Repo = Annotated[DuckDBRepository, Depends(repo)]

    def display(currency: Literal["USD", "INR"] | None = None) -> services.Display:
        cur = currency or settings.reporting_currency
        return services.Display(cur, settings.usd_inr_fallback_rate if cur == "INR" else 1.0)

    Disp = Annotated[services.Display, Depends(display)]
    Fmt = Annotated[OutFormat, Query(alias="format")]

    @app.exception_handler(CostGuardError)
    async def _handle(_: Request, exc: CostGuardError) -> JSONResponse:
        status = 404 if isinstance(exc, NoDataError) else 400
        return JSONResponse({"error": str(exc), "hint": exc.hint}, status_code=status)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """Liveness probe (no auth, no database access)."""
        return {"status": "ok", "version": __version__}

    v1 = [Depends(authorise)]

    @app.get("/v1/summary", dependencies=v1, tags=["costs"])
    def summary(repo: Repo, disp: Disp, by: GroupBy = "service",
                days: Annotated[int, Query(ge=1, le=3650)] = 30) -> dict[str, object]:  # fmt: skip
        """Total cost grouped by a dimension over the last N days of data."""
        from datetime import timedelta

        from costguard.core.models import DateRange

        lo, hi = services.require_data(repo)
        start = max(lo, hi - timedelta(days=days - 1))
        df = repo.summary(by, DateRange(start, hi + timedelta(days=1)))
        return {
            "by": by, "currency": disp.currency, "period": [str(start), str(hi)],
            "rows": [{"key": str(k), "cost": float(c) * disp.fx}
                     for k, c in zip(df["key"], df["cost"], strict=True)],
        }  # fmt: skip

    @app.get("/v1/dashboard", dependencies=v1, tags=["reports"])
    def dashboard(repo: Repo, disp: Disp, budget: float | None = None,
                  fmt: Fmt = "json") -> Response:  # fmt: skip
        return _out(services.dashboard(repo, disp, budget or settings.budget_monthly_usd), fmt)

    @app.get("/v1/trend", dependencies=v1, tags=["reports"])
    def trend(
        repo: Repo,
        disp: Disp,
        by: GroupBy = "service",
        freq: Literal["day", "week", "month"] = "month",
        top: Annotated[int, Query(ge=1, le=20)] = 5,
        fmt: Fmt = "json",
    ) -> Response:
        return _out(services.trend(repo, disp, by=by, freq=freq, top=top), fmt)

    @app.get("/v1/anomalies", dependencies=v1, tags=["analytics"])
    def anomalies(
        repo: Repo,
        disp: Disp,
        method: Literal["baseline", "stl", "iforest"] = "baseline",
        days: Annotated[int, Query(ge=1, le=3650)] = 30,
        fmt: Fmt = "json",
    ) -> Response:
        return _out(services.anomalies(repo, disp, method=method, last_days=days), fmt)

    @app.get("/v1/forecast", dependencies=v1, tags=["analytics"])
    def forecast(repo: Repo, disp: Disp, days: Annotated[int, Query(ge=1, le=180)] = 30,
                 fmt: Fmt = "json") -> Response:  # fmt: skip
        return _out(services.forecast(repo, disp, days=days, paths=1000), fmt)

    @app.get("/v1/risk", dependencies=v1, tags=["analytics"])
    def risk(repo: Repo, disp: Disp, budget: float | None = None,
             level: Annotated[float, Query(ge=0.8, le=0.995)] = 0.95,
             fmt: Fmt = "json") -> Response:  # fmt: skip
        return _out(services.risk(repo, disp, budget=budget or settings.budget_monthly_usd,
                                  level=level, paths=3000), fmt)  # fmt: skip

    @app.get("/v1/variance", dependencies=v1, tags=["finance"])
    def variance(repo: Repo, disp: Disp, current: str | None = None, base: str | None = None,
                 fmt: Fmt = "json") -> Response:  # fmt: skip
        outcome = services.variance(repo, disp, current=current, base=base,
                                    budget=settings.budget_monthly_usd)  # fmt: skip
        return _out(outcome.report, fmt)

    @app.get("/v1/audit", dependencies=v1, tags=["waste"])
    def audit(disp: Disp, min_saving: Annotated[float, Query(ge=0)] = 0.0,
              fmt: Fmt = "json") -> Response:  # fmt: skip
        """Waste audit on the synthetic demo inventory (live AWS audits run from the CLI)."""
        from costguard.providers.synthetic_inventory import synthetic_inventory

        report, _ = services.audit(synthetic_inventory(), disp, min_saving=min_saving)
        return _out(report, fmt)

    return app


def _out(report: Report, fmt: OutFormat) -> Response:
    if fmt == "html":
        return HTMLResponse(to_html(report))
    if fmt == "csv":
        return PlainTextResponse(to_csv(report), media_type="text/csv")
    return JSONResponse(to_dict(report))
