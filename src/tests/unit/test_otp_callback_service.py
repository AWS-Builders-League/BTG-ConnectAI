"""Unit tests for the OTP callback module (Task 9.3, Requirements 16.4–16.7).

These example-based tests pin down the WhatsApp side of the BRE-B transfer OTP
flow implemented in :mod:`lambdas.message_processor.otp_callback`. The
Step Functions ``TransferBrebStateMachine`` pauses at ``GenerateOTP`` using the
``waitForTaskToken`` pattern and persists an :class:`~shared.types.OTPRecord` in
the ``OTP_Store`` table; this module resumes (or blocks) that workflow based on
the code the client replies with.

What is exercised
-----------------
* **Valid code (Req 16.5)** — ``send_task_success(taskToken, {"valid": true})``
  resumes the workflow and the OTP record is deleted.
* **Incorrect code below the block threshold (Req 16.6)** — ``attempts`` is
  incremented and a "Código incorrecto" retry message is sent via Twilio,
  without resuming the workflow; the record is not deleted.
* **Third failed attempt (Req 16.7 / Property 18)** — with two prior failures
  already recorded, a wrong code triggers
  ``send_task_failure(taskToken, "OTPBlockedError")`` and deletes the record;
  no retry message is sent.
* **Expired record (Req 16.4 / Property 17)** — :func:`get_pending_otp` returns
  ``None`` and :func:`validate_and_callback` ignores the message (no callback,
  no delete by the callback).
* **No record** — nothing happens.

Mocking approach
----------------
The module creates a module-level ``boto3.resource("dynamodb")`` and
``boto3.client("stepfunctions")`` at import time. Under ``mock_aws`` we
**rebind ``otp_callback._dynamodb``** to a resource created inside the mock and
create the ``OTP_Store`` table (``pk`` String HASH). The Step Functions client
is replaced with a lightweight spy that records ``send_task_success`` /
``send_task_failure`` kwargs (moto's Step Functions support does not implement
the task-token callbacks). The lazily imported ``messaging.send_twilio_message``
is monkeypatched on the ``messaging`` module so no Twilio access happens.
``OTP_TABLE_NAME`` / region / dummy credentials are set in the environment.
"""

from __future__ import annotations

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.message_processor import messaging, otp_callback

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_TABLE_NAME = "BTGConnectAI-sandbox-OTPStore"
_PHONE = "+573001234567"
_CODE = "123456"
_TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAAtest-task-token"


# ---------------------------------------------------------------------------
# Spies / fixtures
# ---------------------------------------------------------------------------


class _StepFunctionsSpy:
    """Records ``send_task_success`` / ``send_task_failure`` invocations."""

    def __init__(self) -> None:
        self.success_calls: list[dict] = []
        self.failure_calls: list[dict] = []

    def send_task_success(self, **kwargs):
        self.success_calls.append(kwargs)
        return {}

    def send_task_failure(self, **kwargs):
        self.failure_calls.append(kwargs)
        return {}


