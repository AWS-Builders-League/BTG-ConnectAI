"""Strands_Agent Lambda package (Conversational_Agent).

Implements Requirement 10 (comprensión de lenguaje natural), Requirement 11
(memoria/contexto de sesión) and Requirement 12 (gobierno y guardrails de la IA).
The agent runs Claude 3.5 Haiku on Amazon Bedrock through the Strands Agent SDK,
exposes three banking tools (balance, BRE-B transfer, statement) and is invoked
synchronously by the Message_Processor with ``{sessionId, inputText, phoneNumber,
correlationId}``.

Module layout (design §8 Strands_Agent):

* :mod:`.prompts` — the Spanish system prompt and service-menu copy. Pure data;
  imports cleanly with **no** third-party dependency.
* :mod:`.tools` — the three ``@tool`` functions that invoke the Action Group
  Lambdas via ``boto3 lambda.invoke``. The per-invocation ``phoneNumber`` /
  ``correlationId`` reach the tools through :mod:`contextvars` (the model never
  supplies them). Imports cleanly even when the ``strands`` SDK is absent (the
  ``@tool`` decorator degrades to a no-op passthrough), so the Lambda-invoking
  logic stays unit-testable in isolation.
* :mod:`.agent` — builds the Strands :class:`~strands.Agent` with the Bedrock
  model, applies the ``infra`` Bedrock Guardrail and wires session memory. This
  is the only module that constructs a live Bedrock client, so it is imported
  lazily by the handler.
* :mod:`.handler` — the Lambda entry point that sets the invocation context,
  runs the agent and shapes the ``{"response": ...}`` payload the
  Message_Processor expects.

Runs **outside the VPC** in production (only needs Bedrock + Lambda invoke,
secured by IAM); that is a deployment concern (Task 15) and needs no code change.
"""
