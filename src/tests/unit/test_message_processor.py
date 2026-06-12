"""Unit tests for the Message_Processor Lambda (Task 5.11).

These example-based tests pin down the per-message pipeline in
:func:`lambdas.message_processor.handler.record_handler` and the batch-level
partial-failure reporting in :func:`...handler.handler`.

What is exercised
-----------------
* **OTP callback priority (Req 16.4)** — an active OTP challenge short-circuits
  the pipeline: ``otp_callback.handle_otp_callback`` runs and the Strands Agent
  is *never* invoked.
* **Consent gate (Req 1.1/1.2/1.3)** — without accepted Terms & Conditions the
  message is routed to ``consent.handle_consent_flow`` and processing stops.
* **Auth gate (Req 5.1/6.5)** — a banking request with no session, or an expired
  session, stores the pending request and sends a login link without invoking
  the agent; a valid session lets the agent run.
* **Message-type routing (Req 2.5/3.5)** — text, quick-reply button, audio note
  (transcribed), failed transcription and unsupported media each take the right
  branch.
* **Statement PDF (Req 9.4 wiring)** — an agent response carrying a statement
  reference triggers ``messaging.send_twilio_document`` plus the text reply.
* **Partial batch failure (Req 3.9)** — a batch of two records where one raises
  reports *only* the failed record's ``messageId`` as a ``batchItemFailure``.

Isolation approach
------------------
The handler delegates every side effect to sibling modules imported as
``handler.auth`` / ``handler.consent`` / ``handler.messaging`` /
``handler.otp_callback`` / ``handler.transcription`` and to the module-global
``invoke_strands_agent``. We ``monkeypatch`` those attributes with spies that
record their calls, so no real AWS, DynamoDB or Twilio access happens. A
``record_handler`` test feeds a lightweight object exposing ``.body``; the
batch-level test builds a realistic SQS event dict that Powertools turns into
``SQSRecord``s.

``AWS_DEFAULT_REGION`` is set *before* importing the handler because the module
creates a module-level boto3 Lambda client at import time.
"""

from __future__ import annotations

import json
import os

import pytest

# The handler builds ``boto3.client("lambda")`` at import time — give boto3 a
# region (and dummy credentials) before importing it.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from lambdas.message_processor import handler  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Record:
    """Minimal stand-in for a Powertools ``SQSRecord`` (only ``.body`` used)."""

    def __init__(self, body: str) -> None:
        self.body = body


def _payload(**overrides: str) -> dict:
    """Build a TwilioWebhookPayload-shaped dict with sensible defaults."""
    base = {
        "MessageSid": "SM_test",
        "From": "whatsapp:+573001234567",
        "To": "whatsapp:+14155238886",
        "Body": "Hola",
        "NumMedia": "0",
        "correlationId": "corr-123",
    }
    base.update(overrides)
    return base


def _record(**overrides: str) -> _Record:
    """Build a record whose ``.body`` is the JSON-encoded payload."""
    return _Record(json.dumps(_payload(**overrides)))


class _LambdaContext:
    """Minimal Lambda context for Powertools' ``inject_lambda_context``."""

    function_name = "message-processor"
    memory_limit_in_mb = 512
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:message-processor"
    )
    aws_request_id = "test-request-id"


_CONTEXT = _LambdaContext()