class _Spy:
    """Callable that records its calls for assertions."""

    def __init__(self, return_value=None) -> None:
        self.return_value = return_value
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def otp_env(monkeypatch):
    """Wire a moto OTP_Store table, a Step Functions spy and a messaging spy.

    Yields ``(table, sfn_spy, send_spy)`` where ``table`` is the boto3 ``Table``
    resource so tests can seed and read back items, ``sfn_spy`` captures the
    Step Functions callbacks, and ``send_spy`` captures retry messages.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("OTP_TABLE_NAME", _TABLE_NAME)

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=_REGION)
        resource.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = resource.Table(_TABLE_NAME)

        sfn_spy = _StepFunctionsSpy()
        send_spy = _Spy()

        original_dynamodb = otp_callback._dynamodb
        original_sfn = otp_callback._stepfunctions
        otp_callback._dynamodb = resource
        otp_callback._stepfunctions = sfn_spy
        # The retry message is imported lazily via ``from .messaging import
        # send_twilio_message`` inside the function, so patching the attribute
        # on the messaging module is what the import resolves to.
        monkeypatch.setattr(messaging, "send_twilio_message", send_spy)

        try:
            yield table, sfn_spy, send_spy
        finally:
            otp_callback._dynamodb = original_dynamodb
            otp_callback._stepfunctions = original_sfn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_otp(
    table,
    *,
    phone: str = _PHONE,
    code: str = _CODE,
    attempts: int = 0,
    ttl_offset: int = 300,
    task_token: str = _TASK_TOKEN,
) -> None:
    """Insert an OTP_Store item. ``ttl_offset`` is seconds from now (negative = expired)."""
    table.put_item(
        Item={
            "pk": phone,
            "code": code,
            "taskToken": task_token,
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:Transfer:exec",
            "attempts": attempts,
            "transferContext": {"amount": 50000, "destination": "+573009998877"},
            "createdAt": "2025-01-01T00:00:00Z",
            "ttl": int(time.time()) + ttl_offset,
        }
    )


def _get_item(table, phone: str = _PHONE):
    return table.get_item(Key={"pk": phone}).get("Item")


# ---------------------------------------------------------------------------
# 1. Valid code → send_task_success + delete (Requirement 16.5)
# ---------------------------------------------------------------------------


class TestValidCode:
    def test_valid_code_resumes_workflow_and_deletes_record(self, otp_env) -> None:
        table, sfn_spy, send_spy = otp_env
        _seed_otp(table, attempts=0)

        otp_callback.validate_and_callback(_PHONE, _CODE)

        # Workflow resumed with the task token and {"valid": true} output.
        assert len(sfn_spy.success_calls) == 1
        call = sfn_spy.success_calls[0]
        assert call["taskToken"] == _TASK_TOKEN
        assert json.loads(call["output"]) == {"valid": True}
        # No failure callback.
        assert sfn_spy.failure_calls == []
        # The OTP record is deleted.
        assert _get_item(table) is None
        # No retry message on success.
        assert not send_spy.called

    def test_valid_code_with_prefixed_phone_and_whitespace(self, otp_env) -> None:
        table, sfn_spy, _ = otp_env
        _seed_otp(table, attempts=0)

        # whatsapp: prefix is stripped and surrounding whitespace ignored.
        otp_callback.validate_and_callback(f"whatsapp:{_PHONE}", f"  {_CODE}  ")

        assert len(sfn_spy.success_calls) == 1
        assert _get_item(table) is None


# ---------------------------------------------------------------------------
# 2 & 3. Incorrect code below threshold → attempts++ + retry (Requirement 16.6)
# ---------------------------------------------------------------------------


class TestIncorrectCodeRetry:
    def test_first_wrong_code_increments_attempts_and_sends_retry(self, otp_env) -> None:
        table, sfn_spy, send_spy = otp_env
        _seed_otp(table, attempts=0)

        otp_callback.validate_and_callback(_PHONE, "000000")

        # attempts incremented 0 -> 1.
        item = _get_item(table)
        assert item is not None
        assert int(item["attempts"]) == 1
        # Retry message sent to the bare phone with the incorrect-code text.
        assert send_spy.call_count == 1
        args, _ = send_spy.calls[0]
        assert args[0] == _PHONE
        assert args[1] == otp_callback.INCORRECT_CODE_MESSAGE
        # Workflow neither resumed nor failed; record not deleted.
        assert sfn_spy.success_calls == []
        assert sfn_spy.failure_calls == []

    def test_second_wrong_code_increments_to_two_still_not_blocked(self, otp_env) -> None:
        table, sfn_spy, send_spy = otp_env
        _seed_otp(table, attempts=1)

        otp_callback.validate_and_callback(_PHONE, "999999")

        # attempts incremented 1 -> 2 (still below the block threshold).
        item = _get_item(table)
        assert item is not None
        assert int(item["attempts"]) == 2
        assert send_spy.call_count == 1
        assert send_spy.calls[0][0][1] == otp_callback.INCORRECT_CODE_MESSAGE
        # No block on the 2nd wrong code.
        assert sfn_spy.failure_calls == []
        assert sfn_spy.success_calls == []


# ---------------------------------------------------------------------------
# 4. Third failed attempt → send_task_failure(OTPBlockedError) + delete (Req 16.7)
# ---------------------------------------------------------------------------


class TestThirdAttemptBlocks:
    def test_third_wrong_code_fails_task_and_deletes_record(self, otp_env) -> None:
        table, sfn_spy, send_spy = otp_env
        _seed_otp(table, attempts=2)

        otp_callback.validate_and_callback(_PHONE, "111111")

        # Workflow blocked via send_task_failure with the OTPBlockedError name.
        assert len(sfn_spy.failure_calls) == 1
        call = sfn_spy.failure_calls[0]
        assert call["taskToken"] == _TASK_TOKEN
        assert call["error"] == otp_callback.OTP_BLOCKED_ERROR == "OTPBlockedError"
        # Record deleted; no success callback; no retry message.
        assert _get_item(table) is None
        assert sfn_spy.success_calls == []
        assert not send_spy.called


# ---------------------------------------------------------------------------
# 5. Expired record → ignored (Requirement 16.4 / Property 17)
# ---------------------------------------------------------------------------


class TestExpiredRecord:
    def test_get_pending_otp_returns_none_for_expired(self, otp_env) -> None:
        table, _, _ = otp_env
        _seed_otp(table, ttl_offset=-10)

        assert otp_callback.get_pending_otp(_PHONE) is None

    def test_expired_record_is_ignored_by_callback(self, otp_env) -> None:
        table, sfn_spy, send_spy = otp_env
        _seed_otp(table, ttl_offset=-10)

        # No pre-fetched record → callback looks it up, finds it expired, ignores.
        otp_callback.validate_and_callback(_PHONE, _CODE)

        assert sfn_spy.success_calls == []
        assert sfn_spy.failure_calls == []
        assert not send_spy.called
        # The callback itself does not delete an expired record (Step Functions
        # owns the timeout) — the item may still exist.
        assert _get_item(table) is not None


# ---------------------------------------------------------------------------
# 6. No record → ignored
# ---------------------------------------------------------------------------


class TestNoRecord:
    def test_no_record_is_ignored(self, otp_env) -> None:
        _table, sfn_spy, send_spy = otp_env

        otp_callback.validate_and_callback(_PHONE, _CODE)

        assert sfn_spy.success_calls == []
        assert sfn_spy.failure_calls == []
        assert not send_spy.called
