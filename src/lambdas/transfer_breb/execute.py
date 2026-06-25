"""transfer-breb ``ExecuteTransfer`` state task (Requirements 8.11, 8.12).

This Lambda is invoked by the ``ExecuteTransfer`` state of the
``TransferBrebStateMachine`` after the OTP has been validated. It applies the
(mock) balance movement on Mock_Core and produces the transfer receipt that
downstream states deliver to the client (WhatsApp) and publish to the
email/SMS notification queues.

MVP balance semantics (Requirement 8.11): Mock_Core is an in-memory constant, so
the balance debit/credit only lives for this invocation — there is no
cross-invocation persistence and therefore no compensation step is needed if a
later state fails (the source is only ever debited on a successful execution).
In production these mutations become real core calls and a compensating
transaction would be required.

The receipt fields are mandated by Requirement 8.12: ``transactionId``,
``sourceAccount`` (masked), ``destinationAccount`` (masked), ``amount``,
``currency`` ("COP"), ``concept``, ``executedAt`` (ISO 8601) and ``status``
("COMPLETED").

This Lambda runs INSIDE the VPC (banking domain) in production; no code change
is required for that.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.logger import get_logger
from shared.masking import mask_account, mask_phone

from .mock_data import find_account_by_number

logger = get_logger("transfer-breb-execute")


def _generate_transaction_id(now: datetime) -> str:
    """Build a unique transaction id: ``TRX-<epoch>-<6 hex>``."""
    return f"TRX-{int(now.timestamp())}-{uuid.uuid4().hex[:6]}"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Execute the (mock) transfer and produce the receipt.

    Args:
        event: The validated transfer data forwarded by the state machine.
            Expected keys: ``sourceAccount``, ``destinationAccount``,
            ``amount``, ``concept`` (optional), ``phoneNumber``.
        context: Lambda context object (unused beyond logging).

    Returns:
        ``{"receipt": {...}}`` where the receipt satisfies Requirement 8.12.
    """
    source_account = event["sourceAccount"]
    destination_account = event["destinationAccount"]
    amount = event["amount"]
    concept = event.get("concept", "")
    phone_number = event["phoneNumber"]
    correlation_id = event.get("correlationId")

    if correlation_id:
        logger.append_keys(correlation_id=correlation_id)

    logger.info(
        "executing transfer",
        extra={
            "phone": mask_phone(phone_number),
            "source": mask_account(source_account),
            "destination": mask_account(destination_account),
            "amount": amount,
        },
    )

    # Apply the (mock, per-invocation) balance movement on the source account.
    # The destination is resolved by the real core via the BRE-B key in production;
    # in the MVP we only debit the source.
    source_acct = find_account_by_number(phone_number, source_account)
    if source_acct is not None:
        source_acct["available_balance"] -= amount
        source_acct["total_balance"] -= amount

    now = datetime.now(timezone.utc)
    receipt = {
        "transactionId": _generate_transaction_id(now),
        "sourceAccount": mask_account(source_account),
        "destinationAccount": mask_account(destination_account),
        "amount": amount,
        "currency": "COP",
        "concept": concept,
        "executedAt": now.isoformat(),
        "status": "COMPLETED",
    }

    logger.info(
        "transfer executed",
        extra={"transactionId": receipt["transactionId"]},
    )

    return {"receipt": receipt}


__all__ = ["handler"]
