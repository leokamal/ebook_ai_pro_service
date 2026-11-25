"""Error model for /api/v1: {"error": {"code", "message", "request_id"}}.

Handlers only reshape /api/v1 paths; every other path falls through to FastAPI's defaults so the
legacy {"detail": ...} responses are unchanged.
"""
import logging
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("uvicorn.error")
API_PREFIX = "/api/v1"

_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 413: "payload_too_large",
                 422: "validation_error", 429: "rate_limited"}


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, headers: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status_code, self.headers = code, message, status_code, headers


class Unauthorized(AppError):
    def __init__(self, message="Invalid or missing credentials"):
        super().__init__("unauthorized", message, 401, {"WWW-Authenticate": "Bearer"})


class NotFound(AppError):
    def __init__(self, message="Not found"):  # also used for other users' resources (no 403 leak)
        super().__init__("not_found", message, 404)


class Conflict(AppError):
    def __init__(self, message="Conflict", code="conflict"):
        super().__init__(code, message, 409)


class ServiceUnavailable(AppError):
    def __init__(self, message="Service not configured", code="index_unavailable"):
        super().__init__(code, message, 503)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith(API_PREFIX)


def _body(code, message, request, status, headers=None):
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    # the header is set here too: the 500 handler runs outside RequestIdMiddleware
    return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}}, status_code=status,
                        headers={**(headers or {}), "X-Request-ID": rid})


class RequestIdMiddleware:
    """Pure ASGI (safe with streaming): every request gets `request.state.request_id` (a client-supplied
    X-Request-ID is reused only if it is short and harmless) and the id is echoed in the response header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = None
        for k, v in scope.get("headers", []):
            if k == b"x-request-id":
                cand = v.decode("latin-1")[:64]
                rid = cand if re.fullmatch(r"[A-Za-z0-9._-]{8,64}", cand) else None
        rid = rid or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode()))
            await send(message)
        await self.app(scope, receive, send_with_id)


def install_error_handlers(app: FastAPI):
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(AppError)
    async def _app_error(request, exc: AppError):
        return _body(exc.code, exc.message, request, exc.status_code, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc):
        if not _is_api(request):
            return await http_exception_handler(request, exc)
        return _body(_STATUS_CODES.get(exc.status_code, "error"), str(exc.detail), request,
                     exc.status_code, getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):
        logger.exception("unhandled error on %s", request.url.path)  # details only in logs
        if not _is_api(request):
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("Internal Server Error", status_code=500)  # unchanged legacy behaviour
        return _body("internal_error", "Internal server error", request, 500)

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc):
        if not _is_api(request):
            return await request_validation_exception_handler(request, exc)
        fields = ", ".join(".".join(str(p) for p in e["loc"][1:]) or "body" for e in exc.errors())
        return _body("validation_error", f"Invalid request: {fields}", request, 422)
