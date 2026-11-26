# senddock-python

Python SDK for the [SendDock](https://senddock.dev) API — self-hosted email marketing, isolated by project.

> Unofficial MIT-licensed proof-of-concept for [SendDock/senddock#102](https://github.com/SendDock/senddock/issues/102), mirroring [`@senddock/sdk`](https://github.com/SendDock/senddock-js) v0.2.0 one-to-one.

Typed requests and responses (`py.typed`), zero required runtime dependencies (`urllib.request`), `Retry-After`-aware retries, webhook signature verification, and an optional `httpx` async client. Python 3.9+.

```bash
pip install senddock
# Optional async client:
pip install "senddock[async]"
```

## Quickstart

```python
import os
from senddock import SendDock

senddock = SendDock(
    base_url="https://mail.yourcompany.com",
    project_id="your-project-id",
    api_key=os.environ["SENDDOCK_API_KEY"],
)

senddock.send({
    "to": "ada@example.com",
    "template_id": "a3f0...",
    "data": {"name": "Ada"},
})
```

Create an API key in the dashboard under **Project → Settings → API Keys**. Keys are project-scoped — the client always talks to the project the key belongs to. Keep them server-side.

## What you can do

Everything the project-scoped API allows:

### Send a transactional email

Three shapes, one method — a template to any address, a template to a stored subscriber, or raw HTML:

```python
senddock.send({"to": "ada@example.com", "template_id": "tpl-id", "data": {"name": "Ada"}})

senddock.send({"subscriber_id": "sub-id", "template_id": "tpl-id"})

senddock.send({"to": "ada@example.com", "subject": "Hi", "html_body": "<h1>Hello</h1>"})
```

Recipients on the project's suppression list are skipped, not errored — the response is `{"message": "suppressed", "suppressed": 1}` with HTTP `200`.

### Send a batch

Up to 500 recipients per call, each with their own variables:

```python
result = senddock.send_batch({
    "template_id": "tpl-id",
    "recipients": [
        {"to": "ada@example.com", "data": {"name": "Ada"}},
        {"to": "alan@example.com", "data": {"name": "Alan"}},
    ],
})
# {"sent": 2, "failed": 0, "suppressed": 0}
```

### Broadcast to your list

All active subscribers, a saved segment, or a newsletter publication:

```python
senddock.broadcast({"template_id": "tpl-id"})

senddock.broadcast({"template_id": "tpl-id", "segment_id": "seg-id"})

senddock.broadcast({"template_id": "tpl-id", "newsletter_id": "nl-id"})
```

Broadcasts require the instance to have a public URL configured so recipients receive a working unsubscribe link. `segment_id` and `newsletter_id` are mutually exclusive.

### Import subscribers

Server-side bulk import with optional MX and disposable-domain checks:

```python
report = senddock.import_subscribers(
    [{"email": "ada@example.com", "name": "Ada", "tags": ["beta"]}],
    {"validate_mx": True, "validate_disposable": True},
)
# {"imported": 1, "duplicates": 0, "syntax_invalid": 0, "no_mx": 0, "disposable": 0, "suppressed": 0, "rejected": []}
```

### Project stats

```python
stats = senddock.stats()
# {"total": 1520, "sent": 1480, "failed": 12, "bounced": 8, "suppressed": 20, "opened": 980}
```

### Async client (`senddock[async]`)

```python
import os
from senddock import AsyncSendDock

async with AsyncSendDock(
    base_url="https://mail.yourcompany.com",
    project_id="your-project-id",
    api_key=os.environ["SENDDOCK_API_KEY"],
) as senddock:
    await senddock.send({"to": "ada@example.com", "template_id": "tpl-id"})
```

## Verify incoming webhooks

SendDock signs outbound webhooks with `X-SendDock-Signature: t=<unix>,v1=<hmac>`. Always verify against the **raw** request bytes before parsing JSON:

```python
import json
import os
from fastapi import FastAPI, HTTPException, Request
from senddock import verify_webhook_signature

app = FastAPI()

@app.post("/webhooks/senddock")
async def handle_senddock_webhook(request: Request) -> dict[str, bool]:
    raw_body = await request.body()
    valid = verify_webhook_signature(
        payload=raw_body,
        signature=request.headers.get("X-SendDock-Signature", ""),
        secret=os.environ["SENDDOCK_WEBHOOK_SECRET"],
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid signature")
    event = json.loads(raw_body)
    return {"ok": True}
```

## Errors and retries

Non-2xx responses and transport failures raise a typed `SendDockError`:

```python
from senddock import SendDockError

try:
    senddock.send({"to": "ada@example.com", "template_id": "tpl-id"})
except SendDockError as err:
    err.status          # 401, 404, 429, ... or 0 for network/timeout errors
    err.code            # "unauthorized", "not_found", "rate_limited", ...
    err.message         # the API's error string
    err.is_rate_limit   # convenience flags: is_auth_error, is_not_found, is_rate_limit, is_network_error
```

The client retries `429` (honoring `Retry-After`, capped at `60s`) and `500`/`502`/`503`/`504` responses plus transient network errors with exponential backoff (`max_retries=2` by default, `timeout=30.0` seconds). Set `max_retries=0` to disable retries.

## License

MIT — built by [Khoa Phan](https://khoaphan.dev).
