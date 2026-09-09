"""Request logging + security headers middleware.

Ported from the org's fastapi-boilerplate `app/core/middleware.py`. Unlike
the boilerplate, request logging goes through `logging` (JSON, see
`core.logging`) rather than `print()`, so it's actually queryable in log
aggregation later.
"""

import logging
import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Assigns each request a `request_id`, times it, and logs the outcome.

    The `request_id` is stashed on `request.state.request_id` (readable by
    downstream handlers) and echoed back as the `X-Request-ID` response
    header, so a caller/log line can be correlated end to end.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Time the request, tag it with a request id, and log the outcome."""
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = str(process_time)

        logger.info(
            "request handled",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_s": round(process_time, 4),
            },
        )

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds baseline security response headers to every response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Add baseline security headers to the response before returning it."""
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        return response


def setup_middleware(app: FastAPI) -> None:
    """Register all custom middleware on the given FastAPI app."""
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
