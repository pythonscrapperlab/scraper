"""
Centralized configuration for Aevorex scraper.

Loads from environment variables with sensible defaults for local development.
"""

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # === Database ===
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "aevorex_db"
    db_user: str = "aevorex"
    db_password: SecretStr
    db_echo: bool = False  # Log SQL queries
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_timeout: int = 10
    db_command_timeout: int = 30

    # === Scraper General ===
    scraper_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    scraper_timeout: int = 30  # seconds
    scraper_retries: int = 3
    scraper_retry_delay: int = 5  # seconds
    scraper_concurrent_urls: int = 3  # Conservative detail-page concurrency

    # === Transport: Playwright ===
    playwright_headless: bool = True
    playwright_timeout: int = 30000  # milliseconds
    playwright_wait_for: str = "networkidle"  # networkidle | load | domcontentloaded

    # === Transport: Selenium ===
    selenium_headless: bool = True
    selenium_timeout: int = 30  # seconds

    # === Transport: Proxies ===
    proxy_enabled: bool = False
    proxy_provider: str = "webshare"  # brightdata | oxylabs | smartproxy | webshare | dataimpulse
    proxy_user: SecretStr
    proxy_password: SecretStr
    proxy_zone: str | None = "US"

    # === Supabase publishing target ===
    supabase_project_url: str | None = None
    supabase_publishable_key: SecretStr | None = None
    supabase_direct_connection_url: SecretStr | None = None

    # === Scraping: Zillow ===
    zillow_enabled: bool = True
    zillow_base_url: str = "https://www.zillow.com"
    zillow_search_path: str = "/homes/for_sale"

    # === Scraping: Redfin ===
    redfin_enabled: bool = True
    redfin_base_url: str = "https://www.redfin.com"
    redfin_api_url: str = "https://www.redfin.com/api/gis"

    # === Scraping: Realtor ===
    realtor_enabled: bool = True
    realtor_base_url: str = "https://www.realtor.com"

    # === Target States ===
    # Comma-separated list of states (e.g., "FL,CA")
    target_states_str: str = Field(default="FL,CA", validation_alias="target_states")

    # === Scheduler ===
    scheduler_enabled: bool = False
    scheduler_zillow_interval_hours: int = 6
    scheduler_redfin_interval_hours: int = 6
    scheduler_realtor_interval_hours: int = 6
    scheduler_scoring_interval_hours: int = 6

    # === Scoring ===
    scoring_enabled: bool = True

    # === Logging ===
    log_level: str = "INFO"
    log_file: str | None = "logs/aevorex.log"

    @computed_field  # type: ignore
    @property
    def target_states(self) -> list[str]:
        """Get target_states as a list."""
        return [s.strip() for s in self.target_states_str.split(",") if s.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Global settings singleton
settings = Settings()
