"""Property-based tests for the transfer-breb validate/execute/initiator flow.

Covers the transfer-breb properties from the design document and Task 7.4:

    * Property 11 — Valid Transfer Produces Receipt   (Validates: Requirement 8.3)
    * Property 12 — Insufficient Funds Rejection       (Validates: Requirement 8.10)
    * Property 19 — Idempotent StartExecution          (Validates: Requirement 18.7)

Testing approach
----------------
``validate.handler`` and ``execute.handler`` operate over the inline Mock_Core
dataset (``transfer_breb.mock_data``) — no AWS calls — so Properties 11 and 12
need no mocking. We set ``AWS_DEFAULT_REGION`` before importing the modules as a
defensive measure (``initiator`` constructs a boto3 client at import time).

Balance-mutation isolation (critical):
``execute.handler`` mutates the module-level Mock_Core balances **in place**
(``available_balance``/``total_balance`` of the source and destination
accounts). Because Hypothesis runs many examples inside a single test function,
a leaked mutation from one example would corrupt the next (and break the
"balance unchanged" guarantee of Property 12). To keep examples independent we
snapshot **every** product's balances before any mutation and restore them in a
``finally`` block (see ``_balances_preserved``). This is simpler and faster than
reloading the module and is exact (it restores the precise float values).

Property 19 (idempotency) uses moto's mocked Step Functions. NOTE on moto: the
installed moto (5.x) does **not** raise ``ExecutionAlreadyExists`` for a
duplicate execution ``name``; instead it deduplicates by name so a second
``StartExecution`` with the same name yields the *same* execution ARN and
``list_executions`` reports exactly **one** execution. We therefore verify the
end-to-end idempotency invariant ("same correlationId → one execution, both
calls return STARTED") against moto, AND add a dedicated test that monkeypatches
the client to raise ``ExecutionAlreadyExists`` on the second call to prove the
handler's real-AWS idempotency branch (catch → derive ARN → return STARTED).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
import pytest  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from moto import mock_aws  # noqa: E402

from shared.errors import InsufficientFundsError  # noqa: E402
from shared.masking import mask_account  # noqa: E402

from lambdas.transfer_breb import execute, initiator, validate  # noqa: E402
from lambdas.transfer_breb.mock_data import (  # noqa: E402
    MOCK_CLIENTS,
    VALID_DESTINATION_ACCOUNTS,
)

_MAX_EXAMPLES = 100

# Sorted, deterministic list of valid destination account numbers to sample.
_VALID_DESTINATIONS = sorted(VALID_DESTINATION_ACCOUNTS)

# Concepts include the empty string and unicode (Spanish) text.
_concepts = st.text(max_size=40)


class _LambdaContext:
    """Minimal Lambda context object (handlers only use it for logging)."""

    function_name = "transfer-breb"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:transfer-breb"
    aws_request_id = "test-request-id"


_CONTEXT = _LambdaContext()


@contextmanager
def _balances_preserved() -> Iterator[None]:
    """Snapshot every Mock_Core product balance and restore it on exit.

    Guarantees example independence despite ``execute.handler`` mutating the
    module-level dataset in place.
    """
    snapshot = [
        (product, product["available_balance"], product["total_balance"])
        for client in MOCK_CLIENTS
        for product in client["products"]
    ]
    try:
        yield
    finally:
        for product, available, total in snapshot:
            product["available_balance"] = available
            product["total_balance"] = total


# ---------------------------------------------------------------------------
# Property 11: Valid Transfer Produces Receipt
# Validates: Requirement 8.3
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_valid_transfer_produces_receipt(data: st.DataObject) -> None:
    """For a valid source/destination and an amount in (0, available_balance],
    ``validate`` accepts the transfer and ``execute`` produces a COMPLETED
    receipt with the mandated fields (Req 8.3 / 8.12)."""
    client = data.draw(st.sampled_from(MOCK_CLIENTS))
    source = data.draw(st.sampled_from(client["products"]))
    destination = data.draw(st.sampled_from(_VALID_DESTINATIONS))
    concept = data.draw(_concepts)

    balance = source["available_balance"]
    amount = data.draw(
        st.floats(
            min_value=0.01,
            max_value=balance,
            allow_nan=False,
            allow_infinity=False,
        )
    )

    phone = client["phone_number"]
    source_account = source["account_number"]

    event = {
        "sourceAccount": source_account,
        "destinationAccount": destination,
        "amount": amount,
        "concept": concept,
        "phoneNumber": phone,
    }

    # validate must accept the request and pass the data through unchanged.
    validated = validate.handler(dict(event), _CONTEXT)
    assert validated["valid"] is True
    assert validated["amount"] == amount
    assert validated["sourceAccount"] == source_account
    assert validated["destinationAccount"] == destination

    # execute mutates Mock_Core in place — isolate via snapshot/restore.
    with _balances_preserved():
        result = execute.handler(dict(validated), _CONTEXT)

    receipt = result["receipt"]
    assert receipt["status"] == "COMPLETED"
    assert receipt["currency"] == "COP"
    assert isinstance(receipt["transactionId"], str) and receipt["transactionId"]
    assert receipt["transactionId"].startswith("TRX-")
    assert receipt["sourceAccount"] == mask_account(source_account)
    assert receipt["destinationAccount"] == mask_account(destination)
    assert receipt["amount"] == amount
    assert receipt["concept"] == concept
    # executedAt is a valid ISO 8601 timestamp.
    datetime.fromisoformat(receipt["executedAt"])


# ---------------------------------------------------------------------------
# Property 12: Insufficient Funds Rejection
# Validates: Requirement 8.10
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_insufficient_funds_rejected_and_balance_unchanged(data: st.DataObject) -> None:
    """For an amount strictly greater than the source's available balance,
    ``validate`` raises ``InsufficientFundsError`` and does NOT mutate the
    source account balance (Req 8.10)."""
    client = data.draw(st.sampled_from(MOCK_CLIENTS))
    source = data.draw(st.sampled_from(client["products"]))
    destination = data.draw(st.sampled_from(_VALID_DESTINATIONS))
    concept = data.draw(_concepts)

    balance = source["available_balance"]
    total_before = source["total_balance"]
    amount = data.draw(
        st.floats(
            min_value=balance,
            exclude_min=True,
            max_value=balance * 2 + 1_000_000.0,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    # exclude_min guarantees amount > balance, but guard against float rounding.
    assert amount > balance

    event = {
        "sourceAccount": source["account_number"],
        "destinationAccount": destination,
        "amount": amount,
        "concept": concept,
        "phoneNumber": client["phone_number"],
    }

    with pytest.raises(InsufficientFundsError):
        validate.handler(dict(event), _CONTEXT)

    # validate must never mutate balances.
    assert source["available_balance"] == balance
    assert source["total_balance"] == total_before


# ---------------------------------------------------------------------------
# Property 19: Idempotent StartExecution
# Validates: Requirement 18.7
# ---------------------------------------------------------------------------

_TRIVIAL_ASL = json.dumps(
    {
        "Comment": "Trivial state machine for idempotency tests",
        "StartAt": "Done",
        "States": {"Done": {"Type": "Pass", "End": True}},
    }
)

_REGION = "us-east-1"


@contextmanager
def _mocked_state_machine() -> Iterator[tuple[object, str]]:
    """Create a moto Step Functions state machine and rebind the initiator.

    Yields ``(stepfunctions_client, state_machine_arn)`` and restores both the
    initiator's module-level client and ``STATE_MACHINE_ARN`` env var on exit.
    """
    with mock_aws():
        iam = boto3.client("iam", region_name=_REGION)
        role_arn = iam.create_role(
            RoleName="transfer-breb-sfn-role",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "states.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )["Role"]["Arn"]

        sf = boto3.client("stepfunctions", region_name=_REGION)
        state_machine_arn = sf.create_state_machine(
            name="TransferBrebStateMachine",
            definition=_TRIVIAL_ASL,
            roleArn=role_arn,
        )["stateMachineArn"]

        original_client = initiator._stepfunctions
        original_arn = os.environ.get("STATE_MACHINE_ARN")
        initiator._stepfunctions = sf
        os.environ["STATE_MACHINE_ARN"] = state_machine_arn
        try:
            yield sf, state_machine_arn
        finally:
            initiator._stepfunctions = original_client
            if original_arn is None:
                os.environ.pop("STATE_MACHINE_ARN", None)
            else:
                os.environ["STATE_MACHINE_ARN"] = original_arn


def _transfer_event(correlation_id: str) -> dict:
    return {
        "correlationId": correlation_id,
        "phoneNumber": "+573001234567",
        "sourceAccount": "1001234568",
        "destinationAccount": "2009876543",
        "amount": 100_000.0,
        "concept": "prueba",
    }


@pytest.mark.property
@settings(max_examples=25, deadline=None)
@given(correlation_id=st.uuids().map(str))
def test_idempotent_start_execution_single_execution(correlation_id: str) -> None:
    """Calling the initiator twice with the same correlationId results in
    exactly one Step Functions execution, and both calls return STARTED.

    moto note: moto 5.x does not raise ExecutionAlreadyExists for a duplicate
    name; it deduplicates by name, so the second StartExecution returns the same
    execution ARN and only one execution exists. The idempotency invariant
    (one execution per correlationId) holds either way.
    """
    with _mocked_state_machine() as (sf, state_machine_arn):
        event = _transfer_event(correlation_id)

        first = initiator.handler(dict(event), _CONTEXT)
        second = initiator.handler(dict(event), _CONTEXT)

        assert first["status"] == "STARTED"
        assert second["status"] == "STARTED"

        executions = sf.list_executions(stateMachineArn=state_machine_arn)["executions"]
        # Same correlationId (execution name) → exactly one execution.
        assert len(executions) == 1
        assert executions[0]["name"] == correlation_id


@pytest.mark.property
def test_idempotent_start_execution_handles_already_exists() -> None:
    """The handler's real-AWS idempotency branch: when StartExecution raises
    ExecutionAlreadyExists on a duplicate name, the handler catches it, derives
    the existing execution ARN, and still returns STARTED (Req 18.7).

    This explicitly exercises the catch branch that moto's name-deduplication
    does not trigger on its own.
    """
    correlation_id = "fixed-correlation-id-19"
    with _mocked_state_machine() as (sf, state_machine_arn):
        event = _transfer_event(correlation_id)

        first = initiator.handler(dict(event), _CONTEXT)
        assert first["status"] == "STARTED"

        # Force the duplicate-name rejection that real Step Functions raises.
        def _raise_already_exists(**_kwargs):
            raise sf.exceptions.ExecutionAlreadyExists(
                error_response={
                    "Error": {
                        "Code": "ExecutionAlreadyExists",
                        "Message": "Execution already exists",
                    }
                },
                operation_name="StartExecution",
            )

        original_start = sf.start_execution
        sf.start_execution = _raise_already_exists  # type: ignore[method-assign]
        try:
            second = initiator.handler(dict(event), _CONTEXT)
        finally:
            sf.start_execution = original_start  # type: ignore[method-assign]

        assert second["status"] == "STARTED"
        # The handler reconstructs the existing execution ARN deterministically.
        expected_arn = (
            state_machine_arn.replace(":stateMachine:", ":execution:", 1)
            + f":{correlation_id}"
        )
        assert second["executionArn"] == expected_arn
        assert first["executionArn"] == expected_arn

        # Still exactly one execution.
        executions = sf.list_executions(stateMachineArn=state_machine_arn)["executions"]
        assert len(executions) == 1
