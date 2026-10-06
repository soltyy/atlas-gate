"""Ошибки гейта в форме `{error: {code, message}}` — только на маршрутах `/harness/*`.

Обработчик исключений приложения гейт не ставит (это правка `main.py` сверх одного блока):
форму держит свой класс маршрута `GateRoute` — он оборачивает обработчик вместе с разрешением
зависимостей, так что `require_device`, валидация тела и `HTTPException` админских зависимостей
роутера (`require_token`, `local_only`) отвечают той же формой. Прежние маршруты `/v1/*` не затронуты.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
# Базовый класс Starlette: FastAPI бросает именно его (например, «error parsing the body»), а
# fastapi.HTTPException — его подкласс; ловить подкласс значит пропустить часть отказов.
from starlette.exceptions import HTTPException

STATUS_CODES = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                409: "conflict", 413: "payload_too_large", 422: "invalid_request", 502: "agent_backend_error",
                503: "gateway_down"}


class GateError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None,
                   **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, **extra), headers=headers)


class GateRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        inner = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await inner(request)
            except GateError as failed:
                return error_response(failed.status, failed.code, failed.message, **failed.extra)
            except RequestValidationError as bad:
                first = bad.errors()[0] if bad.errors() else {}
                where = ".".join(str(x) for x in first.get("loc", ()))
                return error_response(422, "invalid_request", f"{where}: {first.get('msg', 'неверный запрос')}")
            except HTTPException as http:
                return error_response(http.status_code, STATUS_CODES.get(http.status_code, "error"),
                                      str(http.detail), headers=http.headers)

        return handler
