from functools import lru_cache
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    ENVIRONMENT: Literal["development", "production", "testing"] = "development"
    DEBUG: bool = False
    BASE_URL: str = "http://localhost:8000"

    # PostgreSQL configuration
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "linklite"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        if self.DATABASE_URL:
            # Ensure asyncpg dialect is used
            url = self.DATABASE_URL
            if url.startswith("postgresql://"):
                return url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Redis configuration
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL_SECONDS: int = 86400
    REDIS_NEGATIVE_CACHE_TTL_SECONDS: int = 60
    ENABLE_CACHE: bool = True

    # Rate limiting (per minute per client IP)
    RATE_LIMIT_CREATE_PER_MINUTE: int = 10
    RATE_LIMIT_REDIRECT_PER_MINUTE: int = 300
    TRUST_PROXY_HEADERS: bool = False

    # Analytics flush frequency
    ANALYTICS_FLUSH_INTERVAL_SECONDS: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
