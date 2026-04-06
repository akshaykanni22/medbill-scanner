"""
backend/config.py
============================================================
PURPOSE:
    Single source of truth for all application configuration.
    Uses pydantic-settings to load values from environment
    variables (and the .env file in development).

    All other modules import `settings` from here instead of
    calling os.getenv() directly. This centralises validation:
    if a required env var is missing, the app fails fast at
    startup with a clear error, not silently mid-request.

SECURITY NOTE:
    ANTHROPIC_API_KEY is typed as SecretStr — pydantic will render
    it as "**********" in repr() and str(), so it cannot appear in
    log files or error tracebacks. To get the raw key call:
        settings.anthropic_api_key.get_secret_value()
    Only do this at the point of use (e.g., passing to Anthropic SDK),
    never assign the result to a variable that could be logged.

USAGE:
    from backend.config import settings
    settings.anthropic_api_key.get_secret_value()  # str — raw key
    settings.max_upload_size_mb                    # int
    settings.frontend_url                          # str
============================================================
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment / .env file.

    WHY BaseSettings (not dataclass or plain dict):
        pydantic-settings reads from os.environ automatically,
        validates types, applies defaults, and marks secrets.
        A plain dict would require manual os.getenv() calls
        scattered throughout the codebase with no central validation.

    FIELD DEFAULTS:
        All defaults match the values in env.example.
        Production deployments override via environment variables.
        The .env file is used for local development only.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # WHY extra="ignore": docker-compose.yml and the host environment
        # may inject extra variables (PATH, HOME, etc.). Ignoring unknowns
        # prevents startup failures from unrelated env vars.
        extra="ignore",
    )

    # --- Anthropic ---
    anthropic_api_key: SecretStr = Field(
        min_length=1,
        description=(
            "Anthropic API key. Required. Get from console.anthropic.com. "
            "Typed as SecretStr — never appears in logs or repr(). "
            "Access via settings.anthropic_api_key.get_secret_value()."
        ),
    )

    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        min_length=1,
        description="Claude model ID to use for analysis and dispute generation.",
    )

    # --- ChromaDB ---
    chroma_host: str = Field(
        default="chromadb",
        description=(
            "ChromaDB hostname. 'chromadb' inside Docker (container name = hostname). "
            "'localhost' for local development outside Docker."
        ),
    )
    chroma_port: int = Field(
        default=8000,
        description="ChromaDB HTTP port.",
    )

    # --- RAG ---
    rag_top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of HCPCS results to return per semantic search query.",
    )

    embedding_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="sentence-transformers model name for HCPCS embeddings.",
    )

    # --- File upload ---
    max_upload_size_mb: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum bill upload size in megabytes. Reject anything larger.",
    )

    # --- Rate limiting ---
    rate_limit_per_minute: int = Field(
        default=10,
        ge=1,
        description="Maximum API requests per minute per IP address.",
    )

    # --- CORS ---
    frontend_url: str = Field(
        default="http://localhost:3000",
        description=(
            "Allowed CORS origin. Lock to the frontend URL. Never use wildcard *. "
            "Example: http://localhost:3000 (dev), https://app.example.com (prod)."
        ),
    )


# Module-level singleton.
# WHY module-level: pydantic-settings reads the .env file and validates
# all fields when Settings() is instantiated. Doing this once at import
# time means any misconfiguration surfaces immediately at startup,
# not on the first request. Every import of `settings` gets the same
# validated object — no repeated disk reads.
settings = Settings()
