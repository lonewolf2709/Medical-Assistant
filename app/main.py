import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.logging_config import setup_logging
from app.middleware import RequestLoggingMiddleware
from app.rate_limiter import limiter
from app.routers import health, webhook
from app.services.notification_service import close_client

setup_logging(level="DEBUG" if settings.debug else "INFO")

logger = logging.getLogger(__name__)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Rate limit exceeded → answer 200 on /webhook so Telegram does not retry."""
    logger.warning(
        "Rate limit exceeded",
        extra={
            "path": request.url.path,
            "client": request.client.host if request.client else "unknown",
        },
    )
    if request.url.path == "/webhook":
        return JSONResponse(status_code=200, content={"ok": True})
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch any unhandled exception — log it and return a safe response.

    The webhook never returns 500 to Telegram, which would cause retries.
    """
    logger.exception(
        "Unhandled exception",
        extra={"path": request.url.path, "method": request.method, "error": str(exc)},
    )
    if request.url.path == "/webhook":
        return JSONResponse(status_code=200, content={"ok": True})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI started. Reminder polling runs via Celery Beat.")
    yield
    await close_client()


def create_app(debug: bool | None = None) -> FastAPI:
    """Build the application. `debug` defaults to the DEBUG setting."""
    debug = settings.debug if debug is None else debug

    app = FastAPI(title="Medication Assistant Bot", lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    app.add_exception_handler(Exception, global_exception_handler)

    # Logs full request bodies — which contain health data. Never enable in production.
    if settings.log_requests:
        if not debug:
            logger.warning("LOG_REQUESTS is on outside debug — request bodies contain user health data")
        app.add_middleware(RequestLoggingMiddleware)

    app.include_router(health.router)
    app.include_router(webhook.router)

    if debug:
        # Unauthenticated test endpoints that can act as any user — debug only.
        from app.routers import dev

        app.include_router(dev.router)
        logger.warning("DEBUG is on — unauthenticated /dev endpoints are mounted")

    if not settings.telegram_webhook_secret:
        logger.error(
            "TELEGRAM_WEBHOOK_SECRET is not set — /webhook will reject every request. "
            "Set it and register it with Telegram's setWebhook."
        )

    return app


app = create_app()
