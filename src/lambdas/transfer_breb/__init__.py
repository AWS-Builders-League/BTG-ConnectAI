"""transfer-breb Action Group Lambdas.

This package contains the three Python 3.13 handlers that make up the BRE-B
transfer flow orchestrated by the ``TransferBrebStateMachine``:

* :mod:`initiator` — Strands Agent tool. Fires the state machine via
  ``StartExecution`` (idempotent on ``correlationId``) and returns immediately.
* :mod:`validate` — ``ValidateTransfer`` state task. Validates the transfer
  against Mock_Core, raising domain errors on failure.
* :mod:`execute` — ``ExecuteTransfer`` state task. Applies the (mock) balance
  movement and produces the transfer receipt.

The shared synthetic banking data lives in :mod:`mock_data`.
"""
