"""Unit tests for the Email_Service Lambda (Task 11.2, Requirements 17.4 / 17.7).

These example-based tests pin down the concrete behaviour of the SQS-triggered
email consumer: building a BTG Pactual confirmation email from a transfer
receipt and sending it via Amazon SES, while never letting a failed send leak
out of the batch into the WhatsApp flow.

What is exercised
-----------------
* **transfer_confirmation with masking (Req 17.4 / 17.6)** — a
  ``transfer_confirmation`` event whose receipt carries *raw* account numbers
  results in exactly one SES ``send_email`` call, addressed from the verified
  sender to the client, whose HTML and Text bodies show only the **masked**
  account (last 4 digits) and never the full raw number. The COP-formatted
  amount and the referential-information disclaimer are present too.
* **Partial batch failure (Req 17.7)** — a batch of two records, one good and
  one whose SES send fails, reports *only* the failed record's ``messageId`` as
  a ``batchItemFailure`` so SQS retries only that message and the failure never
  reaches the main flow.
* **Unknown type skipped** — an event with an unhandled ``type`` is acknowledged
  (no send, no failure).

Mocking approach
----------------
The handler creates a module-level ``boto3.client("ses")`` at import time. We
replace ``handler._ses_client`` with a lightweight **spy** that records every
``send_email`` call and can be configured to raise a botocore ``ClientError``
for a specific recipient. A spy is chosen over moto here because (a) it lets us
assert the exact email body contents directly and (b) it makes the
conditional-failure needed by the partial-batch test trivial — moto's SES would
require verifying the sender identity and could not selectively fail one
recipient.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from lambdas.email_service import handler
from shared.formatting import format_cop
from shared.masking import mask_account

pytestmark = pytest.mark.unit

_SENDER = "noreply@btgpactual.com.co"
_TO = "client@example.com"
_BAD_TO = "broken@example.com"

# Raw (unmasked) account numbers as they could arrive in the receipt.
_SOURCE_ACCOUNT = "4009998888"
_DESTINATION_ACCOUNT = "4001234567"
_AMOUNT = 1234567.89


# ---------------------------------------------------------------------------
# Spy / fixtures
# ---------------------------------------------------------------------------


class _SesSpy:
    """Records ``send_email`` calls; stands in for the SES client.

    Optionally raises an SES ``ClientError`` when the message targets
    ``fail_for`` (a recipient address), so a single record in a batch can be
    made to fail deterministically.
    """

    def __init__(self, fail_for: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_for = fail_for

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        recipients = kwargs.get("Destination", {}).get("ToAddresses", [])
        if self.fail_for is not None and self.fail_for in recipients:
            raise ClientError(
                {
                    "Error": {
                        "Code": "MessageRejected",
                        "Message": "Email address is not verified.",
                    }
                },
                "SendEmail",
            )
        return {"MessageId": "test-ses-message-id"}

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "send_email was never called"
        return self.calls[-1]


@pytest.fixture
def ses_env(monkeypatch):
    """Configure SES sender + dummy AWS env for the duration of a test."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv(handler.SES_SENDER_EMAIL_ENV, _SENDER)


@pytest.fixture
def ses_spy(monkeypatch, ses_env):
    """Replace ``handler._ses_client`` with a recording spy; restore on teardown."""
    spy = _SesSpy()
    monkeypatch.setattr(handler, "_ses_client", spy)
    return spy


class _LambdaContext:
    """Minimal Lambda context for Powertools' ``inject_lambda_context``."""

    function_name = "email-service"
    memory_limit_in_mb = 256
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:email-service"
    )
    aws_request_id = "test-request-id"


_CONTEXT = _LambdaContext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _receipt(**overrides: Any) -> dict[str, Any]:
    """Build a transfer receipt with raw account numbers."""
    receipt = {
        "transactionId": "TX-20240101-0001",
        "sourceAccount": _SOURCE_ACCOUNT,
        "destinationAccount": _DESTINATION_ACCOUNT,
        "amount": _AMOUNT,
        "currency": "COP",
        "concept": "Pago arriendo",
        "executedAt": "2024-01-01T10:00:00Z",
        "status": "COMPLETED",
    }
    receipt.update(overrides)
    return receipt


def _event(to: str = _TO, payload: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """Build a ``transfer_confirmation`` EmailNotificationEvent body."""
    event = {
        "type": "transfer_confirmation",
        "correlationId": "corr-123",
        "to": to,
        "payload": payload if payload is not None else _receipt(),
    }
    event.update(overrides)
    return event


def _sqs_record(message_id: str, body_payload: dict[str, Any]) -> dict[str, Any]:
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
        },
        "messageAttributes": {},
        "md5OfBody": "0" * 32,
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:email-notification-queue",
        "awsRegion": "us-east-1",
    }