class _Spy:
    """Callable that records its calls and returns a configurable value."""

    def __init__(self, return_value=None, side_effect=None) -> None:
        self.return_value = return_value
        self.side_effect = side_effect
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.side_effect is not None:
            return self.side_effect(*args, **kwargs)
        return self.return_value

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def stubs(monkeypatch):
    """Stub every sibling-module function the handler delegates to.

    Returns a dict of named :class:`_Spy` objects so tests can configure return
    values and assert call counts/arguments. Defaults describe the "happy path":
    no OTP, consent accepted, message type text, a valid auth session and a
    plain-text agent reply.
    """
    spies = {
        # otp_callback
        "get_pending_otp": _Spy(return_value=None),
        "handle_otp_callback": _Spy(),
        # consent
        "get_consent": _Spy(return_value={"status": "accepted"}),
        "has_accepted_consent": _Spy(return_value=True),
        "handle_consent_flow": _Spy(),
        # transcription
        "transcribe_audio": _Spy(return_value="texto transcrito"),
        # auth
        "get_auth_session": _Spy(return_value={"pk": "+573001234567"}),
        "is_expired": _Spy(return_value=False),
        "store_pending_request": _Spy(),
        "send_login_link": _Spy(),
        "derive_session_id": _Spy(return_value="session-abc"),
        # messaging
        "send_twilio_message": _Spy(),
        "send_twilio_document": _Spy(),
        # agent
        "invoke_strands_agent": _Spy(return_value="Tu saldo es $1.000.000,00"),
    }

    monkeypatch.setattr(handler.otp_callback, "get_pending_otp", spies["get_pending_otp"])
    monkeypatch.setattr(handler.otp_callback, "handle_otp_callback", spies["handle_otp_callback"])
    monkeypatch.setattr(handler.consent, "get_consent", spies["get_consent"])
    monkeypatch.setattr(handler.consent, "has_accepted_consent", spies["has_accepted_consent"])
    monkeypatch.setattr(handler.consent, "handle_consent_flow", spies["handle_consent_flow"])
    monkeypatch.setattr(handler.transcription, "transcribe_audio", spies["transcribe_audio"])
    monkeypatch.setattr(handler.auth, "get_auth_session", spies["get_auth_session"])
    monkeypatch.setattr(handler.auth, "is_expired", spies["is_expired"])
    monkeypatch.setattr(handler.auth, "store_pending_request", spies["store_pending_request"])
    monkeypatch.setattr(handler.auth, "send_login_link", spies["send_login_link"])
    monkeypatch.setattr(handler.auth, "derive_session_id", spies["derive_session_id"])
    monkeypatch.setattr(handler.messaging, "send_twilio_message", spies["send_twilio_message"])
    monkeypatch.setattr(handler.messaging, "send_twilio_document", spies["send_twilio_document"])
    monkeypatch.setattr(handler, "invoke_strands_agent", spies["invoke_strands_agent"])

    return spies


# ---------------------------------------------------------------------------
# 1. OTP callback priority (Requirement 16.4)
# ---------------------------------------------------------------------------


class TestOtpCallbackPriority:
    def test_pending_otp_handles_callback_and_skips_agent(self, stubs) -> None:
        pending = {"pk": "+573001234567", "code": "123456", "attempts": 0}
        stubs["get_pending_otp"].return_value = pending

        handler.record_handler(_record(Body="123456"))

        # The OTP callback is handled with the bare phone number and the code.
        assert stubs["handle_otp_callback"].call_count == 1
        args, _ = stubs["handle_otp_callback"].calls[0]
        assert args[0] == "+573001234567"
        assert args[1] == "123456"
        assert args[2] is pending
        # The agent must NOT run while an OTP challenge is pending.
        assert not stubs["invoke_strands_agent"].called
        # Consent / auth gates are never reached either.
        assert not stubs["get_consent"].called
        assert not stubs["get_auth_session"].called


# ---------------------------------------------------------------------------
# 2. Consent gate (Requirements 1.1, 1.2, 1.3)
# ---------------------------------------------------------------------------


class TestConsentGate:
    def test_no_consent_routes_to_consent_flow_and_stops(self, stubs) -> None:
        stubs["get_consent"].return_value = None
        stubs["has_accepted_consent"].return_value = False

        handler.record_handler(_record(Body="Quiero mi saldo"))

        assert stubs["handle_consent_flow"].call_count == 1
        # Processing stops: no routing, no auth, no agent.
        assert not stubs["get_auth_session"].called
        assert not stubs["invoke_strands_agent"].called

    def test_rejected_consent_routes_to_consent_flow_and_stops(self, stubs) -> None:
        stubs["get_consent"].return_value = {"status": "rejected"}
        stubs["has_accepted_consent"].return_value = False

        handler.record_handler(_record(Body="Hola"))

        assert stubs["handle_consent_flow"].call_count == 1
        assert not stubs["invoke_strands_agent"].called

    def test_accepted_consent_proceeds_past_gate(self, stubs) -> None:
        handler.record_handler(_record(Body="Quiero mi saldo"))

        # Consent accepted (fixture default) → flow is not triggered, agent runs.
        assert not stubs["handle_consent_flow"].called
        assert stubs["invoke_strands_agent"].called


# ---------------------------------------------------------------------------
# 3. Auth gate (Requirements 5.1, 6.5)
# ---------------------------------------------------------------------------


