"""Unit tests for the OTP_Service Lambda (Task 8.2, Requirements 16.1 / 16.2).

These example-based tests pin down the concrete behaviour of the
``generate-and-wait`` handler that Step Functions invokes with the task token in
the payload.

What is exercised
-----------------
* **6-digit numeric code + put_item carries the task token (Req 16.1 / 16.2)** —
  a valid event persists an OTP_Store record whose ``code`` is a 6-character
  string of digits, keyed by the normalized phone, carrying the ``taskToken``
  and ``executionArn`` verbatim, with ``attempts == 0``, a ``transferContext``
  holding the amount + destination account, and ``ttl ≈ now + OTP_TTL`` (300s).
* **SMS identifies the operation (Req 16.2)** — the dispatched SMS body contains
  the COP-formatted amount, the **masked** destination account, and the *exact*
  code that was stored; the Pinpoint request targets the configured
  ``ApplicationId``, uses the ``TRANSACTIONAL`` message type, and addresses the
  phone over the ``SMS`` channel.
* **whatsapp: prefix normalized** — a ``whatsapp:+57…`` phone is stored under the
  bare E.164 partition key.
* **Missing phoneNumber / taskToken → ValueError** — without either the workflow
  cannot be resumed, so the task fails loudly.

Mocking approach
----------------
The handler creates module-level ``boto3.resource("dynamodb")`` and
``boto3.client("pinpoint")`` at import time.

* **DynamoDB** is mocked with moto ``mock_aws``: we rebind ``handler._dynamodb``
  to a resource created inside the mock, create the OTP_Store table (``pk``
  String HASH), and restore the original on teardown.
* **Pinpoint** is replaced with a lightweight **spy** object that records the
  ``send_messages`` kwargs. moto's Pinpoint coverage does not include
  ``send_messages`` (message delivery is not emulated), so a spy is the reliable
  way to assert the request shape and SMS body without a real AWS call.
"""

from __future__ import annotations

import time

import boto3
import pytest
from moto import mock_aws

from lambdas.otp_service import handler
from shared.constants import OTP_TTL
from shared.formatting import format_cop
from shared.masking import mask_account

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_TABLE_NAME = "BTGConnectAI-sandbox-OTPStore"
_APP_ID = "test-pinpoint-app-id"

_PHONE = "+573001234567"
_AMOUNT = 150000
_DESTINATION = "1021803076"
_TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAAtest-task-token-0123456789"
_EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:TransferBreb:exec-1"


# ---------------------------------------------------------------------------
# Spy / fixtures
# ---------------------------------------------------------------------------


class _PinpointSpy:
    """Records ``send_messages`` calls; stands in for the Pinpoint client.

    moto does not emulate ``pinpoint.send_messages`` (SMS delivery), so we
    capture the request kwargs here and return a minimal, realistic response.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_messages(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return {"MessageResponse": {"ApplicationId": kwargs.get("ApplicationId")}}

    @property
    def last(self) -> dict:
        assert self.calls, "send_messages was never called"
        return self.calls[-1]


@pytest.fixture
def otp_table(monkeypatch):
    """Provide a moto OTP_Store table wired into the handler module.

    Yields the boto3 ``Table`` resource so tests can read back written items.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("OTP_TABLE_NAME", _TABLE_NAME)
    monkeypatch.setenv("PINPOINT_APP_ID", _APP_ID)
    # Ensure optional origination/sender config does not leak in from the env.
    monkeypatch.delenv("PINPOINT_ORIGINATION_NUMBER", raising=False)
    monkeypatch.delenv("PINPOINT_SENDER_ID", raising=False)

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=_REGION)
        resource.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = resource.Table(_TABLE_NAME)

        original = handler._dynamodb
        handler._dynamodb = resource
        try:
            yield table
        finally:
            handler._dynamodb = original


@pytest.fixture
def pinpoint_spy():
    """Replace ``handler._pinpoint`` with a recording spy; restore on teardown."""
    spy = _PinpointSpy()
    original = handler._pinpoint
    handler._pinpoint = spy
    try:
        yield spy
    finally:
        handler._pinpoint = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(**overrides: object) -> dict:
    """Build a valid ``generate-and-wait`` event, with optional overrides."""
    event = {
        "operation": "generate-and-wait",
        "phoneNumber": _PHONE,
        "transferAmount": _AMOUNT,
        "destinationAccount": _DESTINATION,
        "taskToken": _TASK_TOKEN,
        "executionArn": _EXECUTION_ARN,
    }
    event.update(overrides)
    return event


def _sms_message(call: dict) -> dict:
    """Extract the ``SMSMessage`` block from a captured send_messages call."""
    return call["MessageRequest"]["MessageConfiguration"]["SMSMessage"]


# ---------------------------------------------------------------------------
# 1. 6-digit numeric code + put_item carries the task token (Req 16.1 / 16.2)
# ---------------------------------------------------------------------------


