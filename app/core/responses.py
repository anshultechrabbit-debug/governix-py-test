from typing import Any, Generic, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.request_context import get_request_id

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T
    request_id: str | None = None


class ErrorBody(BaseModel):
    code: int  # the HTTP status (401, 404, ...), repeated for clients that read only the JSON
    reason: str  # stable machine-readable identifier (INVALID_CREDENTIALS, VERSION_CONFLICT, ...)
    message: str
    details: Any | None = None


class ApiErrorResponse(BaseModel):
    success: bool = False
    error: ErrorBody
    request_id: str | None = None


def ok(data: T) -> ApiResponse[T]:
    return ApiResponse[T](data=data, request_id=get_request_id())


def error_response(
    status_code: int, reason: str, message: str, details: Any | None = None
) -> JSONResponse:
    body = ApiErrorResponse(
        error=ErrorBody(code=status_code, reason=reason, message=message, details=details),
        request_id=get_request_id(),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))
