import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings

RESERVED_ALIASES = {
    "api",
    "docs",
    "health",
    "admin",
    "static",
    "redoc",
    "openapi.json",
    "favicon.ico",
}

ALIAS_REGEX = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


class LinkCreateRequest(BaseModel):
    long_url: str = Field(
        ...,
        description="Destination URL to shorten (HTTP/HTTPS only, max 2048 chars).",
        max_length=2048,
    )
    custom_alias: str | None = Field(
        default=None,
        description="Optional user-chosen alias (3-32 characters, [A-Za-z0-9_-]).",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional future timestamp when the link expires.",
    )

    @field_validator("long_url")
    @classmethod
    def validate_long_url(cls, v: str) -> str:
        url_str = v.strip()
        if not url_str:
            raise ValueError("URL cannot be empty")
        if len(url_str) > 2048:
            raise ValueError("URL exceeds maximum length of 2048 characters")

        try:
            parsed = urlparse(url_str)
        except Exception as e:
            raise ValueError(f"Malformed URL: {e}") from e

        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme '{scheme}'. Only http and https are allowed")

        if not parsed.netloc or not parsed.hostname:
            raise ValueError("URL must include a valid host / domain name")

        # Loop prevention: Reject target pointing to the LinkLite service itself
        settings = get_settings()
        base_parsed = urlparse(settings.BASE_URL)

        target_host = (parsed.hostname or "").lower()
        base_host = (base_parsed.hostname or "").lower()

        # Check hostname match and optional port match
        if target_host == base_host:
            target_port = parsed.port or (443 if scheme == "https" else 80)
            base_port = base_parsed.port or (443 if base_parsed.scheme == "https" else 80)
            if target_port == base_port:
                raise ValueError(
                    "Cannot shorten a URL that points to this shortener service (loop prevention)"
                )

        return url_str

    @field_validator("custom_alias")
    @classmethod
    def validate_custom_alias(cls, v: str | None) -> str | None:
        if v is None:
            return None
        alias = v.strip()
        if not ALIAS_REGEX.match(alias):
            raise ValueError(
                "Custom alias must be 3-32 characters long and contain only "
                "letters, numbers, underscores, or hyphens"
            )
        if alias.lower() in RESERVED_ALIASES:
            raise ValueError(f"Alias '{alias}' is a reserved keyword and cannot be used")
        return alias

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        # Convert to timezone-aware UTC for comparison
        now = datetime.now(UTC)
        expiry = v if v.tzinfo else v.replace(tzinfo=UTC)
        if expiry <= now:
            raise ValueError("Expiration date must be in the future")
        return expiry


class LinkResponse(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None = None
    click_count: int = 0
