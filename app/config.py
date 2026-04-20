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

    # NL summary (plan/08-nl-summary.md)
    summary_mode: Literal["llm", "fallback_only"] = "llm"
    summary_model: str = "claude-haiku-4-5"
    summary_max_tokens: int = 150
    summary_temperature: float = 0.3
    summary_timeout_s: float = 10.0
    summary_prompt_version: str = "v1"

    # Charts (plan/15-charts-bathymetry)
    charts_dir: Path = Field(default=Path("./data/charts"))
    gebco_path: Path | None = None
    gebco_download_url: str = (
        "https://www.bodc.ac.uk/data/open_download/gebco/gebco_2024_sub_ice_topo/zip/"
    )
    gebco_auto_download: bool = True
    shallow_cutoff_m: float = 2.0
    charts_max_age_days: int = 90
    navaid_bbox_pad_nm: float = 2.0
    tide_modulated_depth: bool = False
    tide_interpolation_radius_nm: float = 25.0
    noaa_enc_catalog_url: str = "https://charts.noaa.gov/ENCs/ENCProdCat.xml"
    overpass_base_url: str = "https://overpass-api.de/api/interpreter"
    # Extra Overpass mirrors tried in order if the primary returns 5xx.
    # Comma-separated list — shipping multiple mirrors because the
    # public `overpass-api.de` host returns 504 under load on multi-
    # degree bboxes (see plan/15 §Chart fetching failure modes).
    overpass_fallback_urls: str = (
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.openstreetmap.ru/api/interpreter,"
        "https://lz4.overpass-api.de/api/interpreter"
    )
    overpass_timeout_s: float = 240.0
    # "real" = download + preprocess per plan/15; "null" = development
    # stub that treats the planet as navigable water (tests + offline).
    chart_store_mode: Literal["real", "null"] = "real"

    def effective_gebco_path(self) -> Path:
        """Resolve the GEBCO netCDF path, defaulting under `charts_dir`.

        Operators can pre-stage the 8 GB file and point `BV_GEBCO_PATH`
        at it. When unset, we fall back to `charts_dir/gebco/...` so the
        startup auto-download has a stable target that survives process
        restarts.
        """
        if self.gebco_path is not None:
            return self.gebco_path
        return self.charts_dir / "gebco" / "GEBCO_2024_sub_ice_topo.nc"

    # Observability — see plan/14-observability.md
    otel_service_name: str = "better-voyage"
    otel_exporter: Literal["console", "otlp", "none"] = "console"
    otel_endpoint: str = "http://localhost:4318"
    otel_sample_ratio: float = 1.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
