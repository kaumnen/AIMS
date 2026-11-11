"""Errors as RFC 9457 problem details."""

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from aims.text import scrub

MEDIA = "application/problem+json"


class ProblemError(BaseModel):
    """One thing wrong with the request. `loc` points into the body or query; scenario
    problems also carry the YAML `line`."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    loc: list[str | int] = Field(default_factory=list)
    msg: str
    line: int | None = None


class Problem(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str | None = None
    errors: list[ProblemError] = Field(default_factory=list)


class ProblemException(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        code: str | None = None,
        errors: list[ProblemError] | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail or title)
        self.problem = Problem(
            title=title, status=status, detail=detail, code=code, errors=errors or [], **extra
        )
        self.headers = headers or {}


def problem_response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    # a detail may repeat what the caller sent. UTF-8 cannot encode a lone surrogate, so one
    # goes out as its escape.
    return JSONResponse(
        scrub(problem.model_dump(mode="json", exclude_none=True)),
        status_code=problem.status,
        media_type=MEDIA,
        headers=headers,
    )


TITLES = {
    400: "Bad request",
    401: "Not signed in",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    410: "Gone",
    412: "Precondition failed",
    413: "Too large",
    415: "Unsupported media type",
    422: "Invalid request",
    428: "Precondition required",
    429: "Too many requests",
    500: "Server error",
    503: "Unavailable",
}


def install(app: FastAPI, headers: Sequence[tuple[bytes, bytes]]) -> None:
    """Answer every error as a problem, kept on the request's state for the audit log.
    `headers` go on the reply to an unexpected error, as Starlette sends that reply outside
    every middleware."""
    error_headers = {k.decode(): v.decode() for k, v in headers}

    async def on_problem(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProblemException)
        request.state.problem = exc.problem
        return problem_response(exc.problem, exc.headers)

    async def on_http(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        title = TITLES.get(exc.status_code, "Error")
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
        headers = dict(exc.headers) if exc.headers else None
        problem = Problem(title=title, status=exc.status_code, detail=detail)
        request.state.problem = problem
        return problem_response(problem, headers)

    async def on_validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errors = [
            ProblemError(
                loc=[str(x) if not isinstance(x, int) else x for x in e["loc"]], msg=e["msg"]
            )
            for e in exc.errors()
        ]
        problem = Problem(title=TITLES[422], status=422, code="invalid", errors=errors)
        request.state.problem = problem
        return problem_response(problem)

    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        detail = "the server could not answer; its log says why"
        problem = Problem(title=TITLES[500], status=500, detail=detail)
        return problem_response(problem, error_headers)

    app.add_exception_handler(ProblemException, on_problem)
    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(Exception, on_error)


def responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries for error replies, declared under the problem media type itself."""
    schema = {"$ref": "#/components/schemas/Problem"}
    return {
        code: {"description": TITLES.get(code, "Error"), "content": {MEDIA: {"schema": schema}}}
        for code in codes
    }
