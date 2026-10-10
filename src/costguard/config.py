"""Settings, resolved in this order (first wins):

1. CLI flags (applied by the CLI on top of the loaded settings)
2. Environment variables ``COSTGUARD_*`` (e.g. ``COSTGUARD_BUDGET_MONTHLY_USD=25000``)
3. A ``.env`` file in the working directory
4. ``costguard.toml`` in the working directory
5. The defaults below

pydantic-settings validates types once, at startup, so a typo like ``budget_monthly_usd = "lots"``
fails immediately with a clear message instead of deep inside an analysis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
    TomlConfigSettingsSource,
)

from costguard.errors import ConfigError

CONFIG_FILE = "costguard.toml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COSTGUARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        toml_file=CONFIG_FILE,
    )

    db_path: Path = Path(".costguard") / "costguard.duckdb"

    # Budget & reporting
    budget_monthly_usd: float | None = Field(default=None, gt=0)
    reporting_currency: Literal["USD", "INR"] = "USD"
    usd_inr_fallback_rate: float = Field(default=88.0, gt=0)

    # AWS
    aws_profile: str | None = None
    # NoDecode: accept "us-east-1,ap-south-1" from env vars instead of requiring a JSON list
    aws_regions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ap-south-1", "us-east-1"]
    )

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    log_format: Literal["console", "json"] = "console"

    # Interfaces (Phase 7). SecretStr never prints its value in logs or reprs.
    slack_webhook_url: SecretStr | None = None
    api_keys: list[SecretStr] = Field(default_factory=list)

    @field_validator("aws_regions", mode="before")
    @classmethod
    def _split_regions(cls, v: object) -> object:
        return [r.strip() for r in v.split(",") if r.strip()] if isinstance(v, str) else v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_settings(**overrides: object) -> Settings:
    try:
        return Settings(**{k: v for k, v in overrides.items() if v is not None})  # type: ignore[arg-type]
    except SettingsError as exc:
        raise ConfigError(f"Invalid configuration: {exc}", "Check COSTGUARD_* variables.") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(
            f"Invalid configuration: {problems}",
            f"Check {CONFIG_FILE}, .env and COSTGUARD_* environment variables.",
        ) from exc
