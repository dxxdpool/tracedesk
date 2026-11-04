import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """An error the API returns as {"reason_code", "message"} so agents can act on it."""

    def __init__(self, status_code: int, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason_code = reason_code
        self.message = message


def error_response(status_code: int, reason_code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"reason_code": reason_code, "message": message})


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        raise exc
    return error_response(exc.status_code, exc.reason_code, exc.message)


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full details go to the server log only; the client gets a stable code.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, "internal_error", "Unexpected server error")
