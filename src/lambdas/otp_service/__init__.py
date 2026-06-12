"""OTP_Service Lambda package.

Houses the OTP_Service invoked by the ``TransferBrebStateMachine`` Step Functions
workflow at the ``GenerateOTP`` state via the
``arn:aws:states:::lambda:invoke.waitForTaskToken`` integration pattern
(Requirement 16). The handler generates a 6-digit OTP, persists it (with the
Step Functions task token) in the ``OTP_Store`` DynamoDB table and sends it to
the client by SMS through AWS Pinpoint, then returns immediately while the
state machine stays suspended waiting for the callback.
"""

from __future__ import annotations