class TestAuthGate:
    def test_absent_session_sends_login_link_and_skips_agent(self, stubs) -> None:
        stubs["get_auth_session"].return_value = None

        handler.record_handler(_record(Body="Quiero transferir"))

        assert stubs["store_pending_request"].call_count == 1
        # The original request text is stored to resume after login.
        args, _ = stubs["store_pending_request"].calls[0]
        assert args[0] == "+573001234567"
        assert args[1] == "Quiero transferir"
        assert stubs["send_login_link"].call_count == 1
        assert not stubs["invoke_strands_agent"].called

    def test_expired_session_sends_login_link_and_skips_agent(self, stubs) -> None:
        stubs["get_auth_session"].return_value = {"pk": "+573001234567"}
        stubs["is_expired"].return_value = True

        handler.record_handler(_record(Body="Quiero transferir"))

        assert stubs["store_pending_request"].call_count == 1
        assert stubs["send_login_link"].call_count == 1
        assert not stubs["invoke_strands_agent"].called

    def test_valid_session_invokes_agent_and_replies(self, stubs) -> None:
        handler.record_handler(_record(Body="Cuanto tengo"))

        # Deterministic session id derived, agent invoked, text reply sent.
        assert stubs["derive_session_id"].call_count == 1
        assert stubs["invoke_strands_agent"].call_count == 1
        args, _ = stubs["invoke_strands_agent"].calls[0]
        assert args[0] == "session-abc"  # session_id
        assert args[1] == "Cuanto tengo"  # input_text
        assert args[2] == "+573001234567"  # phone_number
        assert not stubs["store_pending_request"].called
        assert not stubs["send_login_link"].called
        assert stubs["send_twilio_message"].call_count == 1


# ---------------------------------------------------------------------------
# 4. Message-type routing (Requirements 2.5, 3.5)
# ---------------------------------------------------------------------------


class TestMessageRouting:
    def test_text_message_passes_body_to_agent(self, stubs) -> None:
        handler.record_handler(_record(Body="  Consulta de saldo  "))

        args, _ = stubs["invoke_strands_agent"].calls[0]
        # Body is stripped before being forwarded.
        assert args[1] == "Consulta de saldo"

    def test_button_payload_takes_precedence_as_input(self, stubs) -> None:
        handler.record_handler(_record(Body="ignored text", ButtonPayload="ver_saldo"))

        args, _ = stubs["invoke_strands_agent"].calls[0]
        assert args[1] == "ver_saldo"

    def test_audio_message_uses_transcription_result(self, stubs) -> None:
        stubs["transcribe_audio"].return_value = "quiero ver mi saldo"

        handler.record_handler(
            _record(
                Body="",
                NumMedia="1",
                MediaUrl0="https://api.twilio.com/media/voice.ogg",
                MediaContentType0="audio/ogg",
            )
        )

        assert stubs["transcribe_audio"].call_count == 1
        t_args, _ = stubs["transcribe_audio"].calls[0]
        assert t_args[0] == "https://api.twilio.com/media/voice.ogg"
        assert t_args[1] == "+573001234567"
        args, _ = stubs["invoke_strands_agent"].calls[0]
        assert args[1] == "quiero ver mi saldo"

    def test_failed_transcription_sends_error_and_skips_agent(self, stubs) -> None:
        stubs["transcribe_audio"].return_value = None

        handler.record_handler(
            _record(
                Body="",
                NumMedia="1",
                MediaUrl0="https://api.twilio.com/media/voice.ogg",
                MediaContentType0="audio/ogg",
            )
        )

        assert stubs["send_twilio_message"].call_count == 1
        args, _ = stubs["send_twilio_message"].calls[0]
        assert args[0] == "+573001234567"
        assert args[1] == handler.ERROR_MESSAGES["transcription_failed"]
        assert not stubs["invoke_strands_agent"].called

    def test_unsupported_media_sends_error_and_skips_agent(self, stubs) -> None:
        handler.record_handler(
            _record(
                Body="",
                NumMedia="1",
                MediaUrl0="https://api.twilio.com/media/photo.jpg",
                MediaContentType0="image/jpeg",
            )
        )

        assert stubs["send_twilio_message"].call_count == 1
        args, _ = stubs["send_twilio_message"].calls[0]
        assert args[1] == handler.ERROR_MESSAGES["unsupported_format"]
        assert not stubs["transcribe_audio"].called
        assert not stubs["invoke_strands_agent"].called

    def test_empty_body_no_media_sends_unsupported_error(self, stubs) -> None:
        handler.record_handler(_record(Body="   ", NumMedia="0"))

        assert stubs["send_twilio_message"].call_count == 1
        args, _ = stubs["send_twilio_message"].calls[0]
        assert args[1] == handler.ERROR_MESSAGES["unsupported_format"]
        assert not stubs["invoke_strands_agent"].called


