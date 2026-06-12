"""Message_Processor Lambda package.

Async Lambda triggered by the inbound SQS FIFO queue. Owns the heavy lifting of
an inbound WhatsApp message: consent gating, audio transcription, auth session
checks, OTP callbacks, Strands_Agent invocation and the Twilio REST response.
Each concern lives in its own module (``consent``, ``auth``, ``transcription``,
``messaging``, ``otp_callback``) and is orchestrated by ``handler``.
"""
