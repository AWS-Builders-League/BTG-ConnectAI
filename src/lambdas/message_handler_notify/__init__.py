"""message-handler-notify Lambda.

Single handler (:mod:`handler`) invoked from the TERMINAL states of the
``TransferBrebStateMachine`` (``NotifyUserSuccess``, ``NotifyValidationFailed``,
``NotifyOTPExpired``, ``NotifyOTPBlocked``, ``NotifyTransferFailed``). It maps
the ``messageType`` carried in the state's ``Parameters`` to the appropriate
Spanish WhatsApp message and delivers it to the client via the Twilio REST API
(Requirements 8.4, 8.7, 8.8, 8.9, 8.10, 8.11, 8.12).
"""
