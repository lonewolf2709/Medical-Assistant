"""
Request/Response logging middleware — DEV/TESTING ONLY.
Remove or disable LOG_REQUESTS=false in production.
"""
import json
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("app.middleware")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every incoming request and outgoing response with a correlation ID."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())[:8]
        start = time.monotonic()

        # Read request body (for POST requests)
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes) if body_bytes else None
        except Exception:
            body = body_bytes.decode("utf-8", errors="replace")[:500]

        logger.info(
            "Incoming request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "body": body,
            },
        )

        # Rebuild request with body (body can only be read once)
        async def receive():
            return {"type": "http.request", "body": body_bytes}

        request = Request(request.scope, receive)

        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000)

        logger.info(
            "Outgoing response",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )

        return response
