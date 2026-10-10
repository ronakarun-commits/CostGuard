# 4. FOCUS-aligned schema, DuckDB storage, synthetic data first

Date: 2026-10-10 · Status: Accepted

## Decision
- **Grain:** one row per day × account × service × region × usage type (SKU). Totals are always
  aggregated from rows, never stored.
- **Schema:** field names follow the FinOps **FOCUS** spec (`billed_cost`, `sub_account_id`,
  `service_name`, `consumed_quantity`...). Quantity and cost are both kept so price and volume can
  be separated later.
- **Core models:** frozen `dataclass(slots=True)`, not Pydantic. The core is pure and processes
  ~10k+ rows; validation happens at the adapters.
- **Storage:** DuckDB (embedded, columnar). Forward-only numbered migrations in a `schema_version`
  table. `INSERT OR REPLACE` on the natural key makes every load idempotent.
- **Synthetic provider:** seeded, generates the whole "world" once; injects four anomaly shapes
  (spike, level shift, drift, price change) after a 60-day warm-up and stores them as ground truth.

## Alternatives
| Choice | Alternative | Why not |
|---|---|---|
| DuckDB | SQLite | Row store; slow for GROUP BY over dates; weak pandas integration |
| DuckDB | Parquet files only | No upsert, no SQL, no migrations |
| DuckDB | Postgres | Needs a server; overkill for a single-user CLI |
| dataclasses | Pydantic models | Validation overhead in the hot path; Pydantic used for config instead |
| FOCUS names | Raw Cost Explorer names | Vendor-specific; FOCUS lets Azure/GCP plug in later |

## Consequences
Anomaly detectors in Phase 5 can be scored with precision/recall against known labels — something
real AWS bills can never provide.
