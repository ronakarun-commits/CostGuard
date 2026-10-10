# 2. Tooling: uv, src layout, hatchling, ruff, mypy strict, pytest

Date: 2026-10-10 · Status: Accepted

## Decision
| Concern | Choice | Alternatives considered | Why |
|---|---|---|---|
| Env + deps + lockfile | **uv** | pip + venv, Poetry, PDM | One fast tool; cross-platform `uv.lock`; manages Python versions too |
| Layout | **src/** | flat | Tests run against the *installed* package, so packaging bugs surface early |
| Build backend | **hatchling** | setuptools, uv_build, poetry-core | Standard, zero-config, PEP 621 metadata |
| Lint + format | **ruff** | flake8 + black + isort | One tool, very fast, includes bandit (`S`) security rules |
| Types | **mypy --strict** | pyright | Industry default for Python libraries; strict from day 1 is cheaper than retrofitting |
| Tests | **pytest + pytest-cov** (fail under 80%) | unittest | Fixtures, parametrize, plugin ecosystem (moto later) |
| CLI | **Typer** | argparse, Click | Type-hint driven, built on Click, pairs with Rich |
| Version | read from package metadata (`importlib.metadata`) | hard-coded `__version__` | Single source of truth in `pyproject.toml` |

## Consequences
- Contributors need uv installed.
- Strict mypy will slow early prototyping slightly; `boto3` will need `boto3-stubs` in Phase 2.
