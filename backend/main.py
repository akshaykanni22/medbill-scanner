"""
backend/main.py
============================================================
PURPOSE:
    FastAPI application entry point.
    Creates the app, wires middleware, mounts routes,
    and configures logging.

    This file has almost no logic — it is the wiring layer.
    Business logic lives in services/, agent/, rag/.
    HTTP concerns live in api/routes.py and api/middleware.py.

HOW TO RUN (development):
    cd /Volumes/Sam-mini-extra/projects/medbill-scanner
    uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

HOW TO RUN (Docker):
    docker-compose up backend
    The Dockerfile CMD invokes uvicorn pointing to this module.

SECURITY NOTES:
    - slowapi error handlers are registered here so rate-limit
      responses use the structured ErrorResponse format rather
      than slowapi's default plain text.
    - Logging is configured at startup so all subsequent imports
      (which may log at module level) are captured.
============================================================
"""

import logging
import logging.config

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from backend.api.middleware import configure_cors, limiter
from backend.api.models import ErrorResponse
from backend.api.routes import router
from backend.config import settings

# ============================================================
# LOGGING SETUP
# ============================================================

# WHY DICTCONFIG (not basicConfig):
#   basicConfig only configures the root logger and only if it
#   has no handlers yet — fragile in multi-module apps.
#   dictConfig is explicit, complete, and the recommended approach
#   for applications (not just libraries).
_LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,   # preserve uvicorn's loggers
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)-8s %(name)-40s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
    "loggers": {
        # Reduce noise from very chatty third-party libraries.
        "chromadb": {"level": "WARNING"},
        "sentence_transformers": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}

logging.config.dictConfig(_LOGGING_CONFIG)
log = logging.getLogger(__name__)


# ============================================================
# EXCEPTION HANDLERS
# Defined before create_app() so they can be passed to
# add_exception_handler() without forward-reference issues.
# ============================================================

async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    Return a structured 429 when a client hits the rate limit.

    WHY NOT slowapi's default handler:
        The default returns plain text. Our frontend expects JSON
        with error/detail fields matching ErrorResponse.

    WHY settings.rate_limit_per_minute (not request.app.state.limiter._default_limits):
        Accessing _default_limits is reading a private attribute — fragile
        if slowapi changes internals. settings.rate_limit_per_minute is our
        own config value and is always correct.
    """
    return JSONResponse(
        status_code=429,
        content=ErrorResponse(
            error="rate_limit_exceeded",
            detail=(
                f"Too many requests. Limit is {settings.rate_limit_per_minute} "
                "requests per minute. Please wait before trying again."
            ),
        ).model_dump(),
    )


# ============================================================
# APP FACTORY
# ============================================================

def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    WHAT:
        1. Creates the FastAPI instance with metadata.
        2. Attaches the slowapi rate limiter to the app state.
        3. Registers the custom 429 JSON handler for rate limit errors.
        4. Configures CORS middleware (locked to FRONTEND_URL).
        5. Mounts all API routes under /api prefix (/api/analyze, /api/health).

    WHY FACTORY FUNCTION (not module-level `app`):
        A factory function makes the app testable — tests can call
        create_app() to get a fresh instance without import side effects.
        FastAPI applications are also easier to introspect when created
        explicitly.

    RETURNS:
        Configured FastAPI instance ready for uvicorn to serve.
    """
    _app = FastAPI(
        title="MedBill Scanner",
        description=(
            "Free medical bill anomaly detector. "
            "Uploads are processed locally — patient data never stored."
        ),
        version="0.1.0",
        # WHY docs_url=/api/docs: keeps docs behind /api prefix,
        # consistent with route structure, easier to proxy.
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Attach slowapi limiter to app state.
    # WHY app.state.limiter: slowapi looks for the limiter on app.state.
    # This is the documented integration pattern for FastAPI.
    _app.state.limiter = limiter

    # Register our custom 429 handler.
    # WHY NOT slowapi's default _rate_limit_exceeded_handler:
    #   The default returns plain text. Our frontend expects JSON with
    #   error/detail fields (ErrorResponse model). We register one handler
    #   here — the only place — so there is no ambiguity about which wins.
    _app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

    # Configure CORS — must come before routes so preflight OPTIONS
    # requests are handled by the middleware before reaching route handlers.
    configure_cors(_app)

    # Mount API routes.
    _app.include_router(router, prefix="/api")

    log.info("MedBill Scanner API started")
    return _app


# Module-level app instance — uvicorn looks for this.
# WHY at module level (not just inside __main__):
#   uvicorn imports this module and reads `app` directly.
#   It does NOT call main(). The factory pattern still works
#   because create_app() is called here at import time.
app = create_app()

