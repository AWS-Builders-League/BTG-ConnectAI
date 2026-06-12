# Technical Design Document

## Overview

BTG ConnectAI MVP es un asistente bancario conversacional serverless que conecta WhatsApp con Amazon Bedrock Agent para ejecutar servicios bancarios en español natural. El sistema soporta entrada multimodal (texto y audio), flujo de consentimiento regulatorio, autenticación vía enlace web, y tres servicios bancarios: consulta de saldos, transferencias BRE-B y generación de extractos PDF.

### Decisiones Arquitectónicas Clave

| Decisión | Elección | Razón |
| -------- | -------- | ----- |
| Runtime (todas las Lambdas) | Python 3.13 | Decisión de equipo: stack 100% Python. Strands SDK nativo, boto3, aws-lambda-powertools |
| IaC | CloudFormation puro (YAML) | Mismo patrón y convenciones del repo `infra`. Nested stacks, deploy via GitHub Actions + OIDC. Sin CDK ni SAM |
| Ubicación de la IaC de recursos compartidos | Repo `infra` (IaC centralizada) | API Gateway, SQS (FIFO inbound + colas de notificación + DLQs), S3 (Statement, Audio_Temp, Login_Page, artefactos), DynamoDB (Consent_Store, Auth_Session, OTP_Store), Bedrock (Agent Core + Guardrails), red (Security Groups + VPC Endpoints), Secrets Manager, SNS de alarmas y observabilidad compartida viven en `infra`. Un solo dueño de la infraestructura base/compartida, gobernada por los lineamientos de `infra` |
| Ubicación de la IaC de cómputo/orquestación | Repo `BTG-ConnectAI` | Solo define cada función Lambda (Webhook_Receiver, Message_Processor, Auth_Service, balance-query, transfer-breb-validate/execute/initiator, statement-generator, OTP_Service, Email_Service, SMS_Service, Strands_Agent) y la state machine `TransferBrebStateMachine`, más el código de aplicación. No contiene IaC de recursos compartidos |
| Contrato entre repos (cross-stack) | CloudFormation `Outputs`/`Export` + SSM Parameter Store, consumidos por la app | `infra` exporta ARNs/URLs/IDs de los recursos que crea; la app los resuelve con `Fn::ImportValue` (mismo region/cuenta) y/o `{{resolve:ssm:...}}` (desacople preferido entre stacks de repos distintos). Convención de nombres `${ProjectName}-${Environment}-<Recurso>` |
| Orden de despliegue | `infra` primero, luego `BTG-ConnectAI` | La app depende de exports/parámetros publicados por `infra`. El deploy de la app falla rápido si `infra` no ha publicado el contrato |
| Empaquetado Lambda | ZIP a S3 + Lambda Layers | CloudFormation `Code: {S3Bucket, S3Key}`. Dependencias pip en Layers compartidos |
| AI Engine | Strands Agent SDK + Amazon Bedrock Agent Core (Claude Haiku 3.5) | Framework open-source AWS sobre Bedrock; control de orquestación, herramientas y memoria de sesión |
| Canal WhatsApp | Twilio (WhatsApp Sandbox) | Onboarding rápido sin aprobación Meta, webhooks REST simples |
| Entrada HTTP | Amazon API Gateway (HTTP API) | Endpoint público expuesto a Twilio; bajo costo, sin servidor |
| Patrón Webhook | Async via SQS FIFO (Webhook_Receiver → queue → Message_Processor) | Receiver responde 200 a Twilio en <1s independientemente del trabajo real; elimina timeouts y retries de Twilio; absorbe spikes; escalabilidad independiente |
| Audio | Amazon Transcribe | Soporte nativo OGG/Opus, español colombiano (es-CO); sin presión de tiempo gracias al async |
| Deduplicación | SQS FIFO `MessageDeduplicationId = MessageSid` | Dedup nativa en ventana de 5 min sin código custom; elimina la tabla Dedup |
| Orden de mensajes | SQS FIFO `MessageGroupId = phoneNumber` | Garantiza que mensajes del mismo cliente se procesen en orden, sin afectar concurrencia entre clientes distintos |
| Autenticación | Lambda + DynamoDB (mock vía enlace web) | Simula flujo real con mínima infraestructura |
| OTP Transaccional | AWS Pinpoint (SMS) | Segundo factor para autorizar transferencias; canal SMS independiente de WhatsApp |
| Email | Amazon SES | Notificaciones formales post-operación; non-blocking respecto al flujo de WhatsApp |
| Extractos | S3 + envío como documento adjunto Twilio | Entrega directa al Bank_Client vía Twilio Media |
| Observabilidad | Lambda Powertools + CloudWatch | Structured logging JSON, métricas nativas |
| Seguridad | IAM roles + AWS managed keys + Secrets Manager | Zero cost, credenciales Twilio nunca en código |

### Flujo de Datos Principal (Happy Path Completo)

```mermaid
sequenceDiagram
    participant BC as Bank_Client (WhatsApp)
    participant TW as Twilio
    participant APIGW as API Gateway
    participant WHR as Webhook_Receiver Lambda
    participant SQS as SQS FIFO inbound-queue
    participant MP as Message_Processor Lambda
    participant DDB_C as DynamoDB (Consent_Store)
    participant DDB_A as DynamoDB (Auth_Session)
    participant TS as Amazon Transcribe
    participant SA as Strands_Agent Lambda
    participant GR as Bedrock Guardrails
    participant SFN as TransferBrebStateMachine
    participant AG_B as Action_Group: balance-query
    participant AG_TS as transfer-breb-initiator
    participant AG_S as statement-generator
    participant S3 as Statement_Bucket (S3)

    BC->>TW: Mensaje WhatsApp (texto o audio)
    TW->>APIGW: POST /webhook/twilio (form-urlencoded)
    APIGW->>WHR: Invoke Lambda (sync)
    WHR->>WHR: Validar X-Twilio-Signature
    WHR->>SQS: SendMessage (MessageGroupId=phoneNumber,<br/>MessageDeduplicationId=MessageSid)
    WHR-->>APIGW: 200 OK
    APIGW-->>TW: 200 OK (latencia total < 1s)
    Note over SQS: FIFO descarta MessageSids duplicados<br/>en ventana de 5 min

    SQS->>MP: Event Source Mapping (batchSize=1)
    MP->>DDB_C: GetItem (consent check)
    alt Sin consentimiento
        MP->>TW: Enviar mensaje con botones T&C (Twilio REST API)
        Note over MP,SQS: SQS marca el mensaje como procesado.<br/>Cuando llegue el ButtonPayload, será otro msg en cola
    end
    alt Audio message
        MP->>TS: StartTranscriptionJob (OGG/Opus desde Twilio Media URL)
        TS-->>MP: Transcripción en texto
    end
    MP->>DDB_A: GetItem (auth session check)
    alt Sin Auth_Session activa
        MP->>TW: Enviar enlace de login (Twilio REST)
        Note over BC,MP: Bank_Client completa login en Login_Page (S3)<br/>Auth_Service crea Auth_Session en DDB_A
    end
    MP->>SA: Invoke (sessionId, inputText, phoneNumber)
    SA->>GR: Evaluar input
    GR-->>SA: Input aprobado
    alt Consulta de saldo
        SA->>AG_B: Invoke balance-query
        AG_B-->>SA: Saldos
    end
    alt Transferencia BRE-B
        SA->>AG_TS: Invoke transfer-breb-initiator
        AG_TS->>SFN: StartExecution (TransferBrebStateMachine)
        AG_TS-->>SA: {executionArn, status: STARTED} (inmediato)
        Note over SFN: Workflow async (ver sección Step Functions)<br/>Cuando termina, NotifyUserSuccess envía respuesta via Twilio
    end
    alt Generación de extracto
        SA->>AG_S: Invoke statement-generator
        AG_S->>S3: PutObject (PDF)
        AG_S-->>SA: {s3Bucket, s3Key, fileName}
    end
    SA->>GR: Evaluar output
    GR-->>SA: Output aprobado
    SA-->>MP: Respuesta final (texto + S3 key si hay PDF)
    alt Respuesta incluye PDF
        MP->>S3: GetSignedUrl (presigned URL)
        MP->>TW: Enviar documento adjunto via mediaUrl (Twilio REST)
    end
    MP->>TW: Enviar respuesta de texto (Twilio REST API)
    TW->>BC: Respuesta en WhatsApp
    MP-->>SQS: Mensaje completado (ACK implícito al retornar sin error)
```

### Flujo de Autenticación (Detalle)

```mermaid
sequenceDiagram
    participant BC as Bank_Client
    participant TW as Twilio
    participant APIGW as API Gateway
    participant WHR as Webhook_Receiver
    participant SQS as SQS FIFO
    participant MP as Message_Processor
    participant LP as Login_Page (S3 Static)
    participant AS as Auth_Service Lambda
    participant DDB as DynamoDB (Auth_Session)

    BC->>TW: "Quiero ver mi saldo"
    TW->>APIGW: POST /webhook/twilio
    APIGW->>WHR: Invoke (sync)
    WHR->>SQS: SendMessage
    WHR-->>APIGW: 200 OK (<1s)
    SQS->>MP: Trigger async
    MP->>DDB: GetItem(phoneNumber)
    DDB-->>MP: No Auth_Session found
    MP->>TW: Enviar enlace de login (Twilio REST API)
    TW->>BC: Mensaje con enlace

    BC->>LP: GET /login?phone=+57300XXXX&token=xyz
    LP-->>BC: Formulario HTML
    BC->>LP: POST /login (credentials)
    LP->>AS: POST /authenticate
    AS->>DDB: PutItem(Auth_Session, TTL=30min)
    AS-->>LP: 200 OK
    LP-->>BC: "Autenticación exitosa, vuelve a WhatsApp"

    Note over BC,MP: El cliente vuelve a WhatsApp y reenvía su solicitud
    BC->>TW: "Quiero ver mi saldo" (segundo intento)
    TW->>APIGW: POST /webhook/twilio
    APIGW->>WHR: Invoke
    WHR->>SQS: SendMessage
    WHR-->>APIGW: 200 OK
    SQS->>MP: Trigger
    MP->>DDB: GetItem(phoneNumber)
    DDB-->>MP: Auth_Session activa
    MP->>MP: Procesar solicitud (consulta de saldo)
```

## Architecture

### Diagrama de Componentes

```mermaid
graph TB
    subgraph "Canal WhatsApp"
        WA[WhatsApp - Cliente]
        TWILIO[Twilio WhatsApp Sandbox]
    end

    subgraph "Sync Ingestion - latencia < 1s"
        APIGW[API Gateway - POST /webhook/twilio]
        WHR[Webhook_Receiver Lambda - Python 3.13]
        SQS_IN[SQS FIFO - inbound-messages-queue<br/>MessageGroupId=phoneNumber<br/>MessageDeduplicationId=MessageSid]
    end

    subgraph "Async Processing - sin presión de tiempo"
        MP[Message_Processor Lambda - Python 3.13]
        DDB_CONSENT[DynamoDB - Consent_Store]
    end

    subgraph "Authentication Layer"
        LP[Login_Page - S3 Static Site]
        AS[Auth_Service Lambda - Python 3.13]
        DDB_AUTH[DynamoDB - Auth_Session]
    end

    subgraph "Audio Processing"
        TRANSCRIBE[Amazon Transcribe]
    end

    subgraph "AI Layer"
        SA[Strands_Agent Lambda - Python 3.13]
        FM[Bedrock Agent Core - Claude Haiku 3.5]
        GR[Bedrock Guardrails]
    end

    subgraph "Action Groups - Tools del Strands Agent"
        AG_BAL[balance-query Lambda - Python 3.13]
        AG_STM[statement-generator Lambda - Python 3.13]
        AG_TRF_START[transfer-breb-initiator Lambda]
    end

    subgraph "Transfer Orchestration - AWS Step Functions"
        SFN[TransferBrebStateMachine]
        SFN_VAL[ValidateTransfer Lambda]
        SFN_EXEC[ExecuteTransfer Lambda]
    end

    subgraph "OTP Service"
        OTP[OTP_Service Lambda - Python 3.13]
        PINPOINT[AWS Pinpoint - SMS]
        DDB_OTP[DynamoDB - OTP Store + TaskToken]
    end

    subgraph "Async Notifications - Event-Driven"
        SQS_EMAIL[SQS - email-notification-queue]
        SQS_SMS[SQS - sms-notification-queue]
        EMAIL[Email_Service Lambda - SQS triggered]
        SMS_SVC[SMS_Service Lambda - SQS triggered]
        SES[Amazon SES]
    end

    subgraph "Storage"
        S3_STM[Statement_Bucket - S3]
        MOCK[Mock_Core - Inline Data]
    end

    subgraph "Observability"
        CW_LOGS[CloudWatch Logs]
        CW_DASH[CloudWatch Dashboard]
        CW_ALARM[CloudWatch Alarms]
        SNS_ALARM[SNS Topic - Alarms]
    end

    subgraph "Security"
        SM[Secrets Manager - Twilio creds]
        IAM[IAM Roles & Policies]
    end

    WA <--> TWILIO
    TWILIO --> APIGW
    APIGW --> WHR
    WHR -->|SendMessage| SQS_IN
    WHR -.->|200 OK <1s| APIGW
    SQS_IN -->|Event Source Mapping<br/>batchSize=1| MP
    MP --> DDB_CONSENT
    MP --> DDB_AUTH
    MP --> TRANSCRIBE
    MP --> SA
    MP --> TWILIO
    MP -.->|SendTaskSuccess con OTP| SFN
    LP --> AS
    AS --> DDB_AUTH
    SA --> FM
    SA --> GR
    SA --> AG_BAL
    SA --> AG_STM
    SA --> AG_TRF_START
    AG_TRF_START -->|StartExecution| SFN
    SFN --> SFN_VAL
    SFN --> OTP
    SFN --> SFN_EXEC
    SFN -->|publish event| SQS_EMAIL
    SFN -->|publish event| SQS_SMS
    SFN -->|notifica al cliente| MP
    AG_BAL --> MOCK
    SFN_VAL --> MOCK
    SFN_EXEC --> MOCK
    AG_STM --> MOCK
    AG_STM --> S3_STM
    AG_STM -->|publish event| SQS_EMAIL
    OTP --> PINPOINT
    OTP --> DDB_OTP
    SQS_EMAIL -->|trigger batch| EMAIL
    SQS_SMS -->|trigger batch| SMS_SVC
    EMAIL --> SES
    EMAIL --> S3_STM
    SMS_SVC --> PINPOINT
    WHR --> CW_LOGS
    MP --> CW_LOGS
    SA --> CW_LOGS
    AG_BAL --> CW_LOGS
    AG_TRF_START --> CW_LOGS
    AG_STM --> CW_LOGS
    AS --> CW_LOGS
    OTP --> CW_LOGS
    EMAIL --> CW_LOGS
    SMS_SVC --> CW_LOGS
    SFN --> CW_LOGS
    SFN_VAL --> CW_LOGS
    SFN_EXEC --> CW_LOGS
    CW_LOGS --> CW_DASH
    CW_ALARM --> SNS_ALARM
    WHR -.-> SM
    MP -.-> SM
    AS -.-> SM
```

