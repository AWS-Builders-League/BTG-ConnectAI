"""SMS_Service Lambda (SQS-triggered, AWS Pinpoint).

Single Python 3.13 handler that consumes the ``sms-notification-queue`` (owned
by ``infra``) via an SQS Event Source Mapping and sends concise, post-operation
transfer-confirmation SMS messages through AWS Pinpoint (design §7
"Event-Driven Async Notifications", Requirements 17.2, 17.8).

The flow is strictly fire-and-forget from the producer's point of view: the
``TransferBrebStateMachine`` ``PublishNotifications`` state enqueues a
``transfer_confirmation`` event (amount + masked destination) and never waits
for delivery, so a Pinpoint outage only delays (queued) or DLQs the SMS — it
never blocks the WhatsApp transaction the client already saw (Requirement 17.7).

This consumer is **independent of OTP_Service**: OTP delivery is synchronous
inside the Step Functions workflow (``waitForTaskToken``); this Lambda only
delivers asynchronous *post-operation* confirmations. The bank statement PDF is
never sent here — its only delivery channel is WhatsApp (Requirement 17.8).

See :mod:`sms_service.handler` for the batch processing and the Pinpoint send.
"""
