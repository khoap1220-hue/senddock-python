from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

SubscriberStatus = Literal["active", "pending", "unsubscribed"]

ImportRejectReason = Literal[
    "syntax_invalid",
    "no_mx",
    "disposable",
    "suppressed",
    "duplicate",
]

WebhookEventType = Literal[
    "email.sent",
    "email.failed",
    "email.bounced",
    "email.opened",
    "email.clicked",
    "subscriber.created",
    "subscriber.unsubscribed",
    "subscriber.newsletter_unsubscribed",
]


class _SendToEmailRequired(TypedDict):
    to: str
    template_id: str


class SendToEmail(_SendToEmailRequired, total=False):
    subject: str
    data: dict[str, str]
    html_fields: list[str]


class _SendToSubscriberRequired(TypedDict):
    subscriber_id: str
    template_id: str


class SendToSubscriber(_SendToSubscriberRequired, total=False):
    subject: str
    html_fields: list[str]


class SendRawHtml(TypedDict):
    to: str
    subject: str
    html_body: str


class SendSentResponse(TypedDict):
    message: Literal["sent"]


class SendSuppressedResponse(TypedDict):
    message: Literal["suppressed"]
    suppressed: int


class _SendCountsRequired(TypedDict):
    sent: int
    failed: int


class SendCountsResponse(_SendCountsRequired, total=False):
    suppressed: int


if TYPE_CHECKING:
    SendRequest = SendToEmail | SendToSubscriber | SendRawHtml
    SendResponse = SendSentResponse | SendSuppressedResponse | SendCountsResponse
else:
    import typing as _typing

    _union_ctor = _typing.Union
    SendRequest = _union_ctor[(SendToEmail, SendToSubscriber, SendRawHtml)]
    SendResponse = _union_ctor[(SendSentResponse, SendSuppressedResponse, SendCountsResponse)]


class _BatchRecipientRequired(TypedDict):
    to: str


class BatchRecipient(_BatchRecipientRequired, total=False):
    data: dict[str, str]


class _SendBatchRequestRequired(TypedDict):
    template_id: str
    recipients: list[BatchRecipient]


class SendBatchRequest(_SendBatchRequestRequired, total=False):
    subject: str
    html_fields: list[str]


class SendBatchResponse(TypedDict):
    sent: int
    failed: int
    suppressed: int


class _BroadcastRequestRequired(TypedDict):
    template_id: str


class BroadcastRequest(_BroadcastRequestRequired, total=False):
    subject: str
    variables: dict[str, str]
    html_fields: list[str]
    segment_id: str
    newsletter_id: str


class _BroadcastResponseRequired(TypedDict):
    sent: int


class BroadcastResponse(_BroadcastResponseRequired, total=False):
    failed: int
    broadcast_id: str


class _ImportSubscriberRowRequired(TypedDict):
    email: str


class ImportSubscriberRow(_ImportSubscriberRowRequired, total=False):
    name: str
    status: SubscriberStatus
    fields: dict[str, Any]
    tags: list[str]


class ImportOptions(TypedDict, total=False):
    validate_mx: bool
    validate_disposable: bool


class ImportRejectedItem(TypedDict):
    email: str
    name: str
    reason: ImportRejectReason


class ImportResponse(TypedDict):
    imported: int
    duplicates: int
    syntax_invalid: int
    no_mx: int
    disposable: int
    suppressed: int
    rejected: list[ImportRejectedItem]


class Stats(TypedDict):
    total: int
    sent: int
    failed: int
    bounced: int
    suppressed: int
    opened: int


class WebhookEvent(TypedDict):
    id: str
    type: WebhookEventType
    created_at: str
    data: dict[str, Any]