### Principios Arquitectónicos

0a. **IaC de recursos compartidos centralizada en el repo `infra`**: Toda la infraestructura base/compartida se define y gobierna en el repo `infra` siguiendo SUS lineamientos (CloudFormation puro, `cloudformation/templates/<modulo>/` + `cloudformation/stacks/<ambiente>/`, tags obligatorios, deploy con OIDC). En `infra` viven: **API Gateway (HTTP API)**, **todas las colas SQS** (FIFO inbound + Standard de notificaciones email/sms + sus DLQs), **todos los buckets S3** (Statement, Audio_Temp, Login_Page, artefactos de Lambda), **todas las tablas DynamoDB** (Consent_Store, Auth_Session, OTP_Store), **Bedrock** (Agent Core + Guardrails), y el resto de infraestructura compartida (**Secrets Manager**, **SNS de alarmas**, **VPC access**: Security Groups + VPC Endpoints, **observabilidad compartida**). `BTG-ConnectAI` ya **no** contiene la IaC de estos recursos.

0b. **BTG-ConnectAI solo define cómputo y orquestación**: Este repo define exclusivamente la IaC de **cada función Lambda** (Webhook_Receiver, Message_Processor, Auth_Service, balance-query, transfer-breb-validate, transfer-breb-execute, transfer-breb-initiator, statement-generator, OTP_Service, Email_Service, SMS_Service, Strands_Agent) y de la **state machine** `TransferBrebStateMachine`, más el código de aplicación (Python 3.13) y la Login_Page estática. Los recursos compartidos se consumen por referencia (ver sección *Integración Cross-Repo / Cross-Stack*).

0c. **Contrato cross-stack explícito**: Las Lambdas y la state machine obtienen ARNs, URLs de cola, nombres de tabla, ARNs de bucket, ARN del Bedrock Agent/Guardrail, Security Group ID y Subnet IDs mediante el contrato publicado por `infra`: CloudFormation `Outputs`/`Export` consumidos con `Fn::ImportValue` y/o SSM Parameter Store. El orden de despliegue es **`infra` primero, luego `BTG-ConnectAI`**.

1. **Estrategia de red híbrida (VPC solo donde aporta)**: VPC no es seguridad por defecto en serverless — el control de acceso real es IAM. Por eso solo las Lambdas del **dominio bancario** (`balance_query`, `transfer_breb_validate`, `transfer_breb_execute`, `statement_generator`) corren en subnets privadas de `IA-Builder-sandbox-networking`. Son las que en EXT-1 se conectarán al core privado. El resto (canal, orquestación, IA, notificaciones, auth) corre **fuera de VPC** en la red managed de Lambda. **No hay NAT Gateway** (`EnableNatGateway=false`). La VPC, las subnets, los Security Groups (incluido `BankingLambdaSG`) y los VPC Endpoints son propiedad del repo `infra`; `BTG-ConnectAI` solo los **referencia** vía `Fn::ImportValue`/SSM para configurar el `VpcConfig` de las Lambdas bancarias.
2. **Cero salida a internet para el dominio bancario**: Las Lambdas en VPC NO tienen ruta `0.0.0.0/0` — alcanzan servicios AWS solo vía **VPC Endpoints Gateway** (gratis para S3/DynamoDB). Esto elimina cualquier ruta de exfiltración: aunque una Lambda bancaria se comprometa, no puede sacar datos a internet. Security Group sin ingress de red, egress 443 solo hacia los endpoints. CloudWatch Logs no requiere endpoint (la plataforma de Lambda los envía, no la ENI).
3. **Runtime único Python 3.13**: Todas las Lambdas (Webhook_Receiver, Message_Processor, Action Groups, OTP_Service, notificadores, Strands_Agent) corren en Python 3.13. Stack 100% Python por decisión de equipo. boto3 para AWS, `aws-lambda-powertools` para logging/tracing, `twilio` SDK para mensajería.
4. **Twilio como canal**: Twilio Sandbox recibe y envía mensajes WhatsApp. API Gateway expone el webhook público. Credenciales Twilio en Secrets Manager.
4a. **Async Webhook Pattern**: Separación estricta `Webhook_Receiver` (sync, latencia <1s, solo valida firma y encola) + `Message_Processor` (async, SQS-triggered, hace todo el trabajo pesado). Twilio nunca espera transcripción ni invocación de Bedrock; recibe 200 OK inmediato. Spike de tráfico es absorbido por la cola, no por throttling de Lambda.
4b. **SQS FIFO para mensajes entrantes**: `inbound-messages-queue.fifo` con `MessageGroupId=phoneNumber` (garantiza orden por cliente) y `MessageDeduplicationId=MessageSid` (dedup automática de retries de Twilio en ventana de 5 min — elimina necesidad de tabla Dedup custom). `batchSize=1` porque cada mensaje es una interacción crítica.
5. **Strands + Bedrock Agent Core**: El Conversational_Agent usa Strands Agent SDK (Python) sobre Bedrock Agent Core (Claude Haiku 3.5). Strands maneja orquestación de herramientas y memoria de sesión.
6. **Step Functions para transacciones distribuidas**: El flujo de transferencia BRE-B (validar → OTP → esperar callback → ejecutar → notificar) corre como state machine de AWS Step Functions usando el patrón `waitForTaskToken`. Esto resuelve el problema de "esperar input asíncrono del cliente" sin bloquear Lambdas y provee manejo nativo de timeouts, reintentos y compensación.
7. **Event-Driven Async Notifications**: Email y SMS de confirmación se publican como eventos a SQS (`email-notification-queue`, `sms-notification-queue`). Las Lambdas consumidoras (`Email_Service`, `SMS_Service`) procesan en batch. El flujo principal no espera respuesta del envío — fire-and-forget total. Esto desacopla productores de consumidores y permite reintentos automáticos con DLQ.
8. **OTP con Task Token**: El OTP_Service no "espera" al usuario. Step Functions pausa la ejecución con `waitForTaskToken`, almacenando el token en DynamoDB junto al OTP. Cuando el cliente responde con el código, el Message_Processor lo valida y llama `SendTaskSuccess`/`SendTaskFailure` para resumir el workflow.
9. **Consent-First**: Ningún servicio se ejecuta sin consentimiento previo registrado en Consent_Store.
10. **Auth-Before-Action**: Operaciones bancarias requieren Auth_Session activa (TTL 30min).
11. **Mock Data Inline**: Datos bancarios sintéticos hardcodeados en las Lambdas de Action Groups para el demo.
12. **Encryption at Rest by Default**: AWS managed keys en DynamoDB y S3 — zero cost, zero management.


## Components and Interfaces

### 1. Webhook_Receiver Lambda

**Responsabilidad:** Punto de entrada SÍNCRONO del sistema. Su única misión es responder a Twilio en menos de un segundo. No hace negocio — valida la firma, parsea el payload y lo encola en SQS FIFO. Toda la lógica pesada se delega al Message_Processor de forma asíncrona.

**Runtime:** Python 3.13
**Memory:** 256 MB
**Timeout:** 10 seconds (en práctica resuelve en <1s)
**Trigger:** Amazon API Gateway (HTTP API — POST /webhook/twilio). El **API Gateway lo crea `infra`**; la **Integration (`AWS_PROXY`), la Route (`POST /webhook/twilio`) y el `AWS::Lambda::Permission`** se definen en `BTG-ConnectAI` usando el `HttpApiId` importado del contrato (ver *Integración Cross-Repo / Cross-Stack*).
**Provisioned Concurrency:** Recomendado en producción (no requerido para hackathon)

#### Lógica del Webhook_Receiver

```python
import json
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

import boto3
from aws_lambda_powertools import Logger
from twilio.request_validator import RequestValidator

logger = Logger(service="webhook-receiver")
sqs = boto3.client("sqs")

INBOUND_QUEUE_URL = os.environ["INBOUND_QUEUE_URL"]
TWILIO_AUTH_TOKEN = _load_twilio_auth_token()  # desde Secrets Manager, cacheado en cold start


@logger.inject_lambda_context
def handler(event: dict, context) -> dict:
    correlation_id = str(uuid.uuid4())
    logger.append_keys(correlation_id=correlation_id)

    # 1. Validar firma X-Twilio-Signature (defensa contra requests no autorizados)
    signature = event["headers"].get("x-twilio-signature", "")
    url = f"https://{event['requestContext']['domainName']}{event['rawPath']}"
    params = {k: v[0] for k, v in parse_qs(event.get("body", "")).items()}

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    if not validator.validate(url, params, signature):
        logger.warning("Invalid Twilio signature, rejecting")
        return {"statusCode": 403, "body": ""}

    # 2. Encolar en SQS FIFO — dedup automática por MessageSid
    sqs.send_message(
        QueueUrl=INBOUND_QUEUE_URL,
        MessageBody=json.dumps({
            **params,
            "correlationId": correlation_id,
            "receivedAt": datetime.now(timezone.utc).isoformat(),
        }),
        MessageGroupId=params["From"],              # Orden por cliente
        MessageDeduplicationId=params["MessageSid"],  # Dedup gratis 5 min
    )

    # 3. 200 OK inmediato — Twilio happy
    return {"statusCode": 200, "body": ""}
```

#### IAM Role del Webhook_Receiver

- `sqs:SendMessage` sobre `inbound-messages-queue.fifo`
- `secretsmanager:GetSecretValue` sobre el secreto con `TWILIO_AUTH_TOKEN` (para validar firma)
- `logs:*` para CloudWatch Logs

> Notar que el Receiver NO necesita acceso a DynamoDB, Bedrock, Transcribe, S3, ni a Twilio REST API. Es deliberadamente minimalista para minimizar la superficie de ataque y el cold start.

---

### 2. Message_Processor Lambda

**Responsabilidad:** Hace TODO el trabajo pesado de procesar un mensaje entrante: valida consentimiento, transcribe audio, valida sesión de autenticación, maneja callbacks de OTP, invoca al Strands_Agent y envía la respuesta al cliente vía Twilio REST API. Se ejecuta de forma asíncrona triggered por SQS — sin presión de tiempo del lado de Twilio.

**Runtime:** Python 3.13
**Memory:** 512 MB
**Timeout:** 120 seconds (suficiente para transcripción + Strands Agent + envío respuesta)
**Trigger:** SQS Event Source Mapping sobre `inbound-messages-queue.fifo`
**Batch Size:** 1 (cada mensaje es interacción crítica del usuario, no se batchea)
**Visibility Timeout (en la cola):** 130s (apenas mayor que el timeout del Lambda para permitir reintento limpio)

#### Interface de Entrada (SQS Event)

```python
from typing import TypedDict, NotRequired

# Payload que Twilio envía como form-urlencoded al webhook,
# re-empaquetado como JSON por el Webhook_Receiver al encolar en SQS
class TwilioWebhookPayload(TypedDict):
    MessageSid: str                       # ID único del mensaje (usado para dedup)
    From: str                             # "whatsapp:+57300XXXXXXX"
    To: str                               # "whatsapp:+14155XXXXXXX" (número Twilio)
    Body: str                             # Texto del mensaje (vacío si es media)
    NumMedia: str                         # "0" | "1" | ...
    MediaUrl0: NotRequired[str]           # URL del audio/imagen si NumMedia > 0
    MediaContentType0: NotRequired[str]   # "audio/ogg" | "image/jpeg" | ...
    ButtonPayload: NotRequired[str]       # Payload del botón de respuesta rápida
    ProfileName: NotRequired[str]         # Nombre de perfil de WhatsApp del cliente
    correlationId: str                    # Inyectado por el Webhook_Receiver
```

#### Lógica Principal

```python
import json
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response

logger = Logger(service="message-processor")
processor = BatchProcessor(event_type=EventType.SQS)


def record_handler(record) -> None:
    """Procesa un único mensaje SQS. Si lanza excepción, Powertools lo reporta
    como batchItemFailure para que SQS lo reintente individualmente."""
    payload: TwilioWebhookPayload = json.loads(record["body"])
    logger.append_keys(correlation_id=payload["correlationId"])

    phone_number = payload["From"].replace("whatsapp:", "")  # E.164

    # 1. OTP callback prioritario — si hay OTP pendiente, no llamamos al agente
    pending_otp = get_pending_otp(phone_number)
    if pending_otp:
        handle_otp_callback(phone_number, payload.get("Body", ""), pending_otp)
        return

    # 2. Verificar consentimiento
    consent = get_consent(phone_number)
    if not consent or not consent.get("accepted"):
        handle_consent_flow(payload, consent, phone_number)
        return

    # 3. Determinar tipo de mensaje y extraer texto
    if payload.get("ButtonPayload"):
        input_text = payload["ButtonPayload"]
    elif payload.get("NumMedia", "0") != "0" and payload.get("MediaContentType0", "").startswith("audio/"):
        input_text = transcribe_audio(payload["MediaUrl0"], phone_number)
        if not input_text:
            send_twilio_message(phone_number, ERROR_MESSAGES["transcription_failed"])
            return
    elif payload.get("Body", "").strip():
        input_text = payload["Body"].strip()
    else:
        send_twilio_message(phone_number, ERROR_MESSAGES["unsupported_format"])
        return

    # 4. Verificar Auth_Session
    auth_session = get_auth_session(phone_number)
    if not auth_session or is_expired(auth_session):
        store_pending_request(phone_number, input_text)
        send_login_link(phone_number)
        return

    # 5. Invocar Strands_Agent
    session_id = derive_session_id(phone_number)
    response = invoke_strands_agent(session_id, input_text, phone_number)

    # 6. Si la respuesta incluye un PDF (extracto), enviarlo como media adjunta
    statement_info = extract_statement_info(response)
    if statement_info:
        send_twilio_document(phone_number, statement_info["s3_bucket"], statement_info["s3_key"])

    # 7. Enviar respuesta de texto (split si > 1600 chars)
    text_response = remove_statement_metadata(response)
    if text_response.strip():
        send_twilio_message(phone_number, text_response)


@logger.inject_lambda_context
def handler(event: dict, context):
    # batchSize=1, pero process_partial_response soporta cualquier tamaño con reportBatchItemFailures
    return process_partial_response(
        event=event,
        record_handler=record_handler,
        processor=processor,
        context=context,
    )
```