def _bodies(call: dict[str, Any]) -> tuple[str, str]:
    """Extract (html, text) bodies from a captured send_email call."""
    body = call["Message"]["Body"]
    return body["Html"]["Data"], body["Text"]["Data"]


# ---------------------------------------------------------------------------
# 1. transfer_confirmation with masking (Req 17.4 / 17.6)
# ---------------------------------------------------------------------------


class TestTransferConfirmationMasking:
    def test_sends_one_email_from_sender_to_client(self, ses_spy) -> None:
        record = SimpleNamespace(body=json.dumps(_event()))

        handler.record_handler(record)

        assert len(ses_spy.calls) == 1
        call = ses_spy.last
        assert call["Source"] == _SENDER
        assert call["Destination"]["ToAddresses"] == [_TO]

    def test_body_shows_masked_account_not_raw(self, ses_spy) -> None:
        record = SimpleNamespace(body=json.dumps(_event()))

        handler.record_handler(record)

        html, text = _bodies(ses_spy.last)
        masked = mask_account(_DESTINATION_ACCOUNT)  # "******4567"

        # Masked value present in both representations.
        assert masked in html
        assert masked in text

        # The raw, full account number must never leak into the email.
        assert _DESTINATION_ACCOUNT not in html
        assert _DESTINATION_ACCOUNT not in text
        assert _SOURCE_ACCOUNT not in html
        assert _SOURCE_ACCOUNT not in text

    def test_body_contains_cop_amount_and_disclaimer(self, ses_spy) -> None:
        record = SimpleNamespace(body=json.dumps(_event()))

        handler.record_handler(record)

        html, text = _bodies(ses_spy.last)
        formatted = format_cop(_AMOUNT)  # "$1.234.567,89"

        assert formatted in html
        assert formatted in text
        assert handler.DISCLAIMER in html
        assert handler.DISCLAIMER in text

    def test_accepts_wrapped_receipt_with_client_name(self, ses_spy) -> None:
        # The documented {"receipt": {...}, "clientName": ...} shape.
        payload = {"receipt": _receipt(), "clientName": "Ana Pérez"}
        record = SimpleNamespace(body=json.dumps(_event(payload=payload)))

        handler.record_handler(record)

        html, text = _bodies(ses_spy.last)
        assert "Ana Pérez" in html
        assert "Ana Pérez" in text
        # Masking still applies under the wrapped shape.
        assert _DESTINATION_ACCOUNT not in html
        assert mask_account(_DESTINATION_ACCOUNT) in html

    def test_handler_path_sends_and_reports_no_failures(self, ses_spy) -> None:
        event = {"Records": [_sqs_record("msg-1", _event())]}

        result = handler.handler(event, _CONTEXT)

        assert result == {"batchItemFailures": []}
        assert len(ses_spy.calls) == 1
        assert ses_spy.last["Source"] == _SENDER


# ---------------------------------------------------------------------------
# 2. Partial batch failure (Req 17.7)
# ---------------------------------------------------------------------------


class TestPartialBatchFailure:
    def test_only_failed_record_is_reported(self, monkeypatch, ses_env) -> None:
        # SES fails for the bad recipient, succeeds for the good one.
        spy = _SesSpy(fail_for=_BAD_TO)
        monkeypatch.setattr(handler, "_ses_client", spy)

        event = {
            "Records": [
                _sqs_record("msg-good", _event(to=_TO)),
                _sqs_record("msg-bad", _event(to=_BAD_TO)),
            ]
        }

        result = handler.handler(event, _CONTEXT)

        # Only the failing record is surfaced for retry; the good one is acked.
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}

        # Both records were attempted, so the good email still went out.
        assert len(spy.calls) == 2
        good_calls = [
            c for c in spy.calls if c["Destination"]["ToAddresses"] == [_TO]
        ]
        assert len(good_calls) == 1


# ---------------------------------------------------------------------------
# 3. Unknown type is skipped (no send, no failure)
# ---------------------------------------------------------------------------


class TestUnknownType:
    def test_unknown_type_is_acknowledged_without_send(self, ses_spy) -> None:
        record = SimpleNamespace(
            body=json.dumps({"type": "balance_alert", "correlationId": "c-9", "to": _TO})
        )

        # Skipped silently — no exception, no SES call.
        handler.record_handler(record)

        assert ses_spy.calls == []

    def test_unknown_type_not_reported_as_batch_failure(self, ses_spy) -> None:
        event = {
            "Records": [
                _sqs_record("msg-unknown", {"type": "nope", "to": _TO}),
            ]
        }

        result = handler.handler(event, _CONTEXT)

        assert result == {"batchItemFailures": []}
        assert ses_spy.calls == []
