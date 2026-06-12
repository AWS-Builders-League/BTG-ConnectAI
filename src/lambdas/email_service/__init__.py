"""Email_Service Lambda (SQS-triggered, Amazon SES).

Single Python 3.13 handler that consumes the ``email-notification-queue``
(owned by ``infra``) via an SQS Event Source Mapping and sends formal,
post-operation confirmation emails through Amazon SES (design §7
"Event-Driven Async Notifications", Requirements 17.2–17.7).

The flow is strictly fire-and-forget from the producer's point of view: the
``TransferBrebStateMachine`` ``PublishNotifications`` state enqueues a
``transfer_confirmation`` event and never waits for delivery, so an SES outage
only delays (queued) or DLQs the email — it never blocks the WhatsApp
transaction the client already saw (Requirement 17.7).

See :mod:`email_service.handler` for the batch processing and the SES send.
"""