#### Flujo de Consentimiento

```python
def handle_consent_flow(payload: TwilioWebhookPayload, consent: dict | None, phone_number: str) -> None:
    # Respuesta a botón de T&C (Twilio envía el payload del botón en ButtonPayload)
    if payload.get("ButtonPayload") == "accept_tc":
        store_consent(phone_number, "accepted")
        send_welcome_message(phone_number)
        return

    if payload.get("ButtonPayload") == "reject_tc":
        store_consent(phone_number, "rejected")
        send_twilio_message(phone_number, ERROR_MESSAGES["consent_required"])
        return

    # Primer mensaje sin consentimiento — enviar T&C con botones de acción rápida (Twilio)
    send_terms_and_conditions_message(phone_number)


def send_terms_and_conditions_message(phone_number: str) -> None:
    # Twilio soporta botones de respuesta rápida via Content Templates
    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_NUMBER}",
        to=f"whatsapp:{phone_number}",
        content_sid=TWILIO_TC_TEMPLATE_SID,
        content_variables=json.dumps({"phoneNumber": phone_number}),
    )
```

#### Transcripción de Audio

```python
def transcribe_audio(media_url: str, phone_number: str) -> str | None:
    try:
        # 1. Descargar audio desde Twilio Media URL (requiere auth Twilio)
        audio_bytes = download_twilio_media(media_url)

        # 2. Subir a S3 temporal para Transcribe
        s3_key = f"audio-temp/{uuid.uuid4()}.ogg"
        s3.put_object(Bucket=AUDIO_TEMP_BUCKET, Key=s3_key, Body=audio_bytes, ContentType="audio/ogg")

        # 3. Iniciar transcripción
        job_name = f"btg-connectai-{uuid.uuid4()}"
        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode="es-CO",
            MediaFormat="ogg",
            Media={"MediaFileUri": f"s3://{AUDIO_TEMP_BUCKET}/{s3_key}"},
            OutputBucketName=AUDIO_TEMP_BUCKET,
            OutputKey=f"transcriptions/{job_name}.json",
        )

        # 4. Polling hasta completar (sin presión de tiempo gracias al async; max 30s)
        transcript = wait_for_transcription(job_name, timeout_seconds=30)

        # 5. Limpiar archivos temporales
        cleanup_temp_files(s3_key, f"transcriptions/{job_name}.json")
        return transcript
    except Exception:
        logger.exception("Audio transcription failed")
        return None
```

#### Envío de Enlace de Login

```python
def send_login_link(phone_number: str) -> None:
    callback_token = generate_callback_token(phone_number)
    login_url = f"{LOGIN_PAGE_URL}?phone={quote(phone_number)}&token={callback_token}"

    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_NUMBER}",
        to=f"whatsapp:{phone_number}",
        body=(
            "🔐 Para ejecutar operaciones bancarias necesitas autenticarte.\n\n"
            f"Inicia sesión aquí: {login_url}\n\nEl enlace es válido por 10 minutos."
        ),
    )
```

> **Deduplicación**: ya NO existe función custom. SQS FIFO descarta duplicados por `MessageDeduplicationId = MessageSid` en ventana de 5 minutos. Ver decisiones arquitectónicas.

#### Invocación del Strands_Agent Lambda

```python
def invoke_strands_agent(session_id: str, input_text: str, phone_number: str) -> str:
    response = lambda_client.invoke(
        FunctionName=STRANDS_AGENT_LAMBDA_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "sessionId": session_id,
            "inputText": input_text,
            "phoneNumber": phone_number,
        }).encode("utf-8"),
    )
    result = json.loads(response["Payload"].read())
    return result["response"]
```

#### Envío de Respuesta vía Twilio (con split)

```python
MAX_TWILIO_MESSAGE_LENGTH = 1600


def send_twilio_message(phone_number: str, text: str) -> None:
    for chunk in split_message(text, MAX_TWILIO_MESSAGE_LENGTH):
        twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_NUMBER}",
            to=f"whatsapp:{phone_number}",
            body=chunk,
        )


def split_message(text: str, max_length: int) -> list[str]:
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        # Buscar último salto de línea o espacio antes del límite
        split_index = remaining.rfind("\n", 0, max_length)
        if split_index == -1 or split_index < max_length * 0.5:
            split_index = remaining.rfind(" ", 0, max_length)
        if split_index == -1:
            split_index = max_length
        chunks.append(remaining[:split_index])
        remaining = remaining[split_index:].lstrip()

    return chunks
```

#### Envío de Documento PDF vía Twilio (Extracto Bancario)

```python
def send_twilio_document(phone_number: str, s3_bucket: str, s3_key: str) -> None:
    # 1. Generar presigned URL temporal de S3 (Twilio necesita URL pública para descargar el media)
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": s3_bucket, "Key": s3_key},
        ExpiresIn=300,  # 5 minutos — suficiente para que Twilio descargue
    )

    # 2. Enviar mensaje con media adjunto via Twilio
    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_NUMBER}",
        to=f"whatsapp:{phone_number}",
        body="📄 Aquí tienes tu extracto bancario.",
        media_url=[presigned_url],
    )
```


### 3. Auth_Service Lambda

**Responsabilidad:** Backend de autenticación mock. Valida credenciales contra usuarios de prueba hardcodeados y crea Auth_Session en DynamoDB. Simula un flujo de autenticación vía enlace web.

**Runtime:** Python 3.13  
**Memory:** 128 MB  
**Timeout:** 10 seconds  
**Trigger:** API Gateway (HTTP API) o Function URL

#### Interface

```python
from typing import TypedDict, NotRequired

# POST /authenticate
class AuthenticateRequest(TypedDict):
    username: str
    password: str
    phoneNumber: str      # E.164 — vincula sesión al teléfono
    callbackToken: str    # Token para validar origen legítimo

class AuthenticateResponse(TypedDict):
    success: bool
    message: str
    sessionId: NotRequired[str]   # Solo si success=True
    expiresAt: NotRequired[str]   # ISO 8601 — TTL de la sesión
```

#### Usuarios de Prueba Hardcodeados

```python
TEST_USERS = [
    {"username": "carlos.rodriguez", "password": "Btg2024*Test",
     "phone_number": "+573001234567", "name": "Carlos Rodríguez", "document_id": "1234567890"},
    {"username": "maria.lopez", "password": "Btg2024*Demo",
     "phone_number": "+573009876543", "name": "María López", "document_id": "0987654321"},
    {"username": "juan.garcia", "password": "Btg2024*Hack",
     "phone_number": "+573005551234", "name": "Juan García", "document_id": "1122334455"},
]
```

#### Lógica de Autenticación

```python
import uuid
from datetime import datetime, timedelta, timezone


def authenticate(request: AuthenticateRequest) -> AuthenticateResponse:
    # 1. Validar callback token
    if not is_valid_callback_token(request["callbackToken"], request["phoneNumber"]):
        return {"success": False, "message": "Token inválido"}

    # 2. Buscar usuario
    user = next(
        (u for u in TEST_USERS
         if u["username"] == request["username"] and u["password"] == request["password"]),
        None,
    )
    if not user:
        return {"success": False, "message": "Credenciales incorrectas"}

    # 3. Validar que el teléfono coincide con el usuario
    if user["phone_number"] != request["phoneNumber"]:
        return {"success": False, "message": "Credenciales incorrectas"}

    # 4. Crear Auth_Session en DynamoDB
    session_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)  # 30 min TTL
    ttl = int(expires_at.timestamp())

    auth_table.put_item(Item={
        "pk": request["phoneNumber"],
        "sessionId": session_id,
        "username": user["username"],
        "name": user["name"],
        "documentId": user["document_id"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "expiresAt": expires_at.isoformat(),
        "ttl": ttl,
    })

    return {
        "success": True,
        "message": "Autenticación exitosa",
        "sessionId": session_id,
        "expiresAt": expires_at.isoformat(),
    }
```

### 4. Login_Page (S3 Static Site)

**Responsabilidad:** Página web simple con formulario de login. Hosted en S3 como sitio estático con CloudFront (o directamente S3 website hosting para MVP).

**Tecnología:** HTML + CSS + JavaScript vanilla (sin framework) — esto corre en el **navegador del cliente**, no es una Lambda, por lo que se mantiene en JavaScript

#### Estructura

```text
login-page/
├── index.html      # Formulario de login
├── styles.css      # Estilos BTG Pactual branding
├── app.js          # Lógica de submit + llamada a Auth_Service
└── assets/
    └── logo.png    # Logo BTG Pactual
```

#### Flujo de la Login_Page

```javascript
// app.js (client-side, corre en el navegador — se mantiene en JavaScript)
async function handleLogin(event) {
  event.preventDefault();

  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;
  const params = new URLSearchParams(window.location.search);
  const phoneNumber = params.get("phone");
  const callbackToken = params.get("token");

  const response = await fetch(AUTH_SERVICE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, phoneNumber, callbackToken }),
  });

  const result = await response.json();

  if (result.success) {
    showSuccess("✅ Autenticación exitosa. Puedes volver a WhatsApp.");
  } else {
    showError(result.message);
  }
}
```

### 5. Action_Group Lambda: balance-query

**Responsabilidad:** Consultar saldos de Fondos de Inversión y Cuenta Corriente del Mock_Core.

**Runtime:** Python 3.13  
**Memory:** 128 MB  
**Timeout:** 15 seconds  
**Trigger:** Bedrock Agent Action Group invocation

#### Interface de Entrada/Salida (Strands tool → Lambda invoke)

Las Action Group Lambdas son invocadas por las tools del Strands_Agent vía `boto3 lambda.invoke` con un payload JSON simple (no usan el formato de Bedrock Agents managed):

```python
# Evento que recibe la Lambda (enviado por la tool del Strands Agent)
class BalanceQueryEvent(TypedDict):
    phoneNumber: str
    productType: NotRequired[str]   # "fondo_inversion" | "cuenta_corriente"

# Respuesta de la Lambda
class ActionGroupResponse(TypedDict):
    success: bool
    data: NotRequired[dict]         # payload específico de la acción
    error: NotRequired[str]         # código de error si success=False
    message: NotRequired[str]
```

### 6. Action_Group Lambda: transfer-breb (validate + execute)

**Responsabilidad:** Validar y ejecutar transferencias BRE-B contra el Mock_Core. Estas funciones son invocadas por los estados `ValidateTransfer` y `ExecuteTransfer` del `TransferBrebStateMachine` (ver sección Step Functions), NO directamente por el Strands Agent (que solo dispara el state machine via `transfer-breb-initiator`).

**Runtime:** Python 3.13  
**Memory:** 128 MB  
**Timeout:** 15 seconds  
**Trigger:** Step Functions task (`ValidateTransfer`, `ExecuteTransfer`)

#### Lógica de Transferencia

```python
import uuid
from datetime import datetime, timezone


class InsufficientFundsError(Exception):
    """Error de dominio — capturado por el Catch del state machine."""


class InvalidDestinationError(Exception):
    """Error de dominio — capturado por el Catch del state machine."""


def execute_transfer(params: dict) -> dict:
    source_account = params["sourceAccount"]
    destination_account = params["destinationAccount"]
    amount = params["amount"]
    concept = params.get("concept", "")
    phone_number = params["phoneNumber"]

    # 1. Validar cuenta origen existe y pertenece al cliente
    source_acct = find_account_by_number(phone_number, source_account)
    if not source_acct:
        raise InvalidDestinationError("Cuenta origen no encontrada")

    # 2. Validar saldo suficiente
    if source_acct["available_balance"] < amount:
        raise InsufficientFundsError("Fondos insuficientes")

    # 3. Validar cuenta destino existe
    dest_acct = find_account_by_number(None, destination_account)
    if not dest_acct:
        raise InvalidDestinationError("Cuenta destino no encontrada")

    # 4. Ejecutar transferencia (mock — actualizar saldos)
    source_acct["available_balance"] -= amount
    source_acct["total_balance"] -= amount
    dest_acct["available_balance"] += amount
    dest_acct["total_balance"] += amount

    # 5. Generar comprobante
    receipt = {
        "transactionId": f"TRX-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:6]}",
        "sourceAccount": mask_account_number(source_account),
        "destinationAccount": mask_account_number(destination_account),
        "amount": amount,
        "currency": "COP",
        "concept": concept,
        "executedAt": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
    }
    return {"success": True, "receipt": receipt}
```

### 7. Action_Group Lambda: statement-generator

**Responsabilidad:** Generar extractos bancarios en PDF, almacenarlos en S3, y retornar la referencia (S3 key) para que el Message_Processor descargue y envíe el PDF al cliente vía WhatsApp (Twilio Media). El extracto NO se envía por email.

**Runtime:** Python 3.13  
**Memory:** 256 MB  
**Timeout:** 30 seconds  
**Trigger:** Strands Agent tool invocation (Lambda invoke via boto3)

#### Lógica de Generación

```python
from datetime import datetime, timezone


def generate_statement(params: dict) -> dict:
    phone_number = params["phoneNumber"]
    account_id = params["accountId"]
    cutoff_date = params["cutoffDate"]

    # 1. Validar fecha de corte (debe ser pasada)
    cutoff = datetime.fromisoformat(cutoff_date)
    if cutoff >= datetime.now(timezone.utc):
        return {"success": False, "error": "INVALID_DATE",
                "message": "La fecha de corte debe ser una fecha pasada"}

    # 2. Obtener datos del cliente y transacciones
    client = find_client_by_phone(phone_number)
    transactions = get_transactions_until_date(account_id, cutoff_date)

    # 3. Generar PDF (usando reportlab o fpdf2)
    pdf_bytes = generate_pdf({
        "client_name": client["name"],
        "account_number": mask_account_number(account_id),
        "period": {"start": get_start_of_month(cutoff_date), "end": cutoff_date},
        "transactions": transactions,
        "final_balance": calculate_balance(transactions),
    })

    # 4. Subir a S3 (vía Gateway Endpoint S3, sin salida a internet)
    s3_key = f"statements/{phone_number}/{account_id}/{cutoff_date}-{uuid.uuid4()}.pdf"
    s3.put_object(Bucket=STATEMENT_BUCKET, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")

    # 5. Retornar referencia S3 para que el Message_Processor envíe el PDF vía Twilio (WhatsApp)
    return {
        "success": True,
        "s3Bucket": STATEMENT_BUCKET,
        "s3Key": s3_key,
        "fileName": f"extracto_{account_id}_{cutoff_date}.pdf",
    }
```

> **Nota:** El extracto se entrega exclusivamente por WhatsApp como documento adjunto. El `Message_Processor` recibe el `{s3Bucket, s3Key, fileName}`, genera una presigned URL de S3 (5 min de vigencia) y la pasa a Twilio en `media_url`. No hay publicación a `email-notification-queue` desde este Lambda.


