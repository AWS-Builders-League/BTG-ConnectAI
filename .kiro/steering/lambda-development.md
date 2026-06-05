---
inclusion: fileMatch
fileMatchPattern: "src/lambdas/**/*.py"
---

# Guía de Desarrollo Lambda — BTG ConnectAI

## Plantilla Base de Handler

```python
"""<Nombre del servicio> - <descripción breve>."""
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger(service="<nombre-servicio>")


@logger.inject_lambda_context(correlation_id_path="body.correlationId")
def handler(event: dict, context: LambdaContext) -> dict:
    """Entry point de la Lambda."""
    logger.info("Procesando evento", extra={"event_keys": list(event.keys())})
    # ... lógica
    return {"statusCode": 200, "body": ""}
```

## Shared Layer

Importar utilidades compartidas así:
```python
from shared.logger import logger
from shared.masking import mask_phone, mask_account, mask_document
from shared.formatting import format_cop
from shared.constants import AUTH_SESSION_TTL, OTP_TTL, MAX_TWILIO_MESSAGE_LENGTH
from shared.types import TwilioWebhookPayload, AuthSession, OTPRecord
```

## Manejo de Errores

- Errores de dominio: clases custom en `src/shared/errors.py` (InsufficientFundsError, InvalidDestinationError, etc.)
- Errores de infraestructura: dejar que Lambda los levante para retry automático (SQS batch item failure)
- Siempre loguear el error con `logger.exception()` antes de re-raise

## Batch Processing (SQS-triggered Lambdas)

```python
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)

processor = BatchProcessor(event_type=EventType.SQS)

def record_handler(record):
    """Procesa un solo mensaje SQS."""
    payload = json.loads(record.body)
    # ... lógica por mensaje
    # Si falla, el exception se reporta como batchItemFailure

@logger.inject_lambda_context
def handler(event, context):
    return process_partial_response(
        event=event, record_handler=record_handler,
        processor=processor, context=context,
    )
```

## Invocación de Tools (Strands Agent)

Las tools se invocan como Lambdas independientes:
```python
import boto3
import json

lambda_client = boto3.client("lambda")

def invoke_tool(function_name: str, payload: dict) -> dict:
    """Invoca una Lambda tool y retorna el resultado."""
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    return json.loads(response["Payload"].read())
```

## Variables de Entorno Comunes

| Variable | Descripción |
|----------|-------------|
| `INBOUND_QUEUE_URL` | URL de la cola SQS FIFO de ingesta |
| `TWILIO_SECRET_ARN` | ARN del secreto con credenciales Twilio |
| `CONSENT_TABLE_NAME` | Nombre tabla DynamoDB Consent_Store |
| `AUTH_SESSION_TABLE_NAME` | Nombre tabla DynamoDB Auth_Session |
| `OTP_TABLE_NAME` | Nombre tabla DynamoDB OTP_Store |
| `STATEMENT_BUCKET` | Nombre del bucket S3 para extractos |
| `AUDIO_TEMP_BUCKET` | Nombre del bucket S3 para audio temporal |
| `STATE_MACHINE_ARN` | ARN del TransferBrebStateMachine |
| `EMAIL_QUEUE_URL` | URL cola de notificaciones email |
| `SMS_QUEUE_URL` | URL cola de notificaciones SMS |

## Reglas de Red

- Lambdas que necesitan Twilio/APIs públicas → **FUERA de VPC**
- Lambdas de dominio bancario (balance, transfer validate/execute, statement) → **EN VPC privada**
- Las Lambdas en VPC solo acceden a S3 y DynamoDB vía VPC Endpoints (Gateway)
