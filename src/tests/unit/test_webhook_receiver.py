"""Unit tests for the Webhook_Receiver Lambda (Requirements 3.1, 3.2, 3.3).

These example-based tests pin down the concrete behaviour of the three
Webhook_Receiver building blocks and the handler that wires them together:

* **Signature validation (Req 3.2)** — a valid ``X-Twilio-Signature`` lets the
  request through (``200`` + message enqueued); an invalid signature is
  rejected with ``403`` and *nothing* is enqueued.
* **Parser (Req 3.1)** — ``parse_form_urlencoded`` extracts every field Twilio
  sends (text, media, button, profile), handles the base64 transport flag, and
  copes with media-only (empty ``Body``) and text-only messages.
* **Enqueue correctness (Req 3.3)** — the inbound FIFO message is sent with
  ``MessageGroupId = From`` and ``MessageDeduplicationId = MessageSid`` and a
  JSON body enriched with ``correlationId`` and ``receivedAt``.

Mocking approach
----------------
The handler and the ``enqueue`` module create their boto3 clients at *import
time*, and the handler caches the Twilio auth token at module scope
(``handler._twilio_auth_token``). To exercise them under moto we:

* start ``mock_aws`` and create a real FIFO queue (``*.fifo``,
  ``FifoQueue=true``), then **rebind ``enqueue._sqs``** to a client created
  inside the mock (restoring the original on teardown);
* set ``INBOUND_QUEUE_URL`` / ``AWS_DEFAULT_REGION`` in the environment;
* monkeypatch ``handler._load_twilio_auth_token`` to return a fixed token ``T``
  and reset ``handler._twilio_auth_token`` so no cached token leaks across
  tests. The signature is then computed with the *same* token ``T`` via
  ``twilio.request_validator.RequestValidator(T).compute_signature(url, params)``
  so validation and signing agree.

To assert the FIFO attributes precisely (moto does not always echo the
deduplication id back on ``receive_message``) we wrap the moto client's
``send_message`` with a thin spy that records the kwargs *and* forwards the
call to the real (mocked) queue — so we verify both the exact attributes and
that the message genuinely lands on the queue.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlencode

import boto3
import pytest
from moto import mock_aws
from twilio.request_validator import RequestValidator

from lambdas.webhook_receiver import enqueue, handler
from lambdas.webhook_receiver.parser import parse_form_urlencoded

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_QUEUE_NAME = "inbound-messages-queue.fifo"
_AUTH_TOKEN = "test_twilio_auth_token_0123456789abcdef"  # noqa: S105 (test fixture)
_DOMAIN = "api.example.com"
_RAW_PATH = "/webhook/twilio"
_URL = f"https://{_DOMAIN}{_RAW_PATH}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqs_queue(monkeypatch):
    """Provide a moto FIFO queue wired into the ``enqueue`` module.

    Yields a ``(queue_url, send_calls)`` tuple where ``send_calls`` is the list
    of kwargs the handler passed to ``send_message`` (captured by a spy that
    still forwards to the real mocked queue).
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        client = boto3.client("sqs", region_name=_REGION)
        queue_url = client.create_queue(
            QueueName=_QUEUE_NAME,
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
        )["QueueUrl"]

        monkeypatch.setenv("INBOUND_QUEUE_URL", queue_url)

        send_calls: list[dict] = []
        real_send = client.send_message

        def _spy_send(**kwargs):
            send_calls.append(kwargs)
            return real_send(**kwargs)

        client.send_message = _spy_send  # type: ignore[method-assign]

        original_sqs = enqueue._sqs
        enqueue._sqs = client
        try:
            yield queue_url, send_calls
        finally:
            enqueue._sqs = original_sqs


@pytest.fixture
def fixed_auth_token(monkeypatch):
    """Force the handler to use a known auth token and clear its cache."""
    monkeypatch.setattr(handler, "_twilio_auth_token", None)
    monkeypatch.setattr(handler, "_load_twilio_auth_token", lambda: _AUTH_TOKEN)
    return _AUTH_TOKEN


class _LambdaContext:
    """Minimal Lambda context for Powertools' ``inject_lambda_context``."""

    function_name = "webhook-receiver"
    memory_limit_in_mb = 256
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:webhook-receiver"
    )
    aws_request_id = "test-request-id"


_CONTEXT = _LambdaContext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_body(**fields: str) -> str:
    """Build a form-urlencoded body from the given Twilio fields."""
    return urlencode(fields)


def _params_from_body(body: str) -> dict[str, str]:
    """Reproduce exactly how the handler flattens the body into params."""
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _sign(body: str, token: str = _AUTH_TOKEN) -> str:
    """Compute a valid X-Twilio-Signature for ``body`` against ``token``."""
    validator = RequestValidator(token)
    return validator.compute_signature(_URL, _params_from_body(body))


