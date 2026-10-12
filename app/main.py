import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api import health_router, links_router, redirect_router
from app.core.config import get_settings
from app.core.logging import logger, request_id_ctx, setup_logging
from app.db.session import AsyncSessionLocal, close_redis_client
from app.services.analytics_service import start_analytics_worker, stop_analytics_worker

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages application startup and graceful shutdown."""
    setup_logging(debug=settings.DEBUG)
    logger.info(
        "Starting LinkLite application",
        extra={"extra_fields": {"environment": settings.ENVIRONMENT}},
    )
    # Start periodic background flush of Redis click counters to PostgreSQL
    start_analytics_worker(
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.ANALYTICS_FLUSH_INTERVAL_SECONDS,
    )
    yield
    logger.info("Shutting down LinkLite application, closing connections...")
    await stop_analytics_worker()
    await close_redis_client()


app = FastAPI(
    title="LinkLite API",
    description="Production-grade URL Shortener with Redis caching, rate limiting, and analytics.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns or propagates X-Request-ID and logs request lifecycle with duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_ctx.set(req_id)
        start_time = time.perf_counter()

        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            response.headers["X-Request-ID"] = req_id

            # Avoid spamming logs for health probes in development
            if request.url.path != "/health" or response.status_code >= 400:
                logger.info(
                    "Request completed",
                    extra={
                        "extra_fields": {
                            "method": request.method,
                            "path": request.url.path,
                            "status_code": response.status_code,
                            "duration_ms": duration_ms,
                        }
                    },
                )
            return response
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(
                "Unhandled request exception",
                exc_info=exc,
                extra={
                    "extra_fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": duration_ms,
                    }
                },
            )
            raise
        finally:
            request_id_ctx.reset(token)


app.add_middleware(RequestContextMiddleware)

STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Include Routers
app.include_router(health_router)
app.include_router(links_router)
app.include_router(redirect_router)