### 8. Strands_Agent Lambda (Conversational_Agent)

**Responsabilidad:** Interpretar intenciones en español (texto o audio transcrito), mantener contexto conversacional, decidir cuándo invocar herramientas (balance-query, transfer-breb, statement-generator), y formular respuestas naturales. Implementado como Lambda Python 3.13 usando Strands Agent SDK sobre Amazon Bedrock.

**Runtime:** Python 3.13  
**Memory:** 512 MB  
**Timeout:** 60 seconds  
**Trigger:** Lambda InvokeFunction desde Message_Processor (sync)  
**Foundation Model:** Claude Haiku 4.5 via Bedrock (us.anthropic.claude-haiku-4-5-20251001-v1:0, inference profile)  
**Session Strategy:** sessionId derivado del número de teléfono — Strands mantiene historial en memoria de sesión  
**Guardrails:** Bedrock Guardrails aplicados sobre el modelo (content filtering + topic policies)

#### Instrucciones del Agente (System Prompt)

```text
Eres el asistente virtual de BTG Pactual Colombia. Tu nombre es ConnectAI.

SERVICIOS DISPONIBLES:
1. Consulta de saldos (Fondos de Inversión y Cuenta Corriente)
2. Transferencias BRE-B (entre cuentas)
3. Generación de extractos bancarios (PDF)

REGLAS:
1. Responde SIEMPRE en español colombiano natural y amigable.
2. Solo puedes ayudar con los 3 servicios listados arriba e información general de productos BTG Pactual.
3. Si el cliente pregunta algo fuera del dominio bancario, declina amablemente y lista los servicios disponibles.
4. Cuando presentes datos financieros (saldos, montos), SIEMPRE incluye el disclaimer: "📋 Esta información es referencial. Para registros oficiales, consulta los portales del banco."
5. Si no entiendes la solicitud, haz UNA pregunta de aclaración. Si después de 2 intentos no logras entender, ofrece el menú de servicios.
6. Interpreta expresiones coloquiales colombianas: "plata"=dinero, "luca"=mil pesos, "extracto"=estado de cuenta, "pásame plata"=transferencia, "cuánto tengo"=consulta de saldo.
7. Formatea montos en COP con separador de miles (punto) y decimales (coma): $1.234.567,89
8. Para TRANSFERENCIAS: SIEMPRE presenta un resumen con cuenta origen, cuenta destino, monto y concepto, y solicita confirmación explícita ("¿Confirmas esta transferencia?") ANTES de ejecutar.
9. Para EXTRACTOS: Solicita la fecha de corte. Si el cliente da una fecha futura, informa que debe ser una fecha pasada.
10. Cuando presentes transacciones o movimientos, muestra máximo 5 y ofrece ver más si hay adicionales.
11. Si el cliente acaba de autenticarse, salúdalo por su nombre.

FORMATO DE RESPUESTA:
- Usa emojis moderadamente para hacer la conversación amigable
- Usa listas con viñetas para presentar múltiples productos o transacciones
- Mantén las respuestas concisas (máximo 3 párrafos)
```

#### Definición de Tools del Strands Agent

Con Strands Agent SDK, las herramientas se definen con el decorador `@tool` en Python. El docstring y los type hints son lo que el modelo usa para decidir cuándo invocar cada tool — no se requieren OpenAPI schemas (eso era específico de Bedrock Agents managed).

```python
from strands import tool
import boto3
import json

lambda_client = boto3.client("lambda")


@tool
def query_balance(phone_number: str, product_type: str | None = None) -> dict:
    """Consulta los saldos del cliente en BTG Pactual.

    Args:
        phone_number: Número de teléfono del cliente en formato E.164.
        product_type: Opcional. "fondo_inversion" o "cuenta_corriente".
                      Si se omite, retorna todos los productos.

    Returns:
        dict con la lista de productos y sus saldos (availableBalance, totalBalance, cutoffDate).
    """
    resp = lambda_client.invoke(
        FunctionName="balance-query",
        Payload=json.dumps({"phoneNumber": phone_number, "productType": product_type}).encode(),
    )
    return json.loads(resp["Payload"].read())


@tool
def initiate_transfer_breb(
    source_account: str, destination_account: str, amount: float,
    concept: str, phone_number: str,
) -> dict:
    """Inicia una transferencia BRE-B. Dispara el TransferBrebStateMachine que enviará
    un OTP por SMS al cliente para autorizar. NO espera el OTP — retorna inmediatamente.

    Úsala SOLO después de que el cliente confirmó explícitamente la operación.

    Returns:
        dict con executionArn y un mensaje indicando que se envió el OTP por SMS.
    """
    resp = lambda_client.invoke(
        FunctionName="transfer-breb-initiator",
        Payload=json.dumps({
            "sourceAccount": source_account, "destinationAccount": destination_account,
            "amount": amount, "concept": concept, "phoneNumber": phone_number,
        }).encode(),
    )
    return json.loads(resp["Payload"].read())


@tool
def generate_statement(phone_number: str, account_id: str, cutoff_date: str) -> dict:
    """Genera un extracto bancario en PDF para una cuenta hasta una fecha de corte.

    Args:
        phone_number: Teléfono del cliente en E.164.
        account_id: ID de la cuenta.
        cutoff_date: Fecha de corte (ISO 8601). DEBE ser una fecha pasada.

    Returns:
        dict con {success, s3Bucket, s3Key, fileName} o error si la fecha es futura.
    """
    resp = lambda_client.invoke(
        FunctionName="statement-generator",
        Payload=json.dumps({
            "phoneNumber": phone_number, "accountId": account_id, "cutoffDate": cutoff_date,
        }).encode(),
    )
    return json.loads(resp["Payload"].read())
```

### 9. AWS Step Functions — TransferBrebStateMachine

**Responsabilidad:** Orquestar el flujo completo de transferencia BRE-B, que es una **transacción distribuida** que requiere callback asíncrono del usuario (OTP). Reemplaza el `transfer-breb` Lambda monolítico anterior por una state machine que coordina múltiples Lambdas con manejo nativo de timeouts, reintentos y compensación.

**Tipo:** Standard Workflow (vs Express) — permite `waitForTaskToken` con timeouts largos y trazabilidad por ejecución.

**Entrada:**

```json
{
  "phoneNumber": "+573001234567",
  "sourceAccount": "1009876543",
  "destinationAccount": "2009876544",
  "amount": 500000,
  "concept": "Pago arriendo",
  "sessionId": "sess-abc123",
  "correlationId": "uuid-..."
}
```

**Salida:**

```json
{
  "success": true,
  "transactionId": "TRX-...",
  "receipt": { /* TransferReceipt */ }
}
```

#### Diagrama de Estados

```mermaid
stateDiagram-v2
    [*] --> ValidateTransfer
    ValidateTransfer --> GenerateOTP: Validación OK
    ValidateTransfer --> NotifyValidationFailed: INSUFFICIENT_FUNDS / INVALID_DEST
    GenerateOTP --> WaitForOTP: OTP enviado por SMS (Pinpoint)
    WaitForOTP --> ValidateOTP: SendTaskSuccess(otp)
    WaitForOTP --> NotifyOTPExpired: Timeout 5 min
    ValidateOTP --> ExecuteTransfer: OTP válido
    ValidateOTP --> WaitForOTP: OTP inválido, reintentos < 3
    ValidateOTP --> NotifyOTPBlocked: 3 intentos fallidos
    ExecuteTransfer --> PublishNotifications: Mock_Core actualizado
    ExecuteTransfer --> NotifyTransferFailed: Error en ejecución
    PublishNotifications --> NotifyUserSuccess: Eventos SQS publicados
    NotifyUserSuccess --> [*]
    NotifyValidationFailed --> [*]
    NotifyOTPExpired --> [*]
    NotifyOTPBlocked --> [*]
    NotifyTransferFailed --> [*]
```

#### Definición ASL (Amazon States Language)

```json
{
  "Comment": "Flujo de transferencia BRE-B con autorización OTP",
  "StartAt": "ValidateTransfer",
  "States": {
    "ValidateTransfer": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:transfer-breb-validate",
      "ResultPath": "$.validation",
      "Next": "GenerateOTP",
      "Catch": [
        {
          "ErrorEquals": ["InsufficientFundsError", "InvalidDestinationError"],
          "ResultPath": "$.error",
          "Next": "NotifyValidationFailed"
        }
      ]
    },
    "GenerateOTP": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
      "Parameters": {
        "FunctionName": "otp-service",
        "Payload": {
          "operation": "generate-and-wait",
          "phoneNumber.$": "$.phoneNumber",
          "transferAmount.$": "$.amount",
          "taskToken.$": "$$.Task.Token"
        }
      },
      "HeartbeatSeconds": 300,
      "ResultPath": "$.otpResult",
      "Next": "ValidateOTP",
      "Catch": [
        {
          "ErrorEquals": ["States.Timeout"],
          "Next": "NotifyOTPExpired"
        },
        {
          "ErrorEquals": ["OTPBlockedError"],
          "Next": "NotifyOTPBlocked"
        }
      ]
    },
    "ValidateOTP": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.otpResult.valid",
          "BooleanEquals": true,
          "Next": "ExecuteTransfer"
        }
      ],
      "Default": "NotifyOTPExpired"
    },
    "ExecuteTransfer": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:transfer-breb-execute",
      "ResultPath": "$.receipt",
      "Next": "PublishNotifications",
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "Next": "NotifyTransferFailed"
        }
      ]
    },
    "PublishNotifications": {
      "Type": "Parallel",
      "ResultPath": "$.notifications",
      "Branches": [
        {
          "StartAt": "PublishEmailEvent",
          "States": {
            "PublishEmailEvent": {
              "Type": "Task",
              "Resource": "arn:aws:states:::sqs:sendMessage",
              "Parameters": {
                "QueueUrl": "${EmailNotificationQueueUrl}",
                "MessageBody": {
                  "type": "transfer_confirmation",
                  "receipt.$": "$.receipt",
                  "correlationId.$": "$.correlationId"
                }
              },
              "End": true
            }
          }
        },
        {
          "StartAt": "PublishSmsEvent",
          "States": {
            "PublishSmsEvent": {
              "Type": "Task",
              "Resource": "arn:aws:states:::sqs:sendMessage",
              "Parameters": {
                "QueueUrl": "${SmsNotificationQueueUrl}",
                "MessageBody": {
                  "type": "transfer_confirmation",
                  "phoneNumber.$": "$.phoneNumber",
                  "amount.$": "$.amount",
                  "correlationId.$": "$.correlationId"
                }
              },
              "End": true
            }
          }
        }
      ],
      "Next": "NotifyUserSuccess"
    },
    "NotifyUserSuccess": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:message-handler-notify",
      "Parameters": {
        "phoneNumber.$": "$.phoneNumber",
        "messageType": "transfer_success",
        "receipt.$": "$.receipt"
      },
      "End": true
    },
    "NotifyValidationFailed": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:message-handler-notify",
      "Parameters": {
        "phoneNumber.$": "$.phoneNumber",
        "messageType": "validation_failed",
        "error.$": "$.error"
      },
      "End": true
    },
    "NotifyOTPExpired": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:message-handler-notify",
      "Parameters": {
        "phoneNumber.$": "$.phoneNumber",
        "messageType": "otp_expired"
      },
      "End": true
    },
    "NotifyOTPBlocked": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:message-handler-notify",
      "Parameters": {
        "phoneNumber.$": "$.phoneNumber",
        "messageType": "otp_blocked"
      },
      "End": true
    },
    "NotifyTransferFailed": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:::function:message-handler-notify",
      "Parameters": {
        "phoneNumber.$": "$.phoneNumber",
        "messageType": "transfer_failed"
      },
      "End": true
    }
  }
}
```

#### Patrón Task Token — Cómo funciona el callback del OTP

Este es el corazón del state machine. Resuelve el problema crítico: **¿cómo "esperar" a que el usuario tipee el OTP sin bloquear una Lambda?**

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant OTP as OTP_Service
    participant DDB_OTP as DynamoDB OTP_Store
    participant PIN as Pinpoint
    participant BC as Bank_Client
    participant TW as Twilio
    participant APIGW as API Gateway
    participant MH as Message_Processor

    SFN->>OTP: invoke con $$.Task.Token
    OTP->>OTP: Generar código 6 dígitos
    OTP->>DDB_OTP: PutItem {phoneNumber, code, taskToken, ttl=5min, attempts=0}
    OTP->>PIN: SendMessages (SMS con el código)
    PIN-->>BC: SMS con OTP
    OTP-->>SFN: Lambda retorna, pero SFN sigue PAUSADO esperando token
    Note over SFN: Ejecución suspendida — no consume Lambda hours

    BC->>TW: Mensaje WhatsApp con el código OTP
    TW->>APIGW: POST /webhook/twilio
    APIGW->>MH: Invoke
    MH->>DDB_OTP: GetItem(phoneNumber)
    MH->>MH: Validar código + attempts
    alt OTP válido
        MH->>SFN: SendTaskSuccess(taskToken, {valid: true})
        Note over SFN: Workflow continúa a ExecuteTransfer
    else OTP inválido
        MH->>DDB_OTP: UpdateItem (attempts++)
        alt attempts < 3
            MH->>TW: "Código incorrecto, intenta de nuevo"
            Note over SFN: Sigue esperando token
        else attempts >= 3
            MH->>SFN: SendTaskFailure(taskToken, OTPBlockedError)
        end
    end
```

**Key insight:** La Lambda OTP_Service termina rápido (solo envía el SMS), pero Step Functions queda esperando hasta que Message_Processor invoque `SendTaskSuccess` o `SendTaskFailure` con el token guardado. Cero costo de Lambda mientras se espera.

#### Tabla DynamoDB extendida — OTP_Store con TaskToken

| Attribute | Type | Description |
| --------- | ---- | ----------- |
| `pk` | String (PK) | phoneNumber (E.164) |
| `code` | String | Código OTP de 6 dígitos |
| `taskToken` | String | Token de Step Functions para resumir el workflow |
| `executionArn` | String | ARN de la ejecución de Step Functions (para auditoría) |
| `attempts` | Number | Intentos fallidos (max 3) |
| `transferContext` | Map | Datos de la transferencia (monto, destino) para mostrar al validar |
| `createdAt` | String | ISO 8601 |
| `ttl` | Number | Unix epoch + 300s (5 min) |

### 10. Notificaciones Asíncronas con SQS

**Responsabilidad:** Desacoplar el envío de notificaciones (email, SMS de confirmación) del flujo principal. Las Lambdas productoras solo publican eventos a una cola; las consumidoras los procesan independientemente con reintentos automáticos.

#### Arquitectura

```mermaid
graph LR
    SFN[Step Functions<br/>TransferBreb] -->|publish event| Q1[SQS<br/>email-notification-queue]
    STM[statement-generator] -->|publish event| Q1
    SFN -->|publish event| Q2[SQS<br/>sms-notification-queue]
    Q1 -->|Event Source Mapping<br/>batch=10| EMS[Email_Service Lambda]
    Q2 -->|Event Source Mapping<br/>batch=10| SMS[SMS_Service Lambda]
    EMS --> SES[Amazon SES]
    EMS --> S3[S3 - statement PDF]
    SMS --> PIN[AWS Pinpoint]
    Q1 -.->|fallos > maxReceiveCount| DLQ1[email-dlq]
    Q2 -.->|fallos > maxReceiveCount| DLQ2[sms-dlq]
