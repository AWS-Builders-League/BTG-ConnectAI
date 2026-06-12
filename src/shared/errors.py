"""Domain error types for BTG ConnectAI.

These are the canonical, cross-Lambda domain exceptions raised by the banking
flows and caught by the ``TransferBrebStateMachine`` ``Catch`` handlers
(Requirement 18.4). The state machine matches on the *class name* of the raised
exception (e.g. ``InsufficientFundsError``), so the names defined here are part
of the contract between the Lambda tasks and the Amazon States Language
definition — do not rename them without updating the ASL.

* ``InsufficientFundsError`` — the source account does not exist or does not
  have enough available balance to cover the requested transfer. Raised by the
  ``ValidateTransfer`` state → routes to ``NotifyValidationFailed``
  (Requirement 8.10).
* ``InvalidDestinationError`` — the destination account is not a known/valid
  account. Raised by the ``ValidateTransfer`` state → routes to
  ``NotifyValidationFailed`` (Requirement 8.10).
* ``OTPBlockedError`` — the client exhausted the allowed OTP attempts (3 failed
  tries). Raised via Step Functions ``send_task_failure`` from the OTP callback
  handler → routes to ``NotifyOTPBlocked`` (Requirement 16.7).
"""

from __future__ import annotations


class InsufficientFundsError(Exception):
    """Source account is missing or lacks available balance for the transfer.

    Caught by the ``TransferBrebStateMachine`` and routed to
    ``NotifyValidationFailed`` without generating an OTP (Requirement 8.10).
    """


class InvalidDestinationError(Exception):
    """Destination account is not a known/valid account.

    Caught by the ``TransferBrebStateMachine`` and routed to
    ``NotifyValidationFailed`` without generating an OTP (Requirement 8.10).
    """


class OTPBlockedError(Exception):
    """Client exhausted the allowed OTP attempts (brute-force block).

    Signalled to the ``TransferBrebStateMachine`` via ``send_task_failure`` and
    routed to ``NotifyOTPBlocked`` (Requirement 16.7).
    """


__all__ = [
    "InsufficientFundsError",
    "InvalidDestinationError",
    "OTPBlockedError",
]
