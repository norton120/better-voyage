from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///./data/better-voyage.db"
    cache_dir: Path = Field(default=Path("./data/cache"))

    open_meteo_marine_base_url: str = "https://marine-api.open-meteo.com/v1"
    open_meteo_forecast_base_url: str = "https://api.open-meteo.com/v1"
    noaa_tides_base_url: str = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    noaa_metadata_base_url: str = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"

    http_timeout_s: float = 15.0
    http_retries: int = 3
    http_user_agent: str = "better-voyage/0.1 (+https://github.com/thekad/better-voyage)"

    forecast_cache_ttl_s: int = 60 * 60 * 3          # 3h for marine forecast
    tide_cache_ttl_s: int = 60 * 60 * 24             # 24h for tide predictions
    stations_cache_ttl_s: int = 60 * 60 * 24 * 30    # 30d for station metadata

    # Async jobs (plan/16-jobs-async.md)
    max_concurrent_jobs: int = 2
    progress_min_pct_delta: float = 0.05
    progress_min_interval_s: float = 2.0

    # Observability — see plan/14-observability.md
    otel_service_name: str = "better-voyage"
    otel_exporter: Literal["console", "otlp", "none"] = "console"
    otel_endpoint: str = "http://localhost:4318"
    otel_sample_ratio: float = 1.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