```

#### Configuración de las colas

**email-notification-queue:**

| Setting | Valor | Razón |
| ------- | ----- | ----- |
| Visibility Timeout | 60s | Cubre Email_Service timeout (15s) + buffer |
| Message Retention | 4 días | Tiempo razonable de retención si el consumer está caído |
| maxReceiveCount | 3 | Después de 3 fallos consecutivos → DLQ |
| Receive Wait Time | 20s | Long polling — reduce empty receives |
| Encryption | SSE-SQS | AWS managed key |
| DLQ | email-dlq | Para inspección manual de fallos |

**sms-notification-queue:** Misma configuración con DLQ `sms-dlq`.

#### Esquema de eventos (contrato productor ↔ consumidor)

```python
from typing import TypedDict, Literal

# Evento publicado a email-notification-queue
class EmailNotificationEvent(TypedDict):
    type: Literal["transfer_confirmation"]
    correlationId: str   # Para tracing
    to: str              # Email del cliente
    payload: dict        # receipt+clientName

# Evento publicado a sms-notification-queue
class SmsNotificationEvent(TypedDict):
    type: Literal["transfer_confirmation"]
    correlationId: str
    phoneNumber: str          # E.164
    amount: float
    destinationAccount: str   # Ya enmascarado
```

#### Event Source Mapping (CloudFormation — definido en `BTG-ConnectAI`)

> Las colas viven en `infra`; el Event Source Mapping que las ata a las Lambdas consumidoras se define en `BTG-ConnectAI` (repo dueño del cómputo), usando el ARN de la cola resuelto del contrato (`Fn::ImportValue` / SSM).

```yaml
EmailServiceEventSourceMapping:
  Type: AWS::Lambda::EventSourceMapping
  Properties:
    EventSourceArn: !Ref EmailQueueArn        # contrato: de infra (Fn::ImportValue / SSM)
    FunctionName: !Ref EmailServiceFunction
    BatchSize: 10
    MaximumBatchingWindowInSeconds: 5
    FunctionResponseTypes:
      - ReportBatchItemFailures   # Solo reintenta los mensajes fallidos del batch
```

#### Productores publican via boto3

El único productor de `email-notification-queue` es Step Functions (estado `PublishNotifications`), que publica el evento `transfer_confirmation` por SDK integration directo. El `statement-generator` NO publica a esta cola — el extracto se entrega exclusivamente por WhatsApp (ver Requisito 9).

```yaml
# ASL del estado PublishNotifications dentro del TransferBrebStateMachine
PublishNotifications:
  Type: Parallel
  Branches:
    - StartAt: PublishEmail
      States:
        PublishEmail:
          Type: Task
          Resource: arn:aws:states:::sqs:sendMessage
          Parameters:
            QueueUrl.$: $.emailQueueUrl
            MessageBody:
              type: transfer_confirmation
              correlationId.$: $.correlationId
              to.$: $.client.email
              payload.$: $.receipt
          End: true
    - StartAt: PublishSms
      States:
        PublishSms:
          Type: Task
          Resource: arn:aws:states:::sqs:sendMessage
          Parameters:
            QueueUrl.$: $.smsQueueUrl
            MessageBody:
              type: transfer_confirmation
              correlationId.$: $.correlationId
              phoneNumber.$: $.client.phoneNumber
              amount.$: $.receipt.amount
              destinationAccount.$: $.receipt.destinationAccountMasked
          End: true
```

#### Beneficios del patrón

- **Resiliencia**: SES caído → mensajes se acumulan en SQS, se procesan cuando se recupere
- **Escalabilidad independiente**: Email_Service puede escalar a 1000 invocaciones concurrentes sin afectar el flujo principal
- **Reintentos automáticos**: SQS reintenta hasta `maxReceiveCount`, luego envía a DLQ
- **Batch processing**: 10 emails por invocación reduce costo
- **Observabilidad**: Métrica `ApproximateAgeOfOldestMessage` por cola alerta si los consumidores están atrasados

### 11. Bedrock Guardrails

**Responsabilidad:** Filtrar contenido inapropiado, restringir respuestas al dominio bancario, bloquear asesoría financiera personalizada y prevenir prompt injection.

#### Configuración

El Guardrail se define como recurso CloudFormation `AWS::Bedrock::Guardrail` **en el repo `infra`** (junto con el Bedrock Agent Core). El `Strands_Agent` (Lambda en `BTG-ConnectAI`) lo referencia al invocar el modelo (parámetro `guardrailIdentifier`/`guardrailVersion` en el `converse`/`invoke_model`), resolviendo `GuardrailId`/`GuardrailVersion` del contrato cross-stack (`Fn::ImportValue` / SSM). La definición de referencia es:

```yaml
BtgConnectAiGuardrail:
  Type: AWS::Bedrock::Guardrail
  Properties:
    Name: btg-connectai-guardrail
    Description: Guardrail para asistente bancario BTG Pactual Colombia
    BlockedInputMessaging: "Lo siento, no puedo procesar esa solicitud. Solo puedo ayudarte con: consulta de saldos, transferencias BRE-B y generación de extractos bancarios de BTG Pactual."
    BlockedOutputsMessaging: "Lo siento, no puedo proporcionar esa información. ¿Puedo ayudarte con consulta de saldos, transferencias o extractos bancarios?"
    ContentPolicyConfig:
      FiltersConfig:
        - Type: SEXUAL
          InputStrength: HIGH
          OutputStrength: HIGH
        - Type: VIOLENCE
          InputStrength: HIGH
          OutputStrength: HIGH
        - Type: HATE
          InputStrength: HIGH
          OutputStrength: HIGH
        - Type: INSULTS
          InputStrength: MEDIUM
          OutputStrength: HIGH
        - Type: MISCONDUCT
          InputStrength: HIGH
          OutputStrength: HIGH
        - Type: PROMPT_ATTACK
          InputStrength: HIGH
          OutputStrength: NONE
    TopicPolicyConfig:
      TopicsConfig:
        - Name: investment-advice
          Type: DENY
          Definition: "Recomendaciones específicas de inversión, compra o venta de activos financieros, sugerencias sobre portafolio"
          Examples:
            - "¿Debería invertir en acciones de X?"
            - "¿Es buen momento para comprar dólares?"
            - "Recomiéndame un CDT"
            - "¿Qué fondo me conviene más?"
        - Name: non-banking-topics
          Type: DENY
          Definition: "Temas no relacionados con servicios bancarios de BTG Pactual como política, deportes, entretenimiento, salud, cocina"
          Examples:
            - "¿Quién ganó el partido ayer?"
            - "¿Qué opinas del presidente?"
            - "Dame una receta de cocina"
            - "¿Cómo está el clima?"
        - Name: competitor-info
          Type: DENY
          Definition: "Información sobre productos o servicios de otros bancos o entidades financieras competidoras"
          Examples:
            - "¿Qué tasas ofrece Bancolombia?"
            - "Compara BTG con Davivienda"
            - "¿Es mejor un CDT en Nequi?"
```


### 12. Observability Stack

El Dashboard, las Alarms y el tópico SNS de alarmas se definen como recursos CloudFormation (`AWS::CloudWatch::Dashboard`, `AWS::CloudWatch::Alarm`, `AWS::SNS::Topic`) **en el repo `infra`** (observabilidad compartida). El Dashboard incluye widgets de invocations/errors/latency p50/p90 para cada Lambda (Webhook_Receiver, Message_Processor, Auth_Service, Strands_Agent, OTP_Service, Email_Service, SMS_Service, balance-query, transfer-breb-validate, transfer-breb-execute, statement-generator, message-handler-notify), más métricas de Step Functions y SQS. Como las Lambdas se monitorean por nombre (`FunctionName`), `infra` usa la convención de naming del proyecto para construir las dimensiones y/o consume del contrato los nombres de función publicados por `BTG-ConnectAI`. El `AlarmsTopicArn` forma parte del contrato.

#### CloudWatch Alarms (CloudFormation — definido en `infra`)

> Patrón ilustrativo. La alarma vive en `infra`; `MessageProcessorFunction` representa el **nombre de función** publicado por `BTG-ConnectAI` en el contrato (o derivado de la convención de naming), y `AlarmsTopic` es el tópico SNS de alarmas de `infra`.

```yaml
# Patrón de alarma error-rate por Lambda (math expression errors/invocations*100 > 10%)
MessageProcessorErrorRateAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: btg-connectai-message-processor-error-rate
    AlarmDescription: "Error rate > 10% en ventana de 5 min"
    Threshold: 10
    ComparisonOperator: GreaterThanThreshold
    EvaluationPeriods: 1
    AlarmActions:
      - !Ref AlarmsTopic
    Metrics:
      - Id: error_rate
        Expression: "(errors / invocations) * 100"
        Label: ErrorRatePercent
      - Id: errors
        ReturnData: false
        MetricStat:
          Metric:
            Namespace: AWS/Lambda
            MetricName: Errors
            Dimensions:
              - Name: FunctionName
                Value: !Ref MessageProcessorFunction
          Period: 300
          Stat: Sum
      - Id: invocations
        ReturnData: false
        MetricStat:
          Metric:
            Namespace: AWS/Lambda
            MetricName: Invocations
            Dimensions:
              - Name: FunctionName
                Value: !Ref MessageProcessorFunction
          Period: 300
          Stat: Sum
```

> Se replica este patrón de alarma para cada Lambda. Adicionalmente:
> - **Webhook_Receiver latency p99 > 1000ms** (rompe SLO de respuesta async a Twilio)
> - **Step Functions `ExecutionsFailed` > 5 en 5min** sobre TransferBrebStateMachine
> - **SQS DLQ `ApproximateNumberOfMessagesVisible` > 0** en inbound-messages-dlq, email-dlq, sms-dlq
> - **SQS `ApproximateAgeOfOldestMessage` > 60s** en inbound-messages-queue (Processor saturado)

### 13. Infrastructure as Code (CloudFormation) — Reparto entre repos

La IaC está **repartida en dos repositorios** con un límite claro de responsabilidad. Ambos usan CloudFormation puro (YAML), nested stacks, tags obligatorios y deploy via GitHub Actions con OIDC.

| Recurso | Repo dueño de la IaC | Notas |
| ------- | -------------------- | ----- |
| API Gateway (HTTP API) + ruta `/webhook/twilio` | `infra` | La **integración** con `Webhook_Receiver` se resuelve por referencia (ver wiring cruzado) |
| SQS FIFO `inbound-messages-queue.fifo` + `inbound-messages-dlq` | `infra` | |
| SQS `email-notification-queue` / `sms-notification-queue` + `email-dlq` / `sms-dlq` | `infra` | |
| S3: Statement_Bucket, Audio_Temp, Login_Page, artefactos de Lambda | `infra` | |
| DynamoDB: Consent_Store, Auth_Session, OTP_Store | `infra` | |
| Bedrock: Agent Core + Guardrails | `infra` | |
| VPC access: Security Groups (`BankingLambdaSG`), VPC Endpoints (S3/DynamoDB) | `infra` (módulo networking) | VPC/subnets ya existen en `IA-Builder-sandbox-networking` |
| Secrets Manager (Twilio creds), SNS de alarmas, Dashboard/Alarms compartidos | `infra` | |
| **Lambdas** (Webhook_Receiver, Message_Processor, Auth_Service, balance-query, transfer-breb-validate/execute/initiator, statement-generator, OTP_Service, Email_Service, SMS_Service, Strands_Agent) | **`BTG-ConnectAI`** | Incluye IAM roles de cada Lambda |
| **State machine** `TransferBrebStateMachine` (+ su IAM role) | **`BTG-ConnectAI`** | |
| **Event Source Mappings** (SQS → Lambda) | **`BTG-ConnectAI`** | Atan colas de `infra` a Lambdas de la app (ver wiring cruzado) |
| Código de aplicación (Python 3.13) y Login_Page estática | **`BTG-ConnectAI`** | El bucket que la sirve es de `infra`; el contenido lo publica la app |

#### Estructura del repo `infra` (resumen — gobernado por sus propios lineamientos)

```text
cloudformation/
├── templates/
│   ├── networking/          # vpc, subnets, security-groups (+ BankingLambdaSG), vpc-endpoints
│   └── connectai-shared/    # api-gateway, queues, storage, data (DynamoDB), secrets,
│                            #   bedrock (agent + guardrails), sns-alarms, observability
└── stacks/
    └── sandbox/
        ├── networking.yaml          # ya existe
        └── connectai-shared.yaml    # stack raíz de recursos compartidos; publica Outputs/Export + SSM
```

> El reparto exacto de templates dentro de `infra` lo determina ese repo según sus convenciones (`templates/<modulo>/` + `stacks/<ambiente>/`, un template por recurso lógico, tags `Environment`/`Project`/`ManagedBy: cloudformation`). Aquí solo se documenta la **frontera** y el **contrato** que `BTG-ConnectAI` consume.

#### Estructura de repositorio `BTG-ConnectAI` (solo cómputo + orquestación)

```text
cloudformation/
├── templates/
│   └── connectai/
│       ├── iam.yaml                  # IAM roles de todas las Lambdas + role del state machine
│       ├── lambdas-ingestion.yaml    # webhook-receiver, message-processor + EventSourceMapping (SQS infra → Lambda)
│       ├── lambdas-actions.yaml      # balance-query, transfer-breb-validate/execute/initiator, statement-generator
│       ├── lambdas-notify.yaml       # otp-service, email-service, sms-service (+ EventSourceMappings) , message-handler-notify
│       ├── lambda-ai-agent.yaml      # strands agent
│       └── state-machine.yaml        # AWS::StepFunctions::StateMachine (carga ASL)
├── stacks/
│   └── sandbox/
│       └── connectai-app.yaml        # Stack raíz: nested stacks de Lambdas/SFN + resolución del contrato
│                                     #   (Fn::ImportValue / {{resolve:ssm:...}}) publicado por `infra`
├── state-machines/
│   └── transfer-breb.asl.json        # Definición Amazon States Language
└── .github/workflows/
    └── cfn-deploy.yml                # Deploy via OIDC (mismo patrón que infra); corre DESPUÉS de infra
