"""DuckDB-backed cost history.

Why DuckDB: it is an embedded (in-process, single-file) *columnar* analytics database. Cost data is
append-mostly and queried with big GROUP BYs over dates, which is exactly what columnar engines are
fast at. SQLite is row-oriented (great for transactions, slower for aggregations); Parquet files
alone have no upsert, schema migrations or SQL. DuckDB also reads/writes pandas and Parquet
natively, which Phase 5 uses heavily.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

import duckdb
import pandas as pd

from costguard.core.models import AnomalyKind, CostRecord, DateRange, InjectedAnomaly

GroupBy = Literal["service", "account", "region", "category", "date", "month"]

# Allow-list mapping from user-facing names to SQL expressions. Never interpolate user input.
_GROUP_SQL: Final[dict[str, str]] = {
    "service": "service_name",
    "account": "sub_account_name",
    "region": "region_id",
    "category": "service_category",
    "date": "charge_date",
    "month": "strftime(charge_date, '%Y-%m')",
}

_MIGRATIONS: Final[list[str]] = [
    # v1: cost history + ground-truth anomalies
    """
    CREATE TABLE cost_records (
        charge_date        DATE    NOT NULL,
        sub_account_id     VARCHAR NOT NULL,
        sub_account_name   VARCHAR NOT NULL,
        service_name       VARCHAR NOT NULL,
        service_category   VARCHAR NOT NULL,
        region_id          VARCHAR NOT NULL,
        sku_id             VARCHAR NOT NULL,
        consumed_quantity  DOUBLE  NOT NULL,
        consumed_unit      VARCHAR NOT NULL,
        billed_cost        DOUBLE  NOT NULL,
        billing_currency   VARCHAR NOT NULL,
        tags               VARCHAR NOT NULL,   -- JSON object
        source             VARCHAR NOT NULL,
        loaded_at          TIMESTAMP NOT NULL DEFAULT current_timestamp,
        PRIMARY KEY (charge_date, sub_account_id, service_name, region_id, sku_id, source)
    );
    CREATE TABLE injected_anomalies (
        id             VARCHAR PRIMARY KEY,
        kind           VARCHAR NOT NULL,
        sub_account_id VARCHAR NOT NULL,
        service_name   VARCHAR NOT NULL,
        start_date     DATE    NOT NULL,
        end_date       DATE    NOT NULL,
        magnitude      DOUBLE  NOT NULL,
        description    VARCHAR NOT NULL
    );
    """,
    # v2: cache of paid API calls (Cost Explorer is $0.01 per request)
    """
    CREATE TABLE api_cache (
        key        VARCHAR PRIMARY KEY,
        operation  VARCHAR NOT NULL,
        response   VARCHAR NOT NULL,   -- JSON
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
    );
    """,
]

_COST_COLUMNS: Final = [
    "charge_date", "sub_account_id", "sub_account_name", "service_name", "service_category",
    "region_id", "sku_id", "consumed_quantity", "consumed_unit", "billed_cost",
    "billing_currency", "tags", "source",
]  # fmt: skip


class DuckDBRepository:
    """Implements the ``CostRepository`` port. Use as a context manager or call ``close()``."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(self.path)
        self._migrate()

    # ---- lifecycle -----------------------------------------------------------------------------

    def __enter__(self) -> DuckDBRepository:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    def _migrate(self) -> None:
        """Tiny forward-only migration runner, versioned by a single-row table."""
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = int(row[0]) if row and row[0] is not None else 0
        for version, sql in enumerate(_MIGRATIONS[current:], start=current + 1):
            self.con.execute("BEGIN")
            self.con.execute(sql)
            self.con.execute("INSERT INTO schema_version VALUES (?)", [version])
            self.con.execute("COMMIT")

    @property
    def schema_version(self) -> int:
        row = self.con.execute("SELECT max(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # ---- writes --------------------------------------------------------------------------------

    def upsert_costs(self, records: Iterable[CostRecord], source: str) -> int:
        rows = [
            (
                r.charge_date, r.sub_account_id, r.sub_account_name, r.service_name,
                r.service_category, r.region_id, r.sku_id, r.consumed_quantity, r.consumed_unit,
                r.billed_cost, r.billing_currency, json.dumps(r.tags, sort_keys=True), source,
            )
            for r in records
        ]  # fmt: skip
        if not rows:
            return 0
        frame = pd.DataFrame(rows, columns=_COST_COLUMNS)
        self.con.register("incoming", frame)
        try:
            cols = ", ".join(_COST_COLUMNS)
            self.con.execute(
                f"INSERT OR REPLACE INTO cost_records ({cols}) SELECT {cols} FROM incoming"
            )
        finally:
            self.con.unregister("incoming")
        return len(rows)

    def upsert_anomalies(self, anomalies: Iterable[InjectedAnomaly]) -> int:
        rows = [
            (a.id, a.kind.value, a.sub_account_id, a.service_name, a.start_date, a.end_date,
             a.magnitude, a.description)
            for a in anomalies
        ]  # fmt: skip
        if rows:
            self.con.executemany(
                "INSERT OR REPLACE INTO injected_anomalies VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
        return len(rows)

    def delete_source(self, source: str) -> None:
        self.con.execute("DELETE FROM cost_records WHERE source = ?", [source])
        if source == "synthetic":
            self.con.execute("DELETE FROM injected_anomalies")

    # ---- reads ---------------------------------------------------------------------------------

    @staticmethod
    def _where(period: DateRange | None, extra: str = "") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if period is not None:
            clauses.append("charge_date >= ? AND charge_date < ?")
            params += [period.start, period.end]
        if extra:
            clauses.append(extra)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    def get_costs(self, period: DateRange | None = None) -> Sequence[CostRecord]:
        where, params = self._where(period)
        rows = self.con.execute(
            f"SELECT {', '.join(_COST_COLUMNS[:-1])} FROM cost_records {where} "
            "ORDER BY charge_date, sub_account_id, service_name",
            params,
        ).fetchall()
        return [
            CostRecord(
                charge_date=r[0], sub_account_id=r[1], sub_account_name=r[2], service_name=r[3],
                service_category=r[4], region_id=r[5], sku_id=r[6], consumed_quantity=r[7],
                consumed_unit=r[8], billed_cost=r[9], billing_currency=r[10],
                tags=json.loads(r[11]),
            )
            for r in rows
        ]  # fmt: skip

    def get_anomalies(self) -> Sequence[InjectedAnomaly]:
        rows = self.con.execute("SELECT * FROM injected_anomalies ORDER BY start_date").fetchall()
        return [
            InjectedAnomaly(r[0], AnomalyKind(r[1]), r[2], r[3], r[4], r[5], r[6], r[7])
            for r in rows
        ]

    def costs_frame(self, period: DateRange | None = None) -> pd.DataFrame:
        """All records as a DataFrame (for analytics)."""
        where, params = self._where(period)
        df = self.con.execute(
            f"SELECT * EXCLUDE (loaded_at) FROM cost_records {where}",
            params,
        ).df()
        if not df.empty:
            df["charge_date"] = pd.to_datetime(df["charge_date"])
        return df

    def date_bounds(self) -> tuple[date, date] | None:
        row = self.con.execute(
            "SELECT min(charge_date), max(charge_date) FROM cost_records"
        ).fetchone()
        if not row or row[0] is None:
            return None
        return row[0], row[1]

    def summary(self, by: GroupBy, period: DateRange | None = None) -> pd.DataFrame:
        """Total cost grouped by one dimension, largest first (dates/months ordered by time)."""
        if by not in _GROUP_SQL:
            msg = f"cannot group by {by!r}; choose from {sorted(_GROUP_SQL)}"
            raise ValueError(msg)
        expr = _GROUP_SQL[by]
        where, params = self._where(period)
        order = "key" if by in ("date", "month") else "cost DESC"
        return self.con.execute(
            f"SELECT {expr} AS key, sum(billed_cost) AS cost, count(*) AS records "
            f"FROM cost_records {where} GROUP BY key ORDER BY {order}",
            params,
        ).df()

    def daily_series(
        self, period: DateRange | None = None, by: GroupBy | None = None
    ) -> pd.DataFrame:
        """Daily cost, optionally pivoted wide by a dimension (one column per key)."""
        where, params = self._where(period)
        if by is None:
            df = self.con.execute(
                f"SELECT charge_date, sum(billed_cost) AS cost FROM cost_records {where} "
                "GROUP BY charge_date ORDER BY charge_date",
                params,
            ).df()
            return df.set_index(pd.to_datetime(df["charge_date"]))[["cost"]]
        if by not in _GROUP_SQL:
            msg = f"cannot group by {by!r}"
            raise ValueError(msg)
        df = self.con.execute(
            f"SELECT charge_date, {_GROUP_SQL[by]} AS key, sum(billed_cost) AS cost "
            f"FROM cost_records {where} GROUP BY ALL ORDER BY charge_date",
            params,
        ).df()
        wide = df.pivot_table(index="charge_date", columns="key", values="cost", fill_value=0.0)
        wide.index = pd.to_datetime(wide.index)
        return wide

    # ---- API cache (used by the AWS provider) --------------------------------------------------

    def cache_get(self, key: str) -> str | None:
        row = self.con.execute("SELECT response FROM api_cache WHERE key = ?", [key]).fetchone()
        return str(row[0]) if row else None

    def cache_put(self, key: str, operation: str, response: str) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO api_cache (key, operation, response) VALUES (?, ?, ?)",
            [key, operation, response],
        )
