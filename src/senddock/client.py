from __future__ import annotations

import calendar
import dataclasses
import email.utils
import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from .errors import SendDockError
from .models import (
    BroadcastRequest,
    BroadcastResponse,
    ImportOptions,
    ImportResponse,
    ImportSubscriberRow,
    SendBatchRequest,
    SendBatchResponse,
    SendRequest,
    SendResponse,
    Stats,
)

RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS: float = 60.0
BASE_BACKOFF_SECONDS: float = 0.5

SyncTransport = Callable[[urllib.request.Request, float], tuple[int, Mapping[str, str], bytes]]


def _validate_client_init(
    base_url: str,
    project_id: str,
    api_key: str,
    timeout: float,
    max_retries: int,
) -> tuple[str, str, str]:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url is required, e.g. https://senddock.example.com")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key is required (a project API key, sk_...)")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    normalized_base = base_url.strip().rstrip("/")
    encoded_project = urllib.parse.quote(project_id.strip(), safe="")
    return normalized_base, encoded_project, api_key.strip()


def _serialize_value(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            field.name: _serialize_value(getattr(obj, field.name))
            for field in dataclasses.fields(obj)
            if getattr(obj, field.name) is not None
        }
    if isinstance(obj, Mapping):
        return {str(k): _serialize_value(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_serialize_value(item) for item in obj]
    return obj


def _coerce_body_payload(request: Any, kwargs: dict[str, Any], method_name: str) -> dict[str, Any]:
    if request is not None and kwargs:
        raise ValueError(f"{method_name}() accepts either a request object or keyword arguments, not both")
    if request is None:
        if not kwargs:
            raise ValueError(f"{method_name}() requires a request body")
        return cast(dict[str, Any], _serialize_value(kwargs))
    serialized = _serialize_value(request)
    if not isinstance(serialized, dict):
        raise TypeError(f"{method_name}() request must be a mapping or dataclass")
    return serialized


def _build_import_path(
    options: ImportOptions | Any | None,
    validate_mx: bool | None,
    validate_disposable: bool | None,
) -> str:
    opt_dict: dict[str, Any] = {}
    if options is not None:
        serialized = _serialize_value(options)
        if not isinstance(serialized, dict):
            raise TypeError("options must be a mapping or dataclass")
        opt_dict = serialized

    eff_mx = validate_mx if validate_mx is not None else opt_dict.get("validate_mx")
    eff_disp = validate_disposable if validate_disposable is not None else opt_dict.get("validate_disposable")

    params: list[tuple[str, str]] = []
    if eff_mx is not None:
        params.append(("validate_mx", "true" if bool(eff_mx) else "false"))
    if eff_disp is not None:
        params.append(("validate_disposable", "true" if bool(eff_disp) else "false"))

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    return f"/subscribers/import{query}"


def _lookup_header(headers: Mapping[str, str], target: str) -> str | None:
    target_lower = target.lower()
    for key, value in headers.items():
        if key.lower() == target_lower:
            return value
    return None


def _retry_delay_seconds(headers: Mapping[str, str], attempt: int) -> float:
    raw = _lookup_header(headers, "Retry-After")
    if raw is not None:
        stripped = raw.strip()
        try:
            seconds = float(stripped)
        except ValueError:
            seconds = math.nan
        if math.isfinite(seconds) and seconds > 0:
            return min(seconds, MAX_RETRY_AFTER_SECONDS)
        try:
            parsed_dt = email.utils.parsedate_to_datetime(stripped)
            target_ts = (
                float(calendar.timegm(parsed_dt.timetuple()))
                if parsed_dt.tzinfo is None
                else parsed_dt.timestamp()
            )
            delta = target_ts - time.time()
            if delta > 0:
                return min(delta, MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError, OverflowError):
            pass
    return float(BASE_BACKOFF_SECONDS * (2**attempt))


def _parse_success_json(status: int, raw_body: bytes) -> Any:
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as cause:
        raise SendDockError(
            status,
            "the server returned a non-JSON response body",
            cause,
            code="invalid_json",
        ) from cause


def _build_http_error(status: int, raw_body: bytes) -> SendDockError:
    message = f"request failed with status {status}"
    parsed_body: Any = None
    try:
        parsed_body = json.loads(raw_body.decode("utf-8"))
        if isinstance(parsed_body, Mapping):
            err_field = parsed_body.get("error")
            if isinstance(err_field, str) and err_field:
                message = err_field
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = None
    return SendDockError(status, message, parsed_body)


def _is_timeout_exception(cause: BaseException) -> bool:
    if isinstance(cause, TimeoutError):
        return True
    if isinstance(cause, urllib.error.URLError) and isinstance(cause.reason, TimeoutError):
        return True
    return type(cause).__name__ in {"ReadTimeout", "ConnectTimeout", "PoolTimeout", "TimeoutException"}


def _build_network_error(cause: BaseException, timeout_seconds: float) -> SendDockError:
    if _is_timeout_exception(cause):
        timeout_ms = int(round(timeout_seconds * 1000))
        return SendDockError(0, f"request timed out after {timeout_ms}ms", cause, code="timeout")
    if isinstance(cause, urllib.error.URLError):
        detail = str(cause.reason)
    else:
        detail = str(cause) or type(cause).__name__
    return SendDockError(0, f"network error: {detail}", cause, code="network_error")


def _default_urllib_transport(
    req: urllib.request.Request,
    timeout: float,
) -> tuple[int, Mapping[str, str], bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            headers = dict(resp.headers.items())
            body = resp.read()
            return status, headers, body
    except urllib.error.HTTPError as exc:
        try:
            status = int(exc.code)
            headers = dict(exc.headers.items()) if exc.headers is not None else {}
            body = exc.read()
            return status, headers, body
        finally:
            exc.close()


class SendDock:
    """Synchronous SendDock API client backed by the Python standard library."""

    def __init__(
        self,
        base_url: str,
        project_id: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: SyncTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_base, encoded_project, clean_key = _validate_client_init(
            base_url,
            project_id,
            api_key,
            timeout,
            max_retries,
        )
        self._base_url = normalized_base
        self._project_id = encoded_project
        self._api_key = clean_key
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._transport: SyncTransport = transport or _default_urllib_transport
        self._sleep = sleep

    def __enter__(self) -> SendDock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close client resources (no-op for the stateless stdlib transport)."""

    def send(self, request: SendRequest | Any = None, **kwargs: Any) -> SendResponse:
        payload = _coerce_body_payload(request, kwargs, "send")
        return cast(SendResponse, self._request("POST", "/send", payload))

    def send_batch(self, request: SendBatchRequest | Any = None, **kwargs: Any) -> SendBatchResponse:
        payload = _coerce_body_payload(request, kwargs, "send_batch")
        return cast(SendBatchResponse, self._request("POST", "/send/batch", payload))

    def broadcast(self, request: BroadcastRequest | Any = None, **kwargs: Any) -> BroadcastResponse:
        payload = _coerce_body_payload(request, kwargs, "broadcast")
        return cast(BroadcastResponse, self._request("POST", "/broadcast", payload))

    def import_subscribers(
        self,
        rows: Sequence[ImportSubscriberRow | Any],
        options: ImportOptions | Any | None = None,
        *,
        validate_mx: bool | None = None,
        validate_disposable: bool | None = None,
    ) -> ImportResponse:
        path = _build_import_path(options, validate_mx, validate_disposable)
        serialized_rows = [_serialize_value(row) for row in rows]
        return cast(ImportResponse, self._request("POST", path, serialized_rows))

    def stats(self) -> Stats:
        return cast(Stats, self._request("GET", "/stats"))

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{self._base_url}/api/v1/projects/{self._project_id}{path}"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        encoded_body: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded_body = json.dumps(body).encode("utf-8")

        last_error: SendDockError | None = None
        for attempt in range(self._max_retries + 1):
            req = urllib.request.Request(url, data=encoded_body, headers=headers, method=method)
            try:
                status, resp_headers, raw_resp = self._transport(req, self._timeout)
            except SendDockError:
                raise
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as cause:
                last_error = _build_network_error(cause, self._timeout)
                if attempt == self._max_retries:
                    raise last_error from cause
                self._sleep(float(BASE_BACKOFF_SECONDS * (2**attempt)))
                continue

            if 200 <= status < 300:
                return _parse_success_json(status, raw_resp)

            error = _build_http_error(status, raw_resp)
            if status not in RETRYABLE_STATUS or attempt == self._max_retries:
                raise error
            last_error = error
            self._sleep(_retry_delay_seconds(resp_headers, attempt))

        raise last_error or SendDockError(0, "request failed")


class AsyncSendDock:
    """Asynchronous SendDock API client powered by the optional ``httpx`` extra."""

    def __init__(
        self,
        base_url: str,
        project_id: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        normalized_base, encoded_project, clean_key = _validate_client_init(
            base_url,
            project_id,
            api_key,
            timeout,
            max_retries,
        )
        self._base_url = normalized_base
        self._project_id = encoded_project
        self._api_key = clean_key
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._owns_client = http_client is None

        if http_client is None:
            try:
                import httpx
            except ImportError as exc:
                raise ImportError(
                    "AsyncSendDock requires httpx. Install it with: pip install 'senddock[async]'"
                ) from exc
            self._client: Any = httpx.AsyncClient(timeout=self._timeout)
        else:
            self._client = http_client

        if sleep is None:
            import asyncio

            self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        else:
            self._sleep = sleep

    async def __aenter__(self) -> AsyncSendDock:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, request: SendRequest | Any = None, **kwargs: Any) -> SendResponse:
        payload = _coerce_body_payload(request, kwargs, "send")
        return cast(SendResponse, await self._request("POST", "/send", payload))

    async def send_batch(self, request: SendBatchRequest | Any = None, **kwargs: Any) -> SendBatchResponse:
        payload = _coerce_body_payload(request, kwargs, "send_batch")
        return cast(SendBatchResponse, await self._request("POST", "/send/batch", payload))

    async def broadcast(self, request: BroadcastRequest | Any = None, **kwargs: Any) -> BroadcastResponse:
        payload = _coerce_body_payload(request, kwargs, "broadcast")
        return cast(BroadcastResponse, await self._request("POST", "/broadcast", payload))

    async def import_subscribers(
        self,
        rows: Sequence[ImportSubscriberRow | Any],
        options: ImportOptions | Any | None = None,
        *,
        validate_mx: bool | None = None,
        validate_disposable: bool | None = None,
    ) -> ImportResponse:
        path = _build_import_path(options, validate_mx, validate_disposable)
        serialized_rows = [_serialize_value(row) for row in rows]
        return cast(ImportResponse, await self._request("POST", path, serialized_rows))

    async def stats(self) -> Stats:
        return cast(Stats, await self._request("GET", "/stats"))

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{self._base_url}/api/v1/projects/{self._project_id}{path}"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        content: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body).encode("utf-8")

        last_error: SendDockError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    content=content,
                    timeout=self._timeout,
                )
            except SendDockError:
                raise
            except Exception as cause:
                last_error = _build_network_error(cause, self._timeout)
                if attempt == self._max_retries:
                    raise last_error from cause
                await self._sleep(float(BASE_BACKOFF_SECONDS * (2**attempt)))
                continue

            status = int(response.status_code)
            raw_resp = bytes(response.content)
            if 200 <= status < 300:
                return _parse_success_json(status, raw_resp)

            error = _build_http_error(status, raw_resp)
            if status not in RETRYABLE_STATUS or attempt == self._max_retries:
                raise error
            last_error = error
            await self._sleep(_retry_delay_seconds(dict(response.headers.items()), attempt))

        raise last_error or SendDockError(0, "request failed")