```

> Ya **no** existen en `BTG-ConnectAI` los templates `data.yaml`, `storage.yaml`, `queues.yaml`, `secrets.yaml`, `guardrails.yaml`, `api-gateway.yaml` ni `observability.yaml`: esos recursos los crea `infra`. Lo que antes los referenciaba con `!GetAtt`/`!Ref` local ahora los resuelve con `Fn::ImportValue` / SSM (ver siguiente sección).

#### Integración Cross-Repo / Cross-Stack (el contrato)

Como las Lambdas y la state machine viven en un stack distinto (y en un repo distinto) de los recursos que consumen, se define un **contrato explícito** publicado por `infra` y consumido por `BTG-ConnectAI`. Se usan dos mecanismos complementarios:

1. **CloudFormation `Outputs` + `Export` → `Fn::ImportValue`** (acoplamiento fuerte, misma cuenta/region). `infra` exporta cada recurso; la app importa el valor. Ventaja: CloudFormation **bloquea el borrado** de un recurso exportado mientras exista un import (protege contra romper la app). Desventaja: crea dependencia dura entre stacks.

2. **SSM Parameter Store** (acoplamiento débil — **recomendado entre repos distintos**). `infra` escribe parámetros `String` con los identificadores; la app los lee en deploy-time con `{{resolve:ssm:/btgconnectai/sandbox/<recurso>}}` o, para valores dinámicos, como `Parameter` de tipo `AWS::SSM::Parameter::Value<String>`. Ventaja: desacopla los ciclos de vida de los stacks/repos y evita el bloqueo de borrado; el contrato es un namespace de parámetros estable, no una dependencia de stack.

**Recomendación:** usar **SSM Parameter Store como contrato principal entre repos** (desacople), y reservar `Export`/`ImportValue` para casos donde se quiera la protección de borrado de CloudFormation dentro de la misma cuenta. Ambos comparten la misma convención de nombres.

##### Convención de nombres del contrato

Alineada a `${ProjectName}-${Environment}-<Recurso>` (export) y a un namespace SSM equivalente:

| Recurso (en `infra`) | Export Name (CFN) | SSM Parameter |
| -------------------- | ----------------- | ------------- |
| API Gateway HTTP API ID | `BTGConnectAI-sandbox-HttpApiId` | `/btgconnectai/sandbox/api/http-api-id` |
| API Gateway execution endpoint | `BTGConnectAI-sandbox-HttpApiEndpoint` | `/btgconnectai/sandbox/api/endpoint` |
| Inbound queue ARN / URL | `BTGConnectAI-sandbox-InboundQueueArn` / `...-InboundQueueUrl` | `/btgconnectai/sandbox/sqs/inbound-arn` · `.../inbound-url` |
| Email queue ARN / URL | `BTGConnectAI-sandbox-EmailQueueArn` / `...-EmailQueueUrl` | `/btgconnectai/sandbox/sqs/email-arn` · `.../email-url` |
| SMS queue ARN / URL | `BTGConnectAI-sandbox-SmsQueueArn` / `...-SmsQueueUrl` | `/btgconnectai/sandbox/sqs/sms-arn` · `.../sms-url` |
| Consent_Store name / ARN | `BTGConnectAI-sandbox-ConsentTableName` / `...-ConsentTableArn` | `/btgconnectai/sandbox/ddb/consent-name` · `.../consent-arn` |
| Auth_Session name / ARN | `BTGConnectAI-sandbox-AuthTableName` / `...-AuthTableArn` | `/btgconnectai/sandbox/ddb/auth-name` · `.../auth-arn` |
| OTP_Store name / ARN | `BTGConnectAI-sandbox-OtpTableName` / `...-OtpTableArn` | `/btgconnectai/sandbox/ddb/otp-name` · `.../otp-arn` |
| Statement_Bucket name / ARN | `BTGConnectAI-sandbox-StatementBucketName` / `...-StatementBucketArn` | `/btgconnectai/sandbox/s3/statement-name` · `.../statement-arn` |
| Audio_Temp bucket name / ARN | `BTGConnectAI-sandbox-AudioTempBucketName` / `...-AudioTempBucketArn` | `/btgconnectai/sandbox/s3/audio-temp-name` · `.../audio-temp-arn` |
| Bedrock Agent ARN / ID | `BTGConnectAI-sandbox-BedrockAgentArn` / `...-BedrockAgentId` | `/btgconnectai/sandbox/bedrock/agent-arn` · `.../agent-id` |
| Bedrock Guardrail ID / version | `BTGConnectAI-sandbox-GuardrailId` / `...-GuardrailVersion` | `/btgconnectai/sandbox/bedrock/guardrail-id` · `.../guardrail-version` |
| Twilio secret ARN | `BTGConnectAI-sandbox-TwilioSecretArn` | `/btgconnectai/sandbox/secrets/twilio-arn` |
| SNS alarms topic ARN | `BTGConnectAI-sandbox-AlarmsTopicArn` | `/btgconnectai/sandbox/sns/alarms-arn` |
| Banking Lambda SG ID | `BTGConnectAI-sandbox-BankingLambdaSGId` | `/btgconnectai/sandbox/vpc/banking-sg-id` |
| Private Subnet IDs (CSV) | `BTGConnectAI-sandbox-PrivateSubnetIds` | `/btgconnectai/sandbox/vpc/private-subnet-ids` |

> El stack raíz de la app (`connectai-app.yaml`) declara estos valores como `Parameters` resueltos vía SSM (`AWS::SSM::Parameter::Value<String>`) o los importa con `Fn::ImportValue`, y los pasa por nested stack a cada template de Lambda/SFN. Ejemplo de `VpcConfig` para las Lambdas bancarias:
>
> ```yaml
> VpcConfig:
>   SecurityGroupIds:
>     - !Ref BankingLambdaSGId            # parámetro resuelto desde SSM (o Fn::ImportValue)
>   SubnetIds: !Split [",", !Ref PrivateSubnetIds]
> ```

##### Orden de despliegue y dependencias

```mermaid
flowchart LR
    A["infra: networking.yaml\n(VPC, subnets, SGs, endpoints)"] --> B["infra: connectai-shared.yaml\n(API GW, SQS, S3, DynamoDB, Bedrock,\nSecrets, SNS) → publica Outputs/Export + SSM"]
    B --> C["BTG-ConnectAI: connectai-app.yaml\n(Lambdas + State Machine + ESMs)\nresuelve el contrato (ImportValue/SSM)"]
```

1. **`infra` primero**: despliega networking y luego los recursos compartidos, publicando el contrato (Exports + SSM).
2. **`BTG-ConnectAI` después**: su pipeline (`cfn-deploy.yml`) resuelve el contrato y despliega Lambdas, state machine y Event Source Mappings. Si un parámetro/Export no existe, el deploy **falla rápido** — señal de que `infra` no se ha desplegado o cambió el contrato.
3. **Cambios de contrato**: renombrar o eliminar un Export en `infra` requiere coordinar con la app (CloudFormation bloquea el borrado de Exports en uso). Con SSM, el cambio no bloquea pero la app fallaría en el siguiente deploy si el parámetro desaparece — por eso el namespace SSM se trata como **API estable**.

##### Wiring que cruza el límite entre repos

| Wiring | Recurso origen (dueño) | Recurso destino (dueño) | ¿Dónde se define? | Cómo se resuelve la dependencia |
| ------ | ---------------------- | ----------------------- | ----------------- | ------------------------------- |
| API Gateway → Webhook_Receiver | API GW HTTP API (`infra`) | `Webhook_Receiver` Lambda (`BTG-ConnectAI`) | **App** define la **Integration** + **Route** + `AWS::Lambda::Permission`, usando el `HttpApiId` importado de `infra`. Así el wiring vive junto al cómputo que lo necesita y `infra` no depende de ARNs de la app | App importa `HttpApiId`; crea `AWS::ApiGatewayV2::Integration` (`AWS_PROXY` → ARN del Lambda) + `AWS::ApiGatewayV2::Route` (`POST /webhook/twilio`) + permiso de invocación |
| SQS inbound → Message_Processor | `inbound-messages-queue.fifo` (`infra`) | `Message_Processor` (`BTG-ConnectAI`) | **App** define el `AWS::Lambda::EventSourceMapping` | App importa `InboundQueueArn`; el role del Processor recibe `sqs:ReceiveMessage/...` sobre ese ARN |
| SQS email/sms → Email_Service/SMS_Service | `email/sms-notification-queue` (`infra`) | `Email_Service`/`SMS_Service` (`BTG-ConnectAI`) | **App** define los `EventSourceMapping` | App importa `EmailQueueArn`/`SmsQueueArn` |
| Bedrock Agent → Action Group Lambdas | Bedrock Agent (`infra`) | `balance-query`, `transfer-breb-initiator`, `statement-generator` (`BTG-ConnectAI`) | **Dividido**: `infra` define el Agent y sus Action Groups apuntando a los ARNs de las Lambdas; la **app** otorga `AWS::Lambda::Permission` para que `bedrock.amazonaws.com` (con `SourceArn` = Agent ARN) invoque sus Lambdas | Dependencia circular potencial → se rompe con el **orden de despliegue**: la app expone los ARNs de sus Lambdas vía SSM/Export; `infra` los consume para configurar los Action Groups en un paso posterior, o se usa un `AgentAliasArn` estable como `SourceArn` en el permiso. La app importa `BedrockAgentArn` para el `SourceArn` del permiso |
| State machine → SQS notificaciones | `TransferBrebStateMachine` (`BTG-ConnectAI`) | `email/sms-notification-queue` (`infra`) | **App** (es tarea de la state machine) | El role del state machine recibe `sqs:SendMessage` sobre los ARNs importados |
| Lambdas → DynamoDB / S3 / Secrets | Tablas, buckets, secret (`infra`) | Lambdas (`BTG-ConnectAI`) | **App** (políticas IAM de cada Lambda) | Los ARNs vienen del contrato; ver IAM más abajo |

> **Regla general del wiring**: cada *trigger/integration/permiso* se define en el repo **dueño del cómputo** (la app), usando como entrada los identificadores del recurso compartido (que vienen de `infra`). Esto evita que `infra` tenga que conocer los ARNs de Lambdas concretas y mantiene a `infra` agnóstico de la aplicación. La única excepción es la definición del **Action Group** del Bedrock Agent, que por modelo de recurso vive con el Agent en `infra` y se complementa con el `Lambda::Permission` del lado de la app.

#### Empaquetado de Lambdas

Las Lambdas Python se empaquetan como ZIP y se suben al **bucket de artefactos creado por `infra`** antes del deploy. El pipeline (`cfn-deploy.yml` de `BTG-ConnectAI`) hace:

1. `pip install -r src/shared/requirements.txt -t layer/python/` y zip → Lambda Layer compartido (twilio, aws-lambda-powertools, código `shared/`)
2. Por cada Lambda: zip del código → `s3://<artefactos-bucket-infra>/lambdas/<nombre>-<git-sha>.zip` (el nombre del bucket se resuelve del contrato SSM/Export)
3. `aws cloudformation deploy` del stack `connectai-app.yaml` con `--parameter-overrides LambdaCodeKey=<sha>` y los parámetros del contrato
4. Cada `AWS::Lambda::Function` usa `Code: {S3Bucket, S3Key}` y `Layers: [!Ref SharedLayer]`

#### IAM Roles (Least Privilege) — CloudFormation (en `BTG-ConnectAI`)

Los IAM roles de las Lambdas y del state machine se definen en `BTG-ConnectAI` (son específicos del cómputo). La diferencia respecto al diseño anterior es que los **ARNs de los recursos ya no salen de `!GetAtt` local** sino del contrato importado de `infra` (`Fn::ImportValue` o parámetro SSM). Ejemplo:

```yaml
# Webhook_Receiver Role — minimalista, FUERA de VPC (basic execution role)
WebhookReceiverRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal: { Service: lambda.amazonaws.com }
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    Policies:
      - PolicyName: webhook-receiver-policy
        PolicyDocument:
          Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action: sqs:SendMessage
              Resource: !Ref InboundQueueArn        # contrato: Fn::ImportValue / SSM (de infra)
            - Effect: Allow
              Action: secretsmanager:GetSecretValue
              Resource: !Ref TwilioSecretArn         # contrato: Fn::ImportValue / SSM (de infra)

# Message_Processor Role — el trabajo pesado, FUERA de VPC (necesita llamar a Twilio)
MessageProcessorRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal: { Service: lambda.amazonaws.com }
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    Policies:
      - PolicyName: message-processor-policy
        PolicyDocument:
          Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action: [sqs:ReceiveMessage, sqs:DeleteMessage, sqs:ChangeMessageVisibility, sqs:GetQueueAttributes]
              Resource: !Ref InboundQueueArn          # de infra
            - Effect: Allow
              Action: [dynamodb:PutItem, dynamodb:GetItem]
              Resource: !Ref ConsentTableArn           # de infra
            - Effect: Allow
              Action: dynamodb:GetItem
              Resource: !Ref AuthTableArn              # de infra
            - Effect: Allow
              Action: [dynamodb:GetItem, dynamodb:UpdateItem, dynamodb:DeleteItem]
              Resource: !Ref OtpTableArn               # de infra
            - Effect: Allow
              Action: lambda:InvokeFunction
              Resource: !GetAtt StrandsAgentFunction.Arn   # recurso local (app)
            - Effect: Allow
              Action: [states:SendTaskSuccess, states:SendTaskFailure]
              Resource: !Ref TransferBrebStateMachineArn   # recurso local (app)
            - Effect: Allow
              Action: [transcribe:StartTranscriptionJob, transcribe:GetTranscriptionJob]
              Resource: "*"
            - Effect: Allow
              Action: [s3:PutObject, s3:GetObject, s3:DeleteObject]
              Resource: !Sub "${AudioTempBucketArn}/*"     # de infra
            - Effect: Allow
              Action: s3:GetObject
              Resource: !Sub "${StatementBucketArn}/*"      # de infra
            - Effect: Allow
              Action: secretsmanager:GetSecretValue
              Resource: !Ref TwilioSecretArn                # de infra
```

> **Implicación IAM del reparto**: las Lambdas de la app necesitan permisos sobre recursos cuyo **ARN proviene de `infra`**. Como las políticas IAM viven en `BTG-ConnectAI`, los ARNs entran como parámetros del contrato (SSM/Export) y se referencian con `!Ref`/`!Sub`. Esto mantiene el principio de **least privilege** (cada Lambda solo accede a los ARNs concretos que necesita) sin que `infra` tenga que conocer los roles de la app. Para S3/DynamoDB se referencia el ARN exacto del recurso; se evita `Resource: "*"` salvo para acciones que no soportan resource-level (p. ej. Transcribe job APIs).