class TestGenerateAndPersist:
    def test_persists_record_with_code_token_and_context(
        self, otp_table, pinpoint_spy
    ) -> None:
        before = int(time.time())

        result = handler.handler(_event())

        after = int(time.time())

        assert result == {"ok": True}

        item = otp_table.get_item(Key={"pk": _PHONE}).get("Item")
        assert item is not None

        # Code: exactly 6 characters, all digits.
        code = item["code"]
        assert isinstance(code, str)
        assert len(code) == handler.OTP_CODE_LENGTH == 6
        assert code.isdigit()

        # Key + task token + execution ARN persisted verbatim (Req 16.1).
        assert item["pk"] == _PHONE
        assert item["taskToken"] == _TASK_TOKEN
        assert item["executionArn"] == _EXECUTION_ARN

        # Attempts start at zero; transfer context is preserved for the callback.
        assert int(item["attempts"]) == 0
        context = item["transferContext"]
        assert int(context["amount"]) == _AMOUNT
        assert context["destinationAccount"] == _DESTINATION
        assert item["createdAt"]

        # ttl ≈ now + OTP_TTL (300s), allowing for clock drift across the call.
        ttl = int(item["ttl"])
        assert before + OTP_TTL <= ttl <= after + OTP_TTL + 1

    def test_executionarn_defaults_to_empty_when_absent(
        self, otp_table, pinpoint_spy
    ) -> None:
        event = _event()
        del event["executionArn"]

        result = handler.handler(event)

        assert result == {"ok": True}
        item = otp_table.get_item(Key={"pk": _PHONE}).get("Item")
        assert item is not None
        assert item["executionArn"] == ""


# ---------------------------------------------------------------------------
# 2. SMS identifies the operation: amount + masked destination + code (Req 16.2)
# ---------------------------------------------------------------------------


class TestSmsContent:
    def test_sms_contains_amount_masked_destination_and_stored_code(
        self, otp_table, pinpoint_spy
    ) -> None:
        handler.handler(_event())

        # The exact code that was persisted must be the one texted to the client.
        stored_code = otp_table.get_item(Key={"pk": _PHONE})["Item"]["code"]

        call = pinpoint_spy.last
        assert call["ApplicationId"] == _APP_ID

        addresses = call["MessageRequest"]["Addresses"]
        assert addresses == {_PHONE: {"ChannelType": "SMS"}}

        sms = _sms_message(call)
        assert sms["MessageType"] == handler.SMS_MESSAGE_TYPE == "TRANSACTIONAL"

        body = sms["Body"]
        # COP-formatted amount, masked destination, and the stored code appear.
        assert format_cop(_AMOUNT) in body  # "$150.000,00"
        assert mask_account(_DESTINATION) in body  # "******3076"
        assert stored_code in body

        # Privacy: the unmasked destination account must not leak into the SMS.
        assert _DESTINATION not in body

    def test_optional_origination_number_attached_when_configured(
        self, otp_table, pinpoint_spy, monkeypatch
    ) -> None:
        monkeypatch.setenv("PINPOINT_ORIGINATION_NUMBER", "+15551234567")

        handler.handler(_event())

        sms = _sms_message(pinpoint_spy.last)
        assert sms["OriginationNumber"] == "+15551234567"


# ---------------------------------------------------------------------------
# 3. whatsapp: prefix normalized to bare E.164 partition key
# ---------------------------------------------------------------------------


class TestPhoneNormalization:
    def test_whatsapp_prefixed_phone_stored_under_bare_e164(
        self, otp_table, pinpoint_spy
    ) -> None:
        handler.handler(_event(phoneNumber=f"whatsapp:{_PHONE}"))

        # Stored under the bare number, not the whatsapp:-prefixed value.
        assert otp_table.get_item(Key={"pk": _PHONE}).get("Item") is not None
        assert (
            otp_table.get_item(Key={"pk": f"whatsapp:{_PHONE}"}).get("Item") is None
        )

        # The SMS is addressed to the bare E.164 number as well.
        addresses = pinpoint_spy.last["MessageRequest"]["Addresses"]
        assert _PHONE in addresses


# ---------------------------------------------------------------------------
# 4. Missing phoneNumber / taskToken → ValueError
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_phone_number_raises_and_writes_nothing(
        self, otp_table, pinpoint_spy
    ) -> None:
        event = _event()
        del event["phoneNumber"]

        with pytest.raises(ValueError, match="phoneNumber"):
            handler.handler(event)

        # Nothing persisted, nothing sent.
        assert otp_table.get_item(Key={"pk": _PHONE}).get("Item") is None
        assert pinpoint_spy.calls == []

    def test_empty_phone_number_raises(self, otp_table, pinpoint_spy) -> None:
        with pytest.raises(ValueError, match="phoneNumber"):
            handler.handler(_event(phoneNumber=""))

    def test_missing_task_token_raises_and_writes_nothing(
        self, otp_table, pinpoint_spy
    ) -> None:
        event = _event()
        del event["taskToken"]

        with pytest.raises(ValueError, match="taskToken"):
            handler.handler(event)

        assert otp_table.get_item(Key={"pk": _PHONE}).get("Item") is None
        assert pinpoint_spy.calls == []