def _make_event(body: str, signature: str, *, is_base64: bool = False) -> dict:
    """Build an API Gateway HTTP API (v2.0) proxy event for the webhook."""
    if is_base64:
        body = base64.b64encode(body.encode("utf-8")).decode("utf-8")
    return {
        "headers": {"x-twilio-signature": signature, "content-type": "application/x-www-form-urlencoded"},
        "requestContext": {"domainName": _DOMAIN},
        "rawPath": _RAW_PATH,
        "body": body,
        "isBase64Encoded": is_base64,
    }


def _drain(queue_url: str) -> list[dict]:
    """Receive all messages currently on the queue (with all attributes)."""
    client = enqueue._sqs
    received = client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        AttributeNames=["All"],
        MessageAttributeNames=["All"],
    )
    return received.get("Messages", [])


# ---------------------------------------------------------------------------
# 1. Signature validation (Requirement 3.2)
# ---------------------------------------------------------------------------


class TestSignatureValidation:
    def test_valid_signature_returns_200_and_enqueues(self, sqs_queue, fixed_auth_token) -> None:
        queue_url, send_calls = sqs_queue
        body = _make_body(
            MessageSid="SM_valid_1",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Hola, quiero ver mi saldo",
            NumMedia="0",
        )
        event = _make_event(body, _sign(body))

        result = handler.handler(event, _CONTEXT)

        assert result == {"statusCode": 200, "body": ""}
        assert len(send_calls) == 1
        messages = _drain(queue_url)
        assert len(messages) == 1

    def test_invalid_signature_returns_403_and_enqueues_nothing(
        self, sqs_queue, fixed_auth_token
    ) -> None:
        queue_url, send_calls = sqs_queue
        body = _make_body(
            MessageSid="SM_invalid_1",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Mensaje forjado",
            NumMedia="0",
        )
        event = _make_event(body, "this-is-not-a-valid-signature")

        result = handler.handler(event, _CONTEXT)

        assert result == {"statusCode": 403, "body": ""}
        assert send_calls == []  # send_message never called
        assert _drain(queue_url) == []  # queue is empty

    def test_missing_signature_header_returns_403(self, sqs_queue, fixed_auth_token) -> None:
        queue_url, send_calls = sqs_queue
        body = _make_body(
            MessageSid="SM_missing_sig",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Sin firma",
            NumMedia="0",
        )
        event = _make_event(body, "")

        result = handler.handler(event, _CONTEXT)

        assert result["statusCode"] == 403
        assert send_calls == []

    def test_tampered_body_invalidates_signature(self, sqs_queue, fixed_auth_token) -> None:
        """A signature computed for one body must not validate another body."""
        queue_url, send_calls = sqs_queue
        signed_body = _make_body(
            MessageSid="SM_tamper",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Monto original",
            NumMedia="0",
        )
        signature = _sign(signed_body)
        tampered_body = signed_body.replace("Monto+original", "Monto+alterado")
        event = _make_event(tampered_body, signature)

        result = handler.handler(event, _CONTEXT)

        assert result["statusCode"] == 403
        assert send_calls == []


# ---------------------------------------------------------------------------
# 2. Parser handles all fields (Requirement 3.1)
# ---------------------------------------------------------------------------


class TestParser:
    def test_extracts_all_fields(self) -> None:
        body = _make_body(
            MessageSid="SM123",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Hola",
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/media/abc",
            MediaContentType0="audio/ogg",
            ButtonPayload="accept_tc",
            ProfileName="Carlos Rodriguez",
        )

        payload = parse_form_urlencoded(body, is_base64=False)

        assert payload == {
            "MessageSid": "SM123",
            "From": "whatsapp:+573001234567",
            "To": "whatsapp:+14155238886",
            "Body": "Hola",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/abc",
            "MediaContentType0": "audio/ogg",
            "ButtonPayload": "accept_tc",
            "ProfileName": "Carlos Rodriguez",
        }

    def test_base64_encoded_body(self) -> None:
        body = _make_body(
            MessageSid="SM_b64",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Mensaje en base64",
            NumMedia="0",
        )
        encoded = base64.b64encode(body.encode("utf-8")).decode("utf-8")

        payload = parse_form_urlencoded(encoded, is_base64=True)

        assert payload["MessageSid"] == "SM_b64"
        assert payload["Body"] == "Mensaje en base64"
        assert payload["From"] == "whatsapp:+573001234567"

    def test_media_only_message_has_empty_body(self) -> None:
        body = _make_body(
            MessageSid="SM_media",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="",
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/media/voice.ogg",
            MediaContentType0="audio/ogg",
        )

        payload = parse_form_urlencoded(body, is_base64=False)

        assert payload["Body"] == ""
        assert payload["NumMedia"] == "1"
        assert payload["MediaUrl0"] == "https://api.twilio.com/media/voice.ogg"
        assert payload["MediaContentType0"] == "audio/ogg"
        # Optional fields not sent are absent, not empty strings.
        assert "ButtonPayload" not in payload
        assert "ProfileName" not in payload

    def test_text_only_message_omits_media_fields(self) -> None:
        body = _make_body(
            MessageSid="SM_text",
            From="whatsapp:+573001234567",
            To="whatsapp:+14155238886",
            Body="Solo texto",
            NumMedia="0",
        )

        payload = parse_form_urlencoded(body, is_base64=False)

        assert payload["Body"] == "Solo texto"
        assert payload["NumMedia"] == "0"
        assert "MediaUrl0" not in payload
        assert "MediaContentType0" not in payload

    def test_missing_required_fields_get_defaults(self) -> None:
        # Only From present — the rest default (Body -> "", NumMedia -> "0").
        body = _make_body(From="whatsapp:+573001234567")

        payload = parse_form_urlencoded(body, is_base64=False)

        assert payload["From"] == "whatsapp:+573001234567"
        assert payload["MessageSid"] == ""
        assert payload["To"] == ""
        assert payload["Body"] == ""
        assert payload["NumMedia"] == "0"

    def test_empty_body_returns_defaults(self) -> None:
        payload = parse_form_urlencoded("", is_base64=False)

        assert payload == {
            "MessageSid": "",
            "From": "",
            "To": "",
            "Body": "",
            "NumMedia": "0",
        }


