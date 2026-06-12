"""Webhook_Receiver Lambda package.

Synchronous entry point behind API Gateway (HTTP API). Validates the
``X-Twilio-Signature`` header, parses the form-urlencoded payload, and enqueues
the message to the inbound SQS FIFO queue, responding ``200 OK`` to Twilio in
under one second.
"""
