from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_STATUS_CODE_MAP: dict[int, str] = {
    0: "network_error",
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "unprocessable_entity",
    429: "rate_limited",
}


def _derive_error_code(status: int, body: Any) -> str:
    if isinstance(body, Mapping):
        raw_code = body.get("code")
        if isinstance(raw_code, str) and raw_code:
            return raw_code
    if status in _STATUS_CODE_MAP:
        return _STATUS_CODE_MAP[status]
    if 500 <= status < 600:
        return "server_error"
    return f"http_{status}"


class SendDockError(Exception):
    """Typed exception raised on non-2xx API responses, invalid payloads, or network errors."""

    status: int
    code: str
    message: str
    body: Any

    def __init__(
        self,
        status: int,
        message: str,
        body: Any = None,
        *,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body
        self.code = code if code is not None else _derive_error_code(status, body)

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_network_error(self) -> bool:
        return self.status == 0

    @property
    def is_not_found(self) -> bool:
        return self.status == 404

    def __repr__(self) -> str:
        return f"SendDockError(status={self.status!r}, code={self.code!r}, message={self.message!r})"