# ---------------------------------------------------------------------------
# 3. Enqueue correctness (Requirement 3.3)
# ---------------------------------------------------------------------------


class TestEnqueueCorrectness:
    def test_send_message_uses_from_and_messagesid(self, sqs_queue, fixed_auth_token) -> None:
        queue_url, send_calls = sqs_queue
        from_ = "whatsapp:+573009998877"
        message_sid = "SM_dedup_42"
        body = _make_body(
            MessageSid=message_sid,
            From=from_,
            To="whatsapp:+14155238886",
            Body="Quiero transferir plata",
            NumMedia="0",
        )
        event = _make_event(body, _sign(body))

        result = handler.handler(event, _CONTEXT)

        assert result["statusCode"] == 200
        assert len(send_calls) == 1
        call = send_calls[0]
        assert call["QueueUrl"] == queue_url
        assert call["MessageGroupId"] == from_
        assert call["MessageDeduplicationId"] == message_sid

    def test_message_group_id_visible_on_received_message(
        self, sqs_queue, fixed_auth_token
    ) -> None:
        queue_url, _send_calls = sqs_queue
        from_ = "whatsapp:+573001112233"
        body = _make_body(
            MessageSid="SM_group_check",
            From=from_,
            To="whatsapp:+14155238886",
            Body="Hola",
            NumMedia="0",
        )
        event = _make_event(body, _sign(body))

        handler.handler(event, _CONTEXT)

        messages = _drain(queue_url)
        assert len(messages) == 1
        assert messages[0]["Attributes"]["MessageGroupId"] == from_

    def test_enqueued_body_has_correlation_id_and_received_at(
        self, sqs_queue, fixed_auth_token
    ) -> None:
        queue_url, send_calls = sqs_queue
        body = _make_body(
            MessageSid="SM_body_check",
            From="whatsapp:+573004445566",
            To="whatsapp:+14155238886",
            Body="Consulta de saldo",
            NumMedia="0",
        )
        event = _make_event(body, _sign(body))

        handler.handler(event, _CONTEXT)

        sent_body = json.loads(send_calls[0]["MessageBody"])
        assert sent_body["MessageSid"] == "SM_body_check"
        assert sent_body["From"] == "whatsapp:+573004445566"
        assert sent_body["Body"] == "Consulta de saldo"
        # Enrichment added by enqueue_message.
        assert "correlationId" in sent_body and sent_body["correlationId"]
        assert "receivedAt" in sent_body and sent_body["receivedAt"]
        # receivedAt is an ISO 8601 UTC timestamp.
        from datetime import datetime

        datetime.fromisoformat(sent_body["receivedAt"])

    def test_base64_body_is_enqueued(self, sqs_queue, fixed_auth_token) -> None:
        queue_url, send_calls = sqs_queue
        body = _make_body(
            MessageSid="SM_b64_enqueue",
            From="whatsapp:+573007778899",
            To="whatsapp:+14155238886",
            Body="Mensaje base64",
            NumMedia="0",
        )
        event = _make_event(body, _sign(body), is_base64=True)

        result = handler.handler(event, _CONTEXT)

        assert result["statusCode"] == 200
        sent_body = json.loads(send_calls[0]["MessageBody"])
        assert sent_body["MessageSid"] == "SM_b64_enqueue"
        assert sent_body["Body"] == "Mensaje base64"

