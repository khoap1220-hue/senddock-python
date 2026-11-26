from __future__ import annotations

from .client import AsyncSendDock, SendDock
from .errors import SendDockError
from .models import (
    BatchRecipient,
    BroadcastRequest,
    BroadcastResponse,
    ImportOptions,
    ImportRejectedItem,
    ImportRejectReason,
    ImportResponse,
    ImportSubscriberRow,
    SendBatchRequest,
    SendBatchResponse,
    SendCountsResponse,
    SendRawHtml,
    SendRequest,
    SendResponse,
    SendSentResponse,
    SendSuppressedResponse,
    SendToEmail,
    SendToSubscriber,
    Stats,
    SubscriberStatus,
    WebhookEvent,
    WebhookEventType,
)
from .webhook import WEBHOOK_SIGNATURE_HEADER, verify_webhook_signature

__version__ = "0.2.0"

__all__ = [
    "WEBHOOK_SIGNATURE_HEADER",
    "AsyncSendDock",
    "BatchRecipient",
    "BroadcastRequest",
    "BroadcastResponse",
    "ImportOptions",
    "ImportRejectReason",
    "ImportRejectedItem",
    "ImportResponse",
    "ImportSubscriberRow",
    "SendBatchRequest",
    "SendBatchResponse",
    "SendCountsResponse",
    "SendDock",
    "SendDockError",
    "SendRawHtml",
    "SendRequest",
    "SendResponse",
    "SendSentResponse",
    "SendSuppressedResponse",
    "SendToEmail",
    "SendToSubscriber",
    "Stats",
    "SubscriberStatus",
    "WebhookEvent",
    "WebhookEventType",
    "__version__",
    "verify_webhook_signature",
]
