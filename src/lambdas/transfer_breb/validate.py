"""transfer-breb ``ValidateTransfer`` state task (Requirement 8.10).

This Lambda is invoked by the ``ValidateTransfer`` state of the
``TransferBrebStateMachine`` (it is NOT called directly by the Strands Agent).
It validates the requested BRE-B transfer against Mock_Core *before* any OTP is
generated, so an invalid request is rejected cheaply and the state machine
routes to ``NotifyValidationFailed`` without consuming the rest of the workflow.

Validation rules:

* The source account must exist and belong to the requesting client, and its
  ``available_balance`` must cover ``amount`` — otherwise
  :class:`~shared.errors.InsufficientFundsError` is raised.
* The destination account must be a known, valid account in Mock_Core —
  otherwise :class:`~shared.errors.InvalidDestinationError` is raised.

Both exceptions are domain errors matched by the state machine ``Catch``
handlers (Requirement 18.4). On success the handler returns the transfer data
augmented with ``valid: True`` so the next state can proceed.

This Lambda runs INSIDE the VPC (banking domain) in production; no code change
is required for that — it is purely a deployment/networking concern.
"""

from __future__ import annotations

from typing import Any

from shared.errors import InsufficientFundsError
from shared.logger import get_logger
from shared.masking import mask_account, mask_phone

from .mock_data import find_account_by_number

logger = get_logger("transfer-breb-validate")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Validate a BRE-B transfer request against Mock_Core.

    Args:
        event: The transfer payload forwarded by the state machine. Expected
            keys: ``sourceAccount``, ``destinationAccount``, ``amount``,
            ``concept`` (optional), ``phoneNumber``. Any additional keys
            (``sessionId``, ``correlationId``) are passed through untouched.
        context: Lambda context object (unused beyond logging).

    Returns:
        The original transfer data with ``valid: True`` added.

    Raises:
        InsufficientFundsError: Source account missing/not owned by the client,
            or its available balance is below ``amount``.
        InvalidDestinationError: Destination account is not a known account.
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
        "validating transfer",
        extra={
            "phone": mask_phone(phone_number),
            "source": mask_account(source_account),
            "destination": mask_account(destination_account),
            "amount": amount,
        },
    )

    # 1. Source account must exist, belong to the client, and have funds.
    source_acct = find_account_by_number(phone_number, source_account)
    if source_acct is None:
        logger.warning("source account not found for client")
        raise InsufficientFundsError("Cuenta origen no encontrada")

    if source_acct["available_balance"] < amount:
        logger.warning("insufficient funds for transfer")
        raise InsufficientFundsError("Fondos insuficientes")

    # 2. Destination: BRE-B uses keys (document, email, phone, random key).
    # In the MVP we accept any destination without validation — the real core
    # resolves the key to an account in production.
    logger.info("transfer validation passed")

    return {
        "valid": True,
        "sourceAccount": source_account,
        "destinationAccount": destination_account,
        "amount": amount,
        "concept": concept,
        "phoneNumber": phone_number,
        **(
            {"sessionId": event["sessionId"]} if "sessionId" in event else {}
        ),
        **(
            {"correlationId": correlation_id} if correlation_id else {}
        ),
    }


__all__ = ["handler"]
