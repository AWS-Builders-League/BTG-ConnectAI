"""Property-based tests for the Message_Processor OTP callback flow.

Covers the OTP-callback properties from the design document and Task 9.2:

    * Property 17 — OTP Expiry        (Validates: Requirement 16.6)
    * Property 18 — Brute Force Block (Validates: Requirement 16.7)

What is under test
------------------
``otp_callback.py`` drives the WhatsApp side of the BRE-B transfer OTP flow. The
two properties exercised here are:

* **Property 17 (OTP Expiry, Req 16.6):** an OTP record whose ``ttl`` is in the
  past must be treated as expired. ``get_pending_otp`` returns ``None`` for it,
  and ``validate_and_callback`` ignores it — it never resumes the paused Step
  Functions workflow (no ``send_task_success`` / ``send_task_failure``). Step
  Functions owns the timeout via ``HeartbeatSeconds``.

* **Property 18 (Brute Force Block, Req 16.7):** starting from a fresh active
  OTP record (``attempts == 0``, future ``ttl``, known code), three consecutive
  *incorrect* codes block the workflow. The 1st and 2nd wrong codes increment
  ``attempts`` and do NOT fail the task; the 3rd wrong code calls
  ``send_task_failure(taskToken, error="OTPBlockedError")`` exactly once and
  deletes the OTP record.

Mocking strategy
----------------
DynamoDB: ``otp_callback.py`` builds its boto3 resource at import time
(``_dynamodb = boto3.resource("dynamodb")``). Each Hypothesis example runs inside
its own ``mock_aws()`` context where we rebind ``otp_callback._dynamodb`` to a
resource created *inside* the mock and (re)create the OTP_Store table. This makes
every example fully isolated (clean table) and immune to import ordering.

Step Functions: ``send_task_success`` / ``send_task_failure`` require a real
running execution + live task token, which moto cannot provide. We therefore
replace ``otp_callback._stepfunctions`` with a lightweight **spy** that records
every call's kwargs, and restore the original client afterwards.

Twilio: the retry message is sent via a lazy ``from .messaging import
send_twilio_message`` inside ``validate_and_callback``. We monkeypatch
``lambdas.message_processor.messaging.send_twilio_message`` with a no-op spy so
the 1st/2nd wrong-code path needs no Twilio credentials or network.

Per-example isolation for Property 18 uses a unique phone number per example
(drawn digits) plus the clean per-example table, and ``max_examples`` is kept
modest (~30) because each example performs several DynamoDB round-trips.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

# --- Force a fake AWS environment BEFORE importing the module under test. -----
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

OTP_TABLE_NAME = "OTP_Store-test"
os.environ["OTP_TABLE_NAME"] = OTP_TABLE_NAME

import boto3  # noqa: E402
import pytest  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from moto import mock_aws  # noqa: E402

from lambdas.message_processor import messaging  # noqa: E402
from lambdas.message_processor import otp_callback  # noqa: E402

_MAX_EXAMPLES = 30

# Fixed, known 6-digit OTP code used as the "correct" code in Property 18.
_STORED_CODE = "123456"

# A future epoch far enough ahead that the record is always active during a test.
_FUTURE_TTL = otp_callback._now_epoch() + 10_000


class _StepFunctionsSpy:
    """Records send_task_success / send_task_failure calls instead of hitting AWS.

    moto cannot satisfy the waitForTaskToken callback APIs (they need a live
    execution + real task token), so we substitute this spy for
    ``otp_callback._stepfunctions`` and assert on the captured kwargs.
    """

    def __init__(self) -> None:
        self.success_calls: list[dict] = []
        self.failure_calls: list[dict] = []

    def send_task_success(self, **kwargs) -> dict:
        self.success_calls.append(kwargs)
        return {}

    def send_task_failure(self, **kwargs) -> dict:
        self.failure_calls.append(kwargs)
        return {}


def _create_otp_table(dynamodb) -> None:
    """Create the OTP_Store table (``pk`` String partition key)."""
    dynamodb.create_table(
        TableName=OTP_TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@contextmanager
def _otp_env() -> Iterator[_StepFunctionsSpy]:
    """Per-example environment: clean mocked OTP_Store + Step Functions spy.

    Rebinds ``otp_callback._dynamodb`` to a resource built inside ``mock_aws``
    and ``otp_callback._stepfunctions`` to a fresh spy, restoring both on exit.
    """
    with mock_aws():
        original_dynamodb = otp_callback._dynamodb
        original_sf = otp_callback._stepfunctions
        spy = _StepFunctionsSpy()
        otp_callback._dynamodb = boto3.resource("dynamodb")
        otp_callback._stepfunctions = spy
        _create_otp_table(otp_callback._dynamodb)
        try:
            yield spy
        finally:
            otp_callback._dynamodb = original_dynamodb
            otp_callback._stepfunctions = original_sf


def _put_otp_record(phone: str, *, code: str, ttl: float, attempts: int = 0) -> None:
    """Write a single OTP_Store item for ``phone`` (bare E.164 partition key)."""
    otp_callback._get_otp_table().put_item(
        Item={
            "pk": phone,
            "code": code,
            "taskToken": f"token-{phone}",
            "executionArn": f"arn:aws:states:us-east-1:123456789012:execution:sm:{phone}",
            "attempts": attempts,
            "transferContext": {"amount": 100000, "destination": "2009876543"},
            "createdAt": "2024-01-01T00:00:00+00:00",
            "ttl": int(ttl),
        }
    )


def _read_attempts(phone: str) -> int | None:
    """Return the current ``attempts`` for ``phone``, or None if the item is gone."""
    item = otp_callback._get_otp_table().get_item(Key={"pk": phone}).get("Item")
    if item is None:
        return None
    return int(item["attempts"])


# Bare E.164-ish Colombian phone numbers (unique digits → per-example isolation).
_phone_strategy = st.builds(
    lambda digits: f"+57{digits}",
    st.text(alphabet="0123456789", min_size=8, max_size=10),
)

# Past TTL offsets: 1s .. 100000s before "now".
_past_offset_strategy = st.integers(min_value=1, max_value=100_000)

# Wrong codes: any 6-char digit string that is NOT the stored code.
_wrong_code_strategy = st.text(alphabet="0123456789", min_size=1, max_size=6).filter(
    lambda c: c.strip() != _STORED_CODE
)


# ---------------------------------------------------------------------------
# Property 17: OTP Expiry
# Validates: Requirement 16.6
# ---------------------------------------------------------------------------
@pytest.mark.property
@settings(
    max_examples=_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(phone=_phone_strategy, past_offset=_past_offset_strategy, code=st.text(max_size=6))
def test_expired_otp_is_ignored(phone: str, past_offset: int, code: str) -> None:
    """An OTP record whose ttl is in the past is treated as expired.

    ``get_pending_otp`` returns ``None`` and ``validate_and_callback`` never
    resumes the workflow (no send_task_success / send_task_failure). (Req 16.6 /
    Property 17.)
    """
    with _otp_env() as spy:
        expired_ttl = otp_callback._now_epoch() - past_offset
        _put_otp_record(phone, code=_STORED_CODE, ttl=expired_ttl, attempts=0)

        # The expired record must be reported as absent.
        assert otp_callback.get_pending_otp(phone) is None

        # Validating against it (any submitted code) must be a no-op callback-wise.
        otp_callback.validate_and_callback(phone, code)

        assert spy.success_calls == []
        assert spy.failure_calls == []


# ---------------------------------------------------------------------------
# Property 18: Brute Force Block
# Validates: Requirement 16.7
# ---------------------------------------------------------------------------
@pytest.mark.property
@settings(
    max_examples=_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(phone=_phone_strategy, wrong_code=_wrong_code_strategy)
def test_three_failed_attempts_block_workflow(
    phone: str, wrong_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three consecutive wrong codes block the workflow on the 3rd attempt.

    Attempts 1 and 2 increment ``attempts`` and do NOT fail the task; attempt 3
    calls ``send_task_failure(taskToken, error="OTPBlockedError")`` exactly once
    and deletes the OTP record. (Req 16.7 / Property 18.)
    """
    # Stub the lazy Twilio retry message (1st/2nd wrong-code path).
    sent_messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        messaging,
        "send_twilio_message",
        lambda to, body: sent_messages.append((to, body)),
    )

    with _otp_env() as spy:
        _put_otp_record(phone, code=_STORED_CODE, ttl=_FUTURE_TTL, attempts=0)

        # --- Attempt 1: wrong code, below threshold -> increment, no failure ---
        otp_callback.validate_and_callback(phone, wrong_code)
        assert spy.failure_calls == []
        assert spy.success_calls == []
        assert _read_attempts(phone) == 1

        # --- Attempt 2: wrong code, below threshold -> increment, no failure ---
        otp_callback.validate_and_callback(phone, wrong_code)
        assert spy.failure_calls == []
        assert spy.success_calls == []
        assert _read_attempts(phone) == 2

        # --- Attempt 3: wrong code, final -> block + delete record ---
        otp_callback.validate_and_callback(phone, wrong_code)

        assert spy.success_calls == []
        assert len(spy.failure_calls) == 1
        failure = spy.failure_calls[0]
        assert failure["error"] == otp_callback.OTP_BLOCKED_ERROR == "OTPBlockedError"
        assert failure["taskToken"] == f"token-{phone}"

        # Record deleted after the block.
        assert _read_attempts(phone) is None

        # Two retry messages were sent (one per non-blocking wrong code).
        assert len(sent_messages) == 2