# ---------------------------------------------------------------------------
# 5. Statement PDF delivery (Requirement 9.4 wiring)
# ---------------------------------------------------------------------------


class TestStatementDelivery:
    def test_statement_response_sends_document_and_text(self, stubs) -> None:
        stubs["invoke_strands_agent"].return_value = {
            "text": "Aquí está tu extracto.",
            "statement": {
                "s3Bucket": "statement-bucket",
                "s3Key": "statements/abc.pdf",
                "fileName": "extracto.pdf",
            },
        }

        handler.record_handler(_record(Body="Quiero mi extracto"))

        assert stubs["send_twilio_document"].call_count == 1
        doc_args, _ = stubs["send_twilio_document"].calls[0]
        assert doc_args[0] == "+573001234567"
        assert doc_args[1] == "statement-bucket"
        assert doc_args[2] == "statements/abc.pdf"
        # The text body is also delivered.
        assert stubs["send_twilio_message"].call_count == 1
        msg_args, _ = stubs["send_twilio_message"].calls[0]
        assert msg_args[1] == "Aquí está tu extracto."

    def test_plain_text_response_sends_no_document(self, stubs) -> None:
        stubs["invoke_strands_agent"].return_value = "Tu saldo es $500.000,00"

        handler.record_handler(_record(Body="Mi saldo"))

        assert not stubs["send_twilio_document"].called
        assert stubs["send_twilio_message"].call_count == 1
        msg_args, _ = stubs["send_twilio_message"].calls[0]
        assert msg_args[1] == "Tu saldo es $500.000,00"

    def test_empty_text_response_sends_nothing(self, stubs) -> None:
        stubs["invoke_strands_agent"].return_value = "   "

        handler.record_handler(_record(Body="Mi saldo"))

        assert not stubs["send_twilio_document"].called
        assert not stubs["send_twilio_message"].called


# ---------------------------------------------------------------------------
# 6. Partial batch failure (Requirement 3.9)
# ---------------------------------------------------------------------------


def _sqs_record(message_id: str, body_payload: dict) -> dict:
    """Build a realistic SQS record dict (the fields Powertools needs)."""
    return {
        "messageId": message_id,
        "receiptHandle": f"receipt-{message_id}",
        "body": json.dumps(body_payload),
        "attributes": {
            "ApproximateReceiveCount": "1",
            "SentTimestamp": "1700000000000",
            "SenderId": "AIDA",
            "ApproximateFirstReceiveTimestamp": "1700000000000",
            "MessageGroupId": body_payload.get("From", ""),
            "MessageDeduplicationId": body_payload.get("MessageSid", ""),
        },
        "messageAttributes": {},
        "md5OfBody": "0" * 32,
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:inbound-messages-queue.fifo",
        "awsRegion": "us-east-1",
    }


class TestPartialBatchFailure:
    def test_only_failed_record_is_reported(self, stubs) -> None:
        good_phone = "+573001111111"
        bad_phone = "+573002222222"

        # The agent raises only for the "bad" phone number; the other succeeds.
        def _maybe_fail(session_id, input_text, phone_number):
            if phone_number == bad_phone:
                raise RuntimeError("Strands_Agent invocation failed")
            return "ok"

        stubs["invoke_strands_agent"].side_effect = _maybe_fail

        good = _payload(
            MessageSid="SM_good",
            From=f"whatsapp:{good_phone}",
            Body="saldo",
            correlationId="corr-good",
        )
        bad = _payload(
            MessageSid="SM_bad",
            From=f"whatsapp:{bad_phone}",
            Body="saldo",
            correlationId="corr-bad",
        )
        event = {
            "Records": [
                _sqs_record("msg-good", good),
                _sqs_record("msg-bad", bad),
            ]
        }

        result = handler.handler(event, _CONTEXT)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}
