"""transfer-breb initiator — Strands Agent tool (Requirements 8.3, 18.7).

This Lambda is the ``initiate-transfer-breb`` tool invoked by the Strands Agent
once the client has explicitly confirmed the operation. It fires the
``TransferBrebStateMachine`` via ``StartExecution`` and returns *immediately* —
it never waits for the OTP callback (that is handled asynchronously by the state
machine + Message_Processor).

Idempotency (Requirement 18.7 / Property 19): the execution ``name`` is set to
the session ``correlationId``. AWS Step Functions rejects a second
``StartExecution`` with the same ``name`` (within the dedup window) by raising
``ExecutionAlreadyExists``, so a retried/duplicated tool invocation for the same
correlationId results in exactly one execution. This handler treats that
rejection as success and still returns ``STARTED``, reconstructing the existing
execution ARN from the state machine ARN and the name.

This Lambda runs OUTSIDE the VPC (it only needs the public Step Functions API,
secured by IAM).

Environment variables:
* ``STATE_MACHINE_ARN`` — ARN of the ``TransferBrebStateMachine`` (resolved from
  the cross-stack contract). Read lazily so import-time has no AWS dependency.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from shared.logger import get_logger
from shared.masking import mask_account, mask_phone

logger = get_logger("transfer-breb-initiator")

# Module-level client reused across warm invocations.
_stepfunctions = boto3.client("stepfunctions")


def _derive_execution_arn(state_machine_arn: str, name: str) -> str:
    """Reconstruct an execution ARN from the state machine ARN and exec name.

    Step Functions execution ARNs follow
    ``arn:aws:states:<region>:<account>:execution:<stateMachineName>:<name>``,
    which is the state machine ARN with ``:stateMachine:`` swapped for
    ``:execution:`` and the execution name appended. Used to return a stable ARN
    on the ``ExecutionAlreadyExists`` (idempotent) path where the API does not
    hand one back.
    """
    return state_machine_arn.replace(":stateMachine:", ":execution:", 1) + f":{name}"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Start the BRE-B transfer state machine and return immediately.

    Args:
        event: Transfer data from the Strands Agent tool. Expected keys:
            ``sourceAccount``, ``destinationAccount``, ``amount``, ``concept``
            (optional), ``phoneNumber`` and ``correlationId`` (used as the
            idempotent execution name).
        context: Lambda context object (unused beyond logging).

    Returns:
        ``{"executionArn": <arn>, "status": "STARTED"}`` — returned both on a
        fresh start and on the idempotent ``ExecutionAlreadyExists`` path.
    """
    correlation_id = event["correlationId"]
    logger.append_keys(correlation_id=correlation_id)

    state_machine_arn = os.environ["STATE_MACHINE_ARN"]

    transfer_input: dict[str, Any] = {
        "phoneNumber": event["phoneNumber"],
        "sourceAccount": event["sourceAccount"],
        "destinationAccount": event["destinationAccount"],
        "amount": event["amount"],
        "concept": event.get("concept", ""),
        "correlationId": correlation_id,
    }
    if "sessionId" in event:
        transfer_input["sessionId"] = event["sessionId"]

    logger.info(
        "starting transfer state machine",
        extra={
            "phone": mask_phone(event["phoneNumber"]),
            "source": mask_account(event["sourceAccount"]),
            "destination": mask_account(event["destinationAccount"]),
            "amount": event["amount"],
        },
    )

    try:
        response = _stepfunctions.start_execution(
            stateMachineArn=state_machine_arn,
            name=correlation_id,
            input=json.dumps(transfer_input),
        )
        execution_arn = response["executionArn"]
        logger.info("transfer execution started")
    except _stepfunctions.exceptions.ExecutionAlreadyExists:
        # Idempotency (Req 18.7 / Property 19): a duplicate StartExecution with
        # the same name is expected and harmless — exactly one execution runs.
        execution_arn = _derive_execution_arn(state_machine_arn, correlation_id)
        logger.info("execution already exists; idempotent start, returning STARTED")

    return {"executionArn": execution_arn, "status": "STARTED"}


__all__ = ["handler"]
