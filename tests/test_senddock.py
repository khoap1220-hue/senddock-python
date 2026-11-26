from __future__ import annotations

import asyncio
import dataclasses
import email.utils
import hashlib
import hmac
import http.server
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from senddock import (
    WEBHOOK_SIGNATURE_HEADER,
    AsyncSendDock,
    SendDock,
    SendDockError,
    SendToEmail,
    verify_webhook_signature,
)


def _json_reply(
    status: int,
    body: Any,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    return status, merged, json.dumps(body).encode("utf-8")


class _MockTransport:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(
        self,
        req: urllib.request.Request,
        timeout: float,
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.calls.append((req, timeout))
        if not self._responses:
            raise AssertionError("MockTransport exhausted unexpectedly")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _sign_webhook(payload: str | bytes, secret: str, timestamp: int) -> str:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(f"{timestamp}.".encode("ascii"))
    mac.update(raw)
    return f"t={timestamp},v1={mac.hexdigest()}"


class TestConstructorValidation:
    @pytest.mark.parametrize(
        ("base_url", "project_id", "api_key"),
        [
            ("", "proj-1", "sk_test"),
            ("   ", "proj-1", "sk_test"),
            ("https://mail.example.com", "", "sk_test"),
            ("https://mail.example.com", "   ", "sk_test"),
            ("https://mail.example.com", "proj-1", ""),
            ("https://mail.example.com", "proj-1", "   "),
        ],
    )
    def test_rejects_missing_required_fields(
        self,
        base_url: str,
        project_id: str,
        api_key: str,
    ) -> None:
        with pytest.raises(ValueError):
            SendDock(base_url=base_url, project_id=project_id, api_key=api_key)

    def test_rejects_invalid_timeout_and_retries(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            SendDock(base_url="https://mail.example.com", project_id="p", api_key="sk", timeout=0)
        with pytest.raises(ValueError, match="max_retries"):
            SendDock(base_url="https://mail.example.com", project_id="p", api_key="sk", max_retries=-1)

    def test_normalizes_trailing_slashes_and_encodes_project_id(self) -> None:
        transport = _MockTransport([_json_reply(200, {"total": 0})])
        with SendDock(
            base_url="https://mail.example.com///",
            project_id="proj/alpha 1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        ) as sd:
            sd.stats()

        req, timeout = transport.calls[0]
        assert req.full_url == "https://mail.example.com/api/v1/projects/proj%2Falpha%201/stats"
        assert timeout == 30.0


class TestSendDockEndpoints:
    def test_send_transactional_email_and_auth_header(self) -> None:
        transport = _MockTransport([_json_reply(200, {"message": "sent"})])
        sd = SendDock(
            base_url="https://mail.example.com/",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        payload: SendToEmail = {"to": "ada@example.com", "template_id": "t-1", "data": {"name": "Ada"}}
        result = sd.send(payload)

        assert result == {"message": "sent"}
        req, _ = transport.calls[0]
        assert req.full_url == "https://mail.example.com/api/v1/projects/proj-1/send"
        assert req.get_method() == "POST"
        assert req.get_header("Authorization") == "Bearer sk_test_123"
        assert req.get_header("Content-type") == "application/json"
        assert json.loads((req.data or b"").decode("utf-8")) == {
            "to": "ada@example.com",
            "template_id": "t-1",
            "data": {"name": "Ada"},
        }

    def test_send_supports_subscriber_raw_html_kwargs_and_dataclass(self) -> None:
        @dataclasses.dataclass
        class CustomSendPayload:
            to: str
            template_id: str
            subject: str | None = None

        transport = _MockTransport(
            [
                _json_reply(200, {"sent": 1, "failed": 0}),
                _json_reply(200, {"message": "sent"}),
                _json_reply(200, {"message": "sent"}),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        res_sub = sd.send({"subscriber_id": "sub-9", "template_id": "t-1"})
        res_html = sd.send(to="ada@example.com", subject="Hi", html_body="<h1>Hello</h1>")
        res_dc = sd.send(CustomSendPayload(to="ada@example.com", template_id="t-2"))

        assert res_sub == {"sent": 1, "failed": 0}
        assert res_html == {"message": "sent"}
        assert res_dc == {"message": "sent"}
        assert json.loads((transport.calls[0][0].data or b"").decode("utf-8")) == {
            "subscriber_id": "sub-9",
            "template_id": "t-1",
        }
        assert json.loads((transport.calls[1][0].data or b"").decode("utf-8")) == {
            "to": "ada@example.com",
            "subject": "Hi",
            "html_body": "<h1>Hello</h1>",
        }
        assert json.loads((transport.calls[2][0].data or b"").decode("utf-8")) == {
            "to": "ada@example.com",
            "template_id": "t-2",
        }

    def test_passes_through_suppressed_200_responses(self) -> None:
        transport = _MockTransport(
            [
                _json_reply(200, {"message": "suppressed", "suppressed": 1}),
                _json_reply(200, {"sent": 0, "failed": 0, "suppressed": 1}),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        direct = sd.send({"to": "suppressed@example.com", "template_id": "t-1"})
        subscriber = sd.send({"subscriber_id": "sub-suppressed", "template_id": "t-1"})

        assert direct == {"message": "suppressed", "suppressed": 1}
        assert subscriber == {"sent": 0, "failed": 0, "suppressed": 1}

    def test_send_batch_returns_counters(self) -> None:
        transport = _MockTransport([_json_reply(200, {"sent": 2, "failed": 0, "suppressed": 1})])
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        result = sd.send_batch(
            {
                "template_id": "t-1",
                "recipients": [
                    {"to": "a@example.com"},
                    {"to": "b@example.com", "data": {"name": "B"}},
                ],
            }
        )

        assert result == {"sent": 2, "failed": 0, "suppressed": 1}
        req, _ = transport.calls[0]
        assert req.full_url == "https://mail.example.com/api/v1/projects/proj-1/send/batch"

    def test_broadcast_supports_all_segment_and_newsletter_targets(self) -> None:
        transport = _MockTransport(
            [
                _json_reply(200, {"sent": 25, "failed": 0}),
                _json_reply(200, {"sent": 10, "failed": 0}),
                _json_reply(200, {"sent": 12, "broadcast_id": "b-9"}),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        r_all = sd.broadcast({"template_id": "t-1"})
        r_seg = sd.broadcast({"template_id": "t-1", "segment_id": "seg-1"})
        r_nl = sd.broadcast({"template_id": "t-1", "newsletter_id": "nl-3"})

        assert r_all == {"sent": 25, "failed": 0}
        assert r_seg == {"sent": 10, "failed": 0}
        assert r_nl == {"sent": 12, "broadcast_id": "b-9"}
        assert json.loads((transport.calls[1][0].data or b"").decode("utf-8")) == {
            "template_id": "t-1",
            "segment_id": "seg-1",
        }
        assert json.loads((transport.calls[2][0].data or b"").decode("utf-8")) == {
            "template_id": "t-1",
            "newsletter_id": "nl-3",
        }

    def test_import_subscribers_sends_bare_array_and_lowercase_query_flags(self) -> None:
        report_payload = {
            "imported": 1,
            "duplicates": 0,
            "syntax_invalid": 0,
            "no_mx": 0,
            "disposable": 0,
            "suppressed": 0,
            "rejected": [],
        }
        transport = _MockTransport(
            [
                _json_reply(200, report_payload),
                _json_reply(200, report_payload),
                _json_reply(200, report_payload),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        rows = [{"email": "a@example.com", "tags": ["beta"]}]
        result = sd.import_subscribers(rows, {"validate_mx": False})
        sd.import_subscribers(rows, validate_mx=True, validate_disposable=True)
        sd.import_subscribers(rows)

        assert result["imported"] == 1
        assert (
            transport.calls[0][0].full_url
            == "https://mail.example.com/api/v1/projects/proj-1/subscribers/import?validate_mx=false"
        )
        assert json.loads((transport.calls[0][0].data or b"").decode("utf-8")) == rows
        assert (
            transport.calls[1][0].full_url
            == "https://mail.example.com/api/v1/projects/proj-1/subscribers/import"
            "?validate_mx=true&validate_disposable=true"
        )
        assert transport.calls[2][0].full_url == "https://mail.example.com/api/v1/projects/proj-1/subscribers/import"

    def test_stats_uses_get_with_no_body(self) -> None:
        stats_body = {"total": 10, "sent": 8, "failed": 1, "bounced": 1, "suppressed": 0, "opened": 5}
        transport = _MockTransport([_json_reply(200, stats_body)])
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        assert sd.stats() == stats_body
        req, _ = transport.calls[0]
        assert req.get_method() == "GET"
        assert req.data is None
        assert req.get_header("Content-type") is None


class TestErrorsAndRetries:
    def test_maps_4xx_and_5xx_status_codes_to_senddock_error(self) -> None:
        transport = _MockTransport(
            [
                _json_reply(401, {"error": "missing or invalid api key"}),
                _json_reply(403, {"error": "this action is not permitted with an API key"}),
                _json_reply(404, {"error": "project not found"}),
                _json_reply(429, {"error": "rate limit exceeded", "code": "project_rate_limit"}),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        with pytest.raises(SendDockError) as e401:
            sd.stats()
        assert e401.value.status == 401
        assert e401.value.code == "unauthorized"
        assert e401.value.message == "missing or invalid api key"
        assert e401.value.is_auth_error is True
        assert e401.value.is_rate_limit is False

        with pytest.raises(SendDockError) as e403:
            sd.stats()
        assert e403.value.status == 403
        assert e403.value.code == "forbidden"
        assert e403.value.is_auth_error is True

        with pytest.raises(SendDockError) as e404:
            sd.stats()
        assert e404.value.status == 404
        assert e404.value.code == "not_found"
        assert e404.value.is_not_found is True

        with pytest.raises(SendDockError) as e429:
            sd.stats()
        assert e429.value.status == 429
        assert e429.value.code == "project_rate_limit"
        assert e429.value.is_rate_limit is True

    def test_handles_malformed_json_and_non_string_error_fields(self) -> None:
        transport = _MockTransport(
            [
                (200, {"Content-Type": "text/html"}, b"<html>gateway</html>"),
                (502, {"Content-Type": "text/plain"}, b"Bad Gateway"),
                _json_reply(400, {"error": {"nested": True}}),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            max_retries=0,
        )

        with pytest.raises(SendDockError) as bad_200:
            sd.stats()
        assert bad_200.value.status == 200
        assert bad_200.value.code == "invalid_json"
        assert bad_200.value.message == "the server returned a non-JSON response body"

        with pytest.raises(SendDockError) as bad_502:
            sd.stats()
        assert bad_502.value.status == 502
        assert bad_502.value.code == "server_error"
        assert bad_502.value.message == "request failed with status 502"
        assert bad_502.value.body is None

        with pytest.raises(SendDockError) as nested_400:
            sd.stats()
        assert nested_400.value.status == 400
        assert nested_400.value.code == "bad_request"
        assert nested_400.value.message == "request failed with status 400"
        assert nested_400.value.body == {"error": {"nested": True}}

    def test_retries_429_with_retry_after_and_caps_hostile_values(self) -> None:
        sleeps: list[float] = []
        future_http_date = email.utils.formatdate(time.time() + 12.0, usegmt=True)
        transport = _MockTransport(
            [
                _json_reply(429, {"error": "rate limit exceeded"}, {"retry-after": "1"}),
                _json_reply(429, {"error": "slow down"}, {"Retry-After": "86400"}),
                _json_reply(429, {"error": "date retry"}, {"Retry-After": future_http_date}),
                _json_reply(200, {"message": "sent"}),
            ]
        )
        sd = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport,
            sleep=sleeps.append,
            max_retries=3,
        )

        result = sd.send({"to": "a@example.com", "template_id": "t-1"})

        assert result == {"message": "sent"}
        assert len(transport.calls) == 4
        assert sleeps[0] == 1.0
        assert sleeps[1] == 60.0
        assert 0.0 < sleeps[2] <= 15.0

    def test_retry_exhaustion_on_5xx_and_no_retry_on_4xx(self) -> None:
        sleeps: list[float] = []
        transport_503 = _MockTransport(
            [
                _json_reply(503, {"error": "unavailable"}),
                _json_reply(503, {"error": "unavailable"}),
                _json_reply(503, {"error": "still unavailable"}),
            ]
        )
        sd_503 = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport_503,
            sleep=sleeps.append,
            max_retries=2,
        )

        with pytest.raises(SendDockError) as err_503:
            sd_503.stats()
        assert len(transport_503.calls) == 3
        assert sleeps == [0.5, 1.0]
        assert err_503.value.status == 503
        assert err_503.value.message == "still unavailable"

        transport_400 = _MockTransport([_json_reply(400, {"error": "invalid body"})])
        sd_400 = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport_400,
            sleep=sleeps.append,
            max_retries=3,
        )
        with pytest.raises(SendDockError, match="invalid body"):
            sd_400.send({"to": "x", "template_id": "t-1"})
        assert len(transport_400.calls) == 1

    def test_network_and_timeout_errors_retry_and_wrap_cleanly(self) -> None:
        sleeps: list[float] = []
        transport_net = _MockTransport(
            [
                urllib.error.URLError("connection refused"),
                _json_reply(200, {"message": "sent"}),
            ]
        )
        sd_net = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            transport=transport_net,
            sleep=sleeps.append,
            max_retries=2,
        )
        assert sd_net.send({"to": "a@example.com", "template_id": "t-1"}) == {"message": "sent"}
        assert sleeps == [0.5]

        transport_timeout = _MockTransport([urllib.error.URLError(TimeoutError("timed out"))])
        sd_timeout = SendDock(
            base_url="https://mail.example.com",
            project_id="proj-1",
            api_key="sk_test_123",
            timeout=30.0,
            transport=transport_timeout,
            max_retries=0,
        )
        with pytest.raises(SendDockError) as err_to:
            sd_timeout.stats()
        assert err_to.value.status == 0
        assert err_to.value.code == "timeout"
        assert err_to.value.is_network_error is True
        assert err_to.value.message == "request timed out after 30000ms"


@contextmanager
def _local_http_server(handler_cls: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


class TestLoopbackStdlibIntegration:
    def test_end_to_end_over_real_urllib_and_http_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        state = {"send_attempts": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                if self.headers.get("Authorization") != "Bearer sk_live_local":
                    self._write_json(401, {"error": "missing or invalid api key"})
                    return
                if self.path == "/api/v1/projects/p-local/stats":
                    self._write_json(
                        200,
                        {"total": 5, "sent": 4, "failed": 0, "bounced": 0, "suppressed": 1, "opened": 3},
                    )
                    return
                if self.path == "/api/v1/projects/p-local/slow":
                    time.sleep(0.25)
                    self._write_json(200, {"ok": True})
                    return
                self._write_json(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                if self.path == "/api/v1/projects/p-local/send":
                    state["send_attempts"] += 1
                    if state["send_attempts"] == 1:
                        self._write_json(
                            429,
                            {"error": "rate limit exceeded"},
                            extra_headers={"Retry-After": "0.01"},
                        )
                        return
                    self._write_json(200, {"message": "sent"})
                    return
                self._write_json(404, {"error": "not found"})

            def _write_json(
                self,
                status: int,
                payload: dict[str, Any],
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(encoded)

        with _local_http_server(Handler) as base_url:
            sd = SendDock(
                base_url=base_url,
                project_id="p-local",
                api_key="sk_live_local",
                timeout=2.0,
                max_retries=1,
            )
            assert sd.send({"to": "ada@example.com", "template_id": "tpl-1"}) == {"message": "sent"}
            assert state["send_attempts"] == 2
            assert sd.stats()["total"] == 5

            bad_auth = SendDock(
                base_url=base_url,
                project_id="p-local",
                api_key="sk_wrong",
                max_retries=2,
            )
            with pytest.raises(SendDockError) as auth_err:
                bad_auth.stats()
            assert auth_err.value.status == 401
            assert auth_err.value.is_auth_error is True

            tiny_timeout = SendDock(
                base_url=base_url,
                project_id="p-local",
                api_key="sk_live_local",
                timeout=0.05,
                max_retries=0,
            )
            with pytest.raises(SendDockError) as timeout_err:
                tiny_timeout._request("GET", "/slow")
            assert timeout_err.value.status == 0
            assert timeout_err.value.code == "timeout"


class TestAsyncSendDock:
    def test_async_client_retries_and_maps_responses(self) -> None:
        class FakeResponse:
            def __init__(self, status_code: int, body: Any, headers: dict[str, str] | None = None) -> None:
                self.status_code = status_code
                self.content = json.dumps(body).encode("utf-8")
                self.headers = headers or {}

        class FakeAsyncClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
                self.closed = False
                self._queue = [
                    FakeResponse(429, {"error": "slow down"}, {"Retry-After": "2"}),
                    FakeResponse(200, {"message": "suppressed", "suppressed": 1}),
                ]

            async def request(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                content: bytes | None,
                timeout: float,
            ) -> FakeResponse:
                _ = timeout
                self.calls.append((method, url, headers, content))
                return self._queue.pop(0)

            async def aclose(self) -> None:
                self.closed = True

        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def run() -> None:
            fake_http = FakeAsyncClient()
            async with AsyncSendDock(
                base_url="https://mail.example.com",
                project_id="proj-async",
                api_key="sk_async_1",
                http_client=fake_http,
                sleep=fake_sleep,
                max_retries=1,
            ) as client:
                res = await client.send({"to": "ada@example.com", "template_id": "t-1"})
                assert res == {"message": "suppressed", "suppressed": 1}
            assert sleeps == [2.0]
            assert len(fake_http.calls) == 2
            assert fake_http.closed is False

        asyncio.run(run())


class TestVerifyWebhookSignature:
    SECRET = "whsec_test_secret"
    PAYLOAD = json.dumps({"id": "evt_1", "type": "email.sent", "data": {}})
    NOW = 1_700_000_000

    def test_header_constant_matches_senddock_spec(self) -> None:
        assert WEBHOOK_SIGNATURE_HEADER == "X-SendDock-Signature"

    def test_accepts_valid_signature_for_str_bytes_and_uppercase_hex(self) -> None:
        sig = _sign_webhook(self.PAYLOAD, self.SECRET, self.NOW)
        assert verify_webhook_signature(self.PAYLOAD, sig, self.SECRET, now=self.NOW) is True
        assert verify_webhook_signature(self.PAYLOAD.encode("utf-8"), sig, self.SECRET, now=self.NOW) is True

        upper_sig = sig.replace("v1=", "v1=").split(",v1=")
        sig_upper = f"{upper_sig[0]},v1={upper_sig[1].upper()}"
        assert verify_webhook_signature(self.PAYLOAD, sig_upper, self.SECRET, now=self.NOW) is True

    def test_rejects_tampered_payload_and_wrong_secret(self) -> None:
        sig = _sign_webhook(self.PAYLOAD, self.SECRET, self.NOW)
        assert verify_webhook_signature(self.PAYLOAD + "x", sig, self.SECRET, now=self.NOW) is False
        assert verify_webhook_signature(self.PAYLOAD, sig, "wrong_secret", now=self.NOW) is False
        assert verify_webhook_signature(self.PAYLOAD, sig, "", now=self.NOW) is False

    def test_enforces_past_and_future_replay_tolerance(self) -> None:
        stale_sig = _sign_webhook(self.PAYLOAD, self.SECRET, self.NOW - 600)
        assert verify_webhook_signature(self.PAYLOAD, stale_sig, self.SECRET, now=self.NOW) is False
        assert (
            verify_webhook_signature(
                self.PAYLOAD,
                stale_sig,
                self.SECRET,
                now=self.NOW,
                tolerance_seconds=700,
            )
            is True
        )

        future_sig = _sign_webhook(self.PAYLOAD, self.SECRET, self.NOW + 301)
        assert verify_webhook_signature(self.PAYLOAD, future_sig, self.SECRET, now=self.NOW) is False

        boundary_sig = _sign_webhook(self.PAYLOAD, self.SECRET, self.NOW - 300)
        assert verify_webhook_signature(self.PAYLOAD, boundary_sig, self.SECRET, now=self.NOW) is True

    @pytest.mark.parametrize(
        "bad_header",
        [
            "",
            "garbage",
            "t=abc,v1=00",
            "t=1700000000",
            f"t=,v1={'0' * 64}",
            f"t=1_700_000_000,v1={'0' * 64}",
            f"t=1700000000,v1={'0' * 32} {'0' * 32}",
            "t=1700000000,v1=abc",
            "t=1700000000,v1=zzzz",
        ],
    )
    def test_rejects_malformed_headers(self, bad_header: str) -> None:
        assert verify_webhook_signature(self.PAYLOAD, bad_header, self.SECRET, now=self.NOW) is False
