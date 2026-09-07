"""Typed exception hierarchy + FastAPI handlers.

Ported from the org's fastapi-boilerplate `app/core/exceptions.py`.
Pipeline code (`extract.py`, `resolve.py`, `integrations/simboard_client.py`)
should raise these instead of letting raw exceptions surface, so any HTTP
surface this app exposes returns a consistent JSON error shape instead of a
stack trace.
"""

from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class BaseCustomException(Exception):
    """Base class for this app's typed exceptions."""

    def __init__(self, message: str, status_code: int = 500):
        """Store the error message and the HTTP status code to respond with."""
        self.message = message
        self.status_code = status_code
        super().__init__(message, status_code)


class ValidationException(BaseCustomException):
    """Raised when input/LLM-output validation fails.

    E.g. `extract.py` schema validation against `ExtractionResult`.
    """

    def __init__(self, message: str = "Validation error", details: Optional[Any] = None):
        """Set an optional `details` payload alongside the base message."""
        super().__init__(message, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.details = details


class NotFoundException(BaseCustomException):
    """Raised when a referenced resource is not found.

    E.g. a SimBoard project/board/user.
    """

    def __init__(self, message: str = "Resource not found"):  # noqa: B042
        """Set the 404 status code with the given message."""
        super().__init__(message, status.HTTP_404_NOT_FOUND)


class UnauthorizedException(BaseCustomException):
    """Raised when a call requires authentication that is missing or invalid."""

    def __init__(self, message: str = "Authentication required"):  # noqa: B042
        """Set the 401 status code with the given message."""
        super().__init__(message, status.HTTP_401_UNAUTHORIZED)


class ForbiddenException(BaseCustomException):
    """Raised when the caller is authenticated but lacks permission."""

    def __init__(self, message: str = "Insufficient permissions"):  # noqa: B042
        """Set the 403 status code with the given message."""
        super().__init__(message, status.HTTP_403_FORBIDDEN)


class ConflictException(BaseCustomException):
    """Raised on a resource conflict, e.g. duplicate card creation."""

    def __init__(self, message: str = "Resource conflict"):  # noqa: B042
        """Set the 409 status code with the given message."""
        super().__init__(message, status.HTTP_409_CONFLICT)


class UpstreamServiceException(BaseCustomException):
    """Raised when an external API call (SimBoard, Claude, Graph) fails or times out.

    Distinct from ValidationException/NotFoundException, which describe our
    own request; this describes a downstream failure.
    """

    def __init__(self, message: str = "Upstream service error", status_code: int = 502):
        """Set the given status code (default 502) with the given message."""
        super().__init__(message, status_code)


async def custom_exception_handler(request: Request, exc: BaseCustomException) -> JSONResponse:
    """Handle BaseCustomException and its subclasses."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.message,
            "type": exc.__class__.__name__,
            "details": getattr(exc, "details", None),
        },
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handle framework-level HTTPException."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "type": "HTTPException"},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle FastAPI request validation errors."""
    errors = []
    for error in exc.errors():
        error_dict = {
            "loc": error.get("loc", []),
            "msg": str(error.get("msg", "")),
            "type": error.get("type", ""),
        }
        if "ctx" in error:
            error_dict["ctx"] = {k: str(v) for k, v in error["ctx"].items()}
        errors.append(error_dict)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": errors},
    )


def setup_exception_handlers(app: FastAPI) -> None:
    """Register all custom exception handlers on the given FastAPI app."""
    app.add_exception_handler(BaseCustomException, custom_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