> Roles análogos (CloudFormation `AWS::IAM::Role`) para el resto de Lambdas, todos en `BTG-ConnectAI`. La diferencia clave es el managed policy de ejecución según la ubicación de red:
>
> - **Fuera de VPC** → `AWSLambdaBasicExecutionRole`: `Auth_Service` (DynamoDB Auth_Session PutItem), `otp-service` (DynamoDB OTP_Store + Pinpoint), `email-service` (SES + SQS consume; solo `transfer_confirmation`), `sms-service` (Pinpoint + SQS consume), `strands-agent` (Bedrock InvokeModel + ApplyGuardrail usando `GuardrailId`/`BedrockAgentArn` del contrato + Lambda InvokeFunction de las tools), `transfer-breb-initiator` (Step Functions StartExecution), `message-handler-notify` (Secrets Twilio)
> - **Dentro de VPC** → `AWSLambdaVPCAccessExecutionRole` (dominio bancario, subnets privadas, usando `BankingLambdaSGId` + `PrivateSubnetIds` del contrato): `balance-query` (solo Logs, mock inline), `transfer-breb-validate/execute` (solo Logs, mock inline), `statement-generator` (S3 PutObject sobre `StatementBucketArn` vía Gateway Endpoint — el extracto se entrega por WhatsApp, no por email)
> - El rol del **state machine** (no es Lambda, vive en la app): Lambda InvokeFunction de las tasks (recursos locales) + SQS SendMessage a las colas de notificación (`EmailQueueArn`/`SmsQueueArn` del contrato).

#### Estructura del código de aplicación (`src/`)

```text
src/                                  # Código fuente de las Lambdas (Python 3.13)
├── lambdas/
│   ├── webhook_receiver/
│   │   ├── handler.py                # Entry point (valida firma, encola)
│   │   ├── twilio_signature.py
│   │   ├── parser.py
│   │   └── enqueue.py
│   ├── message_processor/
│   │   ├── handler.py                # SQS handler con BatchProcessor
│   │   ├── consent.py
│   │   ├── auth.py
│   │   ├── transcription.py
│   │   ├── otp_callback.py
│   │   └── messaging.py
│   ├── ai_agent/
│   │   ├── handler.py                # Strands Agent
│   │   ├── agent.py
│   │   ├── tools.py                  # @tool definitions
│   │   └── prompts.py
│   ├── auth_service/
│   │   ├── handler.py
│   │   └── users.py
│   ├── balance_query/
│   │   ├── handler.py
│   │   └── mock_data.py
│   ├── transfer_breb/
│   │   ├── initiator.py              # Tool → StartExecution
│   │   ├── validate.py               # Step Functions task
│   │   ├── execute.py                # Step Functions task
│   │   └── mock_data.py
│   ├── statement_generator/
│   │   ├── handler.py
│   │   ├── pdf_generator.py          # reportlab / fpdf2
│   │   └── mock_data.py
│   ├── otp_service/
│   │   └── handler.py
│   ├── email_service/
│   │   └── handler.py                # SQS-triggered
│   ├── sms_service/
│   │   └── handler.py                # SQS-triggered
│   └── message_handler_notify/
│       └── handler.py                # Llamada por Step Functions
├── shared/                           # Empaquetado como Lambda Layer
│   ├── logger.py                     # Powertools logger config
│   ├── masking.py
│   ├── formatting.py
│   ├── constants.py
│   └── types.py
├── login-page/                       # Sitio estático (browser JS)
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── tests/
│   ├── unit/                         # pytest
│   └── property/                     # hypothesis
├── requirements.txt                  # Dependencias runtime (twilio, aws-lambda-powertools, strands-agents)
└── requirements-dev.txt              # pytest, hypothesis, moto, boto3-stubs
```

## Data Models

> **Nota sobre deduplicación de mensajes entrantes:** Se eliminó la tabla `Dedup` custom. La deduplicación ahora la maneja **SQS FIFO** nativamente con `MessageDeduplicationId = MessageSid` en ventana de 5 minutos. Esto reduce código, latencia y costo de DynamoDB.

### DynamoDB Table: Consent_Store

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String (Partition Key) | Número telefónico del Bank_Client (E.164) |
| `status` | String | `"accepted"` \| `"rejected"` |
| `acceptedAt` | String | ISO 8601 timestamp de aceptación |
| `tcVersion` | String | Versión de los T&C aceptados (e.g., "1.0") |
| `updatedAt` | String | ISO 8601 timestamp de última actualización |

**Table Settings:**
- Billing Mode: PAY_PER_REQUEST
- TTL: No (consentimiento no expira)
- Encryption: AWS managed key (`aws/dynamodb`)
- No GSIs

### DynamoDB Table: Auth_Session

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String (Partition Key) | Número telefónico del Bank_Client (E.164) |
| `sessionId` | String | UUID v4 de la sesión |
| `username` | String | Username del usuario autenticado |
| `name` | String | Nombre completo del usuario |
| `documentId` | String | Documento de identidad (para vincular con Mock_Core) |
| `createdAt` | String | ISO 8601 timestamp de creación |
| `expiresAt` | String | ISO 8601 timestamp de expiración |
| `ttl` | Number | Unix timestamp de expiración (createdAt + 1800s = 30 min) |

**Table Settings:**
- Billing Mode: PAY_PER_REQUEST
- TTL Attribute: `ttl`
- Encryption: AWS managed key (`aws/dynamodb`)
- No GSIs

### Mock_Core Data (Inline en Action Group Lambdas)

```python
from typing import TypedDict, Literal

# Estructura de datos mock compartida por las Action Group Lambdas (Layer compartido)
class MockProduct(TypedDict):
    account_id: str
    account_number: str
    product_type: Literal["fondo_inversion", "cuenta_corriente"]
    product_name: str
    currency: Literal["COP"]
    available_balance: float
    total_balance: float
    cutoff_date: str            # ISO 8601 date

class MockTransaction(TypedDict):
    transaction_id: str
    account_id: str
    date: str                   # ISO 8601 datetime
    description: str            # Max 100 chars
    amount: float
    currency: Literal["COP"]
    type: Literal["credit", "debit"]

class MockClient(TypedDict):
    phone_number: str           # E.164
    name: str
    email: str
    document_id: str
    products: list[MockProduct]
    transactions: list[MockTransaction]


MOCK_CLIENTS: list[MockClient] = [
    {
        "phone_number": "+573001234567",
        "name": "Carlos Rodríguez",
        "email": "carlos.rodriguez@example.com",
        "document_id": "1234567890",
        "products": [
            {"account_id": "ACC-001", "account_number": "2001234567",
             "product_type": "fondo_inversion", "product_name": "Fondo BTG Pactual Liquidez",
             "currency": "COP", "available_balance": 12_500_000.00, "total_balance": 12_500_000.00,
             "cutoff_date": "2024-12-15"},
            {"account_id": "ACC-002", "account_number": "1001234568",
             "product_type": "cuenta_corriente", "product_name": "Cuenta Corriente BTG",
             "currency": "COP", "available_balance": 3_750_000.50, "total_balance": 4_200_000.50,
             "cutoff_date": "2024-12-15"},
        ],
        "transactions": [
            {"transaction_id": "TRX-001", "account_id": "ACC-002", "date": "2024-12-14T10:30:00Z", "description": "Nómina Empresa XYZ", "amount": 5_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-002", "account_id": "ACC-002", "date": "2024-12-13T15:45:00Z", "description": "Pago servicios públicos", "amount": -350_000, "currency": "COP", "type": "debit"},
            {"transaction_id": "TRX-003", "account_id": "ACC-002", "date": "2024-12-12T09:00:00Z", "description": "Transferencia a Fondo", "amount": -2_000_000, "currency": "COP", "type": "debit"},
            {"transaction_id": "TRX-004", "account_id": "ACC-001", "date": "2024-12-12T09:01:00Z", "description": "Aporte desde Cuenta Corriente", "amount": 2_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-005", "account_id": "ACC-002", "date": "2024-12-10T14:20:00Z", "description": "Compra Rappi", "amount": -85_000, "currency": "COP", "type": "debit"},
        ],
    },
    {
        "phone_number": "+573009876543",
        "name": "María López",
        "email": "maria.lopez@example.com",
        "document_id": "0987654321",
        "products": [
            {"account_id": "ACC-003", "account_number": "2009876543",
             "product_type": "fondo_inversion", "product_name": "Fondo BTG Pactual Renta Fija",
             "currency": "COP", "available_balance": 25_000_000.00, "total_balance": 25_000_000.00,
             "cutoff_date": "2024-12-15"},
            {"account_id": "ACC-004", "account_number": "1009876544",
             "product_type": "cuenta_corriente", "product_name": "Cuenta Corriente BTG",
             "currency": "COP", "available_balance": 8_750_000.50, "total_balance": 8_750_000.50,
             "cutoff_date": "2024-12-15"},
        ],
        "transactions": [
            {"transaction_id": "TRX-006", "account_id": "ACC-004", "date": "2024-12-14T08:00:00Z", "description": "Transferencia recibida", "amount": 3_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-007", "account_id": "ACC-004", "date": "2024-12-11T16:30:00Z", "description": "Pago tarjeta de crédito", "amount": -1_500_000, "currency": "COP", "type": "debit"},
        ],
    },
    {
        "phone_number": "+573005551234",
        "name": "Juan García",
        "email": "juan.garcia@example.com",
        "document_id": "1122334455",
        "products": [
            {"account_id": "ACC-005", "account_number": "1005551234",
             "product_type": "cuenta_corriente", "product_name": "Cuenta Corriente BTG",
             "currency": "COP", "available_balance": 1_200_000.00, "total_balance": 1_200_000.00,
             "cutoff_date": "2024-12-15"},
        ],
        "transactions": [
            {"transaction_id": "TRX-008", "account_id": "ACC-005", "date": "2024-12-13T11:00:00Z", "description": "Depósito efectivo", "amount": 500_000, "currency": "COP", "type": "credit"},
        ],
    },
]
```

### S3 Buckets

#### Audio_Temp_Bucket

- **Purpose:** Almacenamiento temporal de archivos de audio para Amazon Transcribe
- **Lifecycle:** Objetos eliminados automáticamente después de 1 día
- **Encryption:** AWS managed key (`aws/s3`)
- **Access:** Solo Message_Processor Lambda

#### Statement_Bucket

- **Purpose:** Almacenamiento temporal de PDFs de extractos bancarios antes de envío como documento adjunto
- **Lifecycle:** Objetos eliminados automáticamente después de 1 día (PDF se entrega inmediatamente como adjunto)
- **Encryption:** AWS managed key (`aws/s3`)
- **Access:** statement-generator Lambda (write) + Message_Processor Lambda (read/download)
- **Block Public Access:** Enabled (all 4 settings)

### Secrets Manager Structure

```json
{
  "secretName": "btg-connectai/mvp/config",
  "secretValue": {
    "twilioAccountSid": "ACxxxxxxxxxxxxxxxxx",
    "twilioAuthToken": "xxxxxxxxxxxxxxxxxxxx",
    "twilioWhatsAppNumber": "+14155238886",
    "twilioTcTemplateSid": "HXxxxxxxxxxxxxxxxxx",
    "loginPageUrl": "https://d1234567.cloudfront.net",
    "authServiceUrl": "https://xyz123.lambda-url.us-east-1.on.aws"
  }
}
```

### Log Schema (Structured JSON via aws-lambda-powertools)

```python
from typing import TypedDict, Literal, NotRequired

class StructuredLog(TypedDict):
    level: Literal["INFO", "WARNING", "ERROR"]
    message: str
    timestamp: str
    service: str   # "webhook-receiver" | "message-processor" | "ai-agent" | "auth-service" | ...
    correlation_id: str
    function_request_id: str
    # Campos custom
    latency_ms: NotRequired[float]
    status_code: NotRequired[int]
    phone_number_masked: NotRequired[str]   # "****4567"
    message_sid: NotRequired[str]            # Twilio MessageSid
    execution_arn: NotRequired[str]          # Step Functions (transferencias)
    action: NotRequired[str]                 # "consent_check" | "auth_check" | "transcribe" | "invoke_agent" | "otp_callback" | "send_response"
    message_type: NotRequired[str]           # "text" | "audio" | "button"
    auth_event: NotRequired[str]             # "login_success" | "login_failed" | "session_expired"
```

### Data Masking Rules

| Field | Masking Rule | Example |
|-------|-------------|---------|
| Phone number | Retain last 4 digits | `+57300***4567` |
| Account number | Retain last 4 digits | `******4567` |
| Document ID | Retain last 4 digits | `******7890` |
| Username | First char + mask + last char | `c*****z` |


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Message Splitting Round-Trip

*For any* string of arbitrary length, splitting it into chunks of maximum 1600 characters (límite de Twilio WhatsApp) and then concatenating those chunks SHALL produce the original string (minus leading whitespace on subsequent chunks), and every individual chunk SHALL have length ≤ 1600 characters.

**Validates: Requirements 3.10**

### Property 2: SQS FIFO Deduplication (delegada al servicio)

La deduplicación de mensajes entrantes NO es código custom — la garantiza SQS FIFO. *For any* webhook reenviado por Twilio con el mismo `MessageSid`, el `Webhook_Receiver` lo publica con `MessageDeduplicationId = MessageSid`; SQS FIFO SHALL entregar el mensaje al `Message_Processor` exactamente una vez dentro de la ventana de deduplicación de 5 minutos. Esta propiedad se valida con un test de integración (no property-based unit test), enviando el mismo webhook dos veces y verificando una sola invocación del Processor.

**Validates: Requirements 3.4**

### Property 3: Session ID Determinism

*For any* valid E.164 phone number, the `derive_session_id` function SHALL always produce the same session ID for the same phone number (deterministic), and two different phone numbers SHALL produce different session IDs (injective).

**Validates: Requirements 11.1**

### Property 4: Data Masking Correctness

*For any* string representing a sensitive value (phone number, account number, or document ID) with length ≥ 4, the masking function SHALL produce an output where only the last 4 characters of the original are visible, all preceding characters are replaced with a mask character, and the masked output preserves the logical structure (e.g., phone prefix retained).

**Validates: Requirements 14.4**

### Property 5: Consent Gate — Existing Consent Skips T&C

*For any* phone number that has a consent record with status "accepted" in the Consent_Store, the consent check function SHALL return `true` (consent exists), causing the system to skip the T&C flow and proceed to message processing.

**Validates: Requirements 1.4**

### Property 6: Auth Gate — No Session Triggers Login

*For any* phone number that does NOT have an active Auth_Session in DynamoDB (either no record exists or TTL has expired), attempting to execute a banking action SHALL trigger the login flow (return indication that authentication is required).

**Validates: Requirements 5.1, 5.8**

### Property 7: Auth Gate — Active Session Allows Actions

*For any* phone number that has an Auth_Session with a TTL value in the future (not expired), the session validation function SHALL return the session as valid, allowing banking actions to proceed without re-authentication.

**Validates: Requirements 5.6, 6.1, 6.2**

### Property 8: Invalid Credentials Rejection

*For any* username/password combination that does NOT match any entry in the hardcoded test users array, the authentication function SHALL return `success: false` with an error message, and SHALL NOT create an Auth_Session in DynamoDB.

**Validates: Requirements 5.5**

### Property 9: Balance Query Correctness

*For any* phone number that exists in the Mock_Core data, querying the balance SHALL return a response where: (a) all products belonging to that client are included when no filter is specified, (b) each product contains `productType`, `productName`, `currency` (COP), `availableBalance`, `totalBalance`, and `cutoffDate` fields, and (c) all values exactly match the corresponding Mock_Core entries.

**Validates: Requirements 7.1, 7.2, 7.3**

### Property 10: Unknown Client Error

*For any* phone number that does NOT exist in the Mock_Core data, querying the balance or requesting a transfer SHALL return an error response with HTTP status 404 and a descriptive error message indicating no data was found.

**Validates: Requirements 7.4, 8.6**

### Property 11: Transfer Execution Produces Valid Receipt

*For any* valid transfer request (source account exists, belongs to client, has sufficient funds, destination account exists), executing the transfer SHALL produce a receipt containing: `transactionId` (non-empty string), `sourceAccount` (masked), `destinationAccount` (masked), `amount` (matching request), `currency` (COP), `concept`, `executedAt` (valid ISO 8601), and `status` ("COMPLETED").

**Validates: Requirements 8.3**

### Property 12: Insufficient Funds Rejection

*For any* transfer request where the `amount` exceeds the `availableBalance` of the source account in Mock_Core, the transfer function SHALL reject the operation with an error indicating insufficient funds, and the source account balance SHALL remain unchanged.

**Validates: Requirements 8.5**

### Property 13: Future Date Rejection for Statements

*For any* date that is today or in the future (relative to current system time), the statement generation function SHALL reject the request with an error indicating that the cutoff date must be a past date.

**Validates: Requirements 9.2**

### Property 14: Valid Statement Generation with S3 Reference

*For any* valid past cutoff date and existing client/account combination, the statement generation function SHALL produce a result with `success: true`, a non-empty `s3Bucket` string, a non-empty `s3Key` string matching the pattern `statements/{phoneNumber}/{accountId}/{cutoffDate}-{uuid}.pdf`, and a `fileName` string ending in `.pdf`.

**Validates: Requirements 9.3, 9.4**

### Property 15: COP Currency Formatting

*For any* non-negative number, the COP formatting function SHALL produce a string matching the pattern `$X.XXX.XXX,YY` where dots separate thousands and comma separates decimals, with exactly 2 decimal places.

**Validates: Requirements 10.5**

### Property 16: Unsupported Message Format Rejection

*For any* message with type in {"image", "video", "sticker", "document", "location"} (i.e., not "text", "audio", or "interactive"), the message type validation function SHALL classify it as unsupported and return the appropriate error message indicating only text and voice notes are accepted.

**Validates: Requirements 2.5**



## Error Handling

### Error Categories and Responses

| Error Scenario | Component | User-Facing Response | Log Action |
|---------------|-----------|---------------------|------------|
| Non-text/audio message | Message_Processor | "👋 Solo acepto mensajes de texto y notas de voz. Escríbeme o envíame un audio con tu consulta." | INFO log with message type |
| Duplicate message (Twilio retry) | SQS FIFO | None (descartado automáticamente por MessageDeduplicationId) | Visible en métrica SQS, sin invocación del Processor |
| Consent_Store unavailable | Message_Processor | "⚠️ Nuestro servicio está temporalmente no disponible. Por favor intenta de nuevo en unos minutos." | ERROR log with DDB error |
| T&C rejected | Message_Processor | "Para usar nuestros servicios es necesario aceptar los Términos y Condiciones. Cuando estés listo, envíanos un mensaje." | INFO log |
| Audio transcription failed | Message_Processor | "No pude procesar tu nota de voz. Por favor intenta enviarla de nuevo o escríbeme tu consulta como texto." | ERROR log with transcription error |
| Auth_Session expired | Message_Processor | "🔐 Tu sesión ha expirado. Necesitas autenticarte de nuevo para continuar." + login button | INFO log |
| Auth_Session not found | Message_Processor | "🔐 Para ejecutar operaciones bancarias necesitas autenticarte." + login button | INFO log |
| Invalid credentials | Auth_Service | Login_Page shows: "Credenciales incorrectas. Verifica tu usuario y contraseña." | WARN log with masked username |
| Strands Agent timeout (>60s) | Message_Processor | "⚠️ Nuestro servicio está temporalmente no disponible. Por favor intenta de nuevo en unos minutos." | ERROR log with latency; SQS reintenta el mensaje |
| Strands Agent error | Message_Processor | "Lo siento, ocurrió un error procesando tu solicitud. Por favor intenta de nuevo." | ERROR log with error details |
| Guardrails block (input) | Strands Agent | Guardrail's configured blocked input message | WARN log with block reason |
| Guardrails block (output) | Strands Agent | Guardrail's configured blocked output message | WARN log with block reason |
| Client not found in Mock_Core | Action Groups | Agent formats: "No encontré información de cuenta asociada a tu número." | INFO log with masked phone |
| Insufficient funds | transfer-breb | Agent formats: "No tienes fondos suficientes en la cuenta origen para esta transferencia." | INFO log |
| Invalid destination account | transfer-breb | Agent formats: "La cuenta destino no fue encontrada. Verifica el número e intenta de nuevo." | INFO log |
| Future cutoff date | statement-generator | Agent formats: "La fecha de corte debe ser una fecha pasada. Por favor indica una fecha anterior a hoy." | INFO log |
| PDF generation failure | statement-generator | Agent formats: "No pude generar el extracto. Por favor intenta de nuevo." | ERROR log |
| DynamoDB read failure (auth/otp) | Message_Processor | "⚠️ Servicio temporalmente no disponible." | ERROR log; SQS reintenta |
| Secrets Manager failure | All Lambdas | "Servicio temporalmente no disponible." | ERROR log with secret name |
| Twilio messages.create failure | Message_Processor | None (cannot reach user) | ERROR log; SQS reintenta el mensaje completo |

### Retry Strategy

| Operation | Retries | Backoff | Notes |
|-----------|---------|---------|-------|
| SQS SendMessage (Webhook_Receiver) | 2 | boto3 default | Si falla, Twilio reintenta el webhook; SQS FIFO descarta el duplicado |
| DynamoDB GetItem (consent/auth/otp) | 1 | 100ms | Critical path — retry once |
| Strands Agent invoke (Lambda) | 0 | N/A | Timeout at 60s, no retry (SQS reintenta el mensaje completo) |
| Amazon Transcribe | 0 | N/A | Polling con 30s max wait |
| Twilio messages.create | 2 | Exponential (100ms, 200ms) | Important for delivery |
| S3 PutObject (PDF) | 1 | 100ms | Retry once for transient errors |
| Secrets Manager GetSecret | 1 | 100ms | Cached en cold start |
| SQS message processing (Message_Processor) | 3 | Visibility timeout 130s | maxReceiveCount=3 → DLQ |

### Error Response Templates (Spanish)

```python
ERROR_MESSAGES = {
    "unsupported_format": "👋 Solo acepto mensajes de texto y notas de voz. Escríbeme o envíame un audio con tu consulta.",
    "transcription_failed": "🎙️ No pude procesar tu nota de voz. Por favor intenta enviarla de nuevo o escríbeme tu consulta como texto.",
    "service_unavailable": "⚠️ Nuestro servicio está temporalmente no disponible. Por favor intenta de nuevo en unos minutos.",
    "generic_error": "Lo siento, ocurrió un error procesando tu solicitud. Por favor intenta de nuevo.",
    "consent_required": "Para usar nuestros servicios es necesario aceptar los Términos y Condiciones. Cuando estés listo, envíanos un mensaje.",
    "auth_required": "🔐 Para ejecutar operaciones bancarias necesitas autenticarte.",
    "auth_expired": "🔐 Tu sesión ha expirado. Necesitas autenticarte de nuevo para continuar.",
    "auth_success": "✅ ¡Autenticación exitosa! Procesando tu solicitud...",
    "welcome_message": (
        "👋 ¡Bienvenido a BTG ConnectAI! Estos son los servicios disponibles:\n\n"
        "💰 *Consulta de saldos* — Fondos de Inversión y Cuenta Corriente\n"
        "💸 *Transferencias BRE-B* — Entre cuentas\n"
        "📄 *Extractos bancarios* — Generación de PDF\n\n"
        "Puedes solicitarme cualquier servicio en lenguaje natural. ¡Escríbeme o envíame una nota de voz!"
    ),
}
```

### Timeout Configuration

| Component | Timeout | Rationale |
|-----------|---------|-----------|
| Webhook_Receiver Lambda | 10s | Solo valida firma + encola; resuelve <1s |
| Message_Processor Lambda | 120s | Transcripción (30s) + Strands agent (60s) + envío Twilio |
| Auth_Service Lambda | 10s | Simple credential check + DDB write |
| balance-query Lambda | 15s | Mock data instant, buffer for cold start |
| transfer-breb-validate/execute Lambda | 15s | Mock data instant, buffer for cold start |
| statement-generator Lambda | 30s | PDF generation + S3 upload |
| Transcription polling | 30s | Max wait para Amazon Transcribe (async, sin presión) |
| Strands Agent invoke | 60s | Max wait antes de timeout |
| inbound-messages-queue visibility | 130s | Apenas > timeout del Processor (120s) |

## Testing Strategy

### Property-Based Testing (PBT)

**Library:** [hypothesis](https://hypothesis.readthedocs.io/) (Python)  
**Minimum iterations:** 100 per property (`@settings(max_examples=100)`)  
**Tag format:** `Feature: btg-connect-ai-mvp, Property {number}: {title}`

Properties to implement as PBT (con hypothesis):
1. Message splitting round-trip (≤ 1600 chars)
3. Session ID determinism
4. Data masking correctness
5. Consent gate logic
6. Auth gate (no session → login)
7. Auth gate (active session → proceed)
8. Invalid credentials rejection
9. Balance query correctness
10. Unknown client error
11. Transfer receipt validity
12. Insufficient funds rejection (raises `InsufficientFundsError`)
13. Future date rejection
14. Statement generation with S3 reference
15. COP currency formatting
16. Unsupported format rejection

> Property 2 (deduplicación) NO es PBT — la garantiza SQS FIFO y se valida con test de integración.

### Unit Tests (Example-Based)

| Test | Component | What it verifies |
|------|-----------|-----------------|
| T&C interactive message format | Message_Processor | Correct WhatsApp interactive payload structure |
| Welcome message content | Message_Processor | All 3 services listed in welcome |
| Login button message format | Message_Processor | Correct interactive button payload |
| Auth_Service with each test user | Auth_Service | Each of 3 users can authenticate |
| Transfer cancellation | transfer-breb | No state change on cancel |
| Empty statement generation | statement-generator | PDF generated with "no transactions" note |
| Correlation ID generation | All Lambdas | UUID v4 format, attached to logger |
| Log structure validation | All Lambdas | JSON format with required fields |

### Integration Tests

| Test | What it verifies |
|------|-----------------|
| Full consent flow | First message → T&C → accept → welcome |
| Full auth flow | Request → login button → authenticate → process |
| Audio transcription pipeline | Audio upload → Transcribe → text extraction |
| Balance query end-to-end | Auth'd request → agent → balance-query → formatted response |
| Transfer end-to-end | Auth'd request → agent → confirm → transfer-breb → receipt |
| Statement end-to-end | Auth'd request → agent → statement-generator → PDF document attachment via WhatsApp |
| Guardrails blocking | Out-of-domain request → blocked response |
| Session memory | Multi-turn conversation with context |

### CloudFormation Template Validation

> Estas validaciones aplican a los templates que viven en **`BTG-ConnectAI`** (Lambdas + state machine + wiring). Las validaciones de los recursos compartidos (S3 Block Public Access, DynamoDB encryption, ausencia de NAT Gateway, VPC Endpoints, colas FIFO) son responsabilidad del repo **`infra`** y se ejecutan en su propio pipeline. Aquí se verifica además que la app **resuelve el contrato** correctamente.

| Test | Tool | What it verifies |
|------|------|-----------------|
| Template lint | `cfn-lint` | Sintaxis y best practices de los templates YAML de la app |
| VpcConfig solo en dominio bancario | pytest sobre template | `balance-query`, `transfer-breb-validate/execute`, `statement-generator` tienen `VpcConfig` con `BankingLambdaSGId`/`PrivateSubnetIds` resueltos del contrato; el resto NO tiene VpcConfig |
| Contrato resuelto (cross-stack) | pytest sobre template | El stack raíz declara los parámetros del contrato (ARNs/URLs/IDs de `infra`) vía `Fn::ImportValue` o `AWS::SSM::Parameter::Value<String>`; ningún ARN de recurso compartido está hardcodeado |
| IAM policy scoping | `cfn-nag` / `checkov` | Least privilege por Lambda; los `Resource` apuntan a ARNs del contrato, sin wildcards peligrosos |
| Wiring cruzado | pytest sobre template | Integration/Route de API GW, Event Source Mappings y `Lambda::Permission` (Bedrock Agent) referencian identificadores del contrato, no recursos locales inexistentes |
| Lambda runtime | pytest sobre template | Todas las Lambdas usan `python3.13` |

### Test Execution

```bash
# Unit + Property tests (pytest + hypothesis)
pytest src/tests/unit src/tests/property -v

# Validación de templates CloudFormation (solo los de BTG-ConnectAI: Lambdas + SFN + wiring)
cfn-lint cloudformation/templates/connectai/*.yaml cloudformation/stacks/sandbox/*.yaml
checkov -d cloudformation/   # security scanning (IAM, least privilege)

# Validar definición del state machine
aws stepfunctions validate-state-machine-definition \
  --definition file://cloudformation/state-machines/transfer-breb.asl.json

# Integration tests (requiere infra desplegado primero, luego el stack de la app)
pytest src/tests/integration -v --stack-name BTGConnectAI-sandbox-app
```
