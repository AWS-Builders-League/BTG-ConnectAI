# BTG ConnectAI — Contexto del Proyecto

## Resumen

Asistente bancario conversacional para WhatsApp (BTG Pactual Colombia). El cliente habla en lenguaje natural (texto o nota de voz) y el asistente entiende y ejecuta operaciones bancarias sin menús.

## Stack Tecnológico

- **Runtime**: Python 3.13 (todas las Lambdas)
- **IaC**: CloudFormation puro (YAML) — templates anidados, NO CDK ni SAM
- **AI**: Amazon Bedrock (Claude Haiku 3.5) con Strands Agent SDK
- **Canal**: Twilio WhatsApp Sandbox
- **Orquestación**: AWS Step Functions (Standard) con patrón waitForTaskToken
- **Colas**: Amazon SQS (FIFO para ingesta, Standard para notificaciones)
- **Datos**: DynamoDB (Consent, Auth_Session, OTP_Store)
- **Storage**: S3 (audio temporal, extractos PDF, login page estática)
- **Notificaciones**: Amazon SES (email) + AWS Pinpoint (SMS/OTP)
- **Observabilidad**: CloudWatch + Lambda Powertools (JSON structured logs)
- **Seguridad**: Secrets Manager, Bedrock Guardrails, cifrado AWS managed keys

## Patrones Arquitectónicos Clave

1. **Async Webhook Pattern**: Webhook_Receiver (sync, <1s) → SQS FIFO → Message_Processor (async)
2. **SQS FIFO Dedup**: MessageDeduplicationId=MessageSid, MessageGroupId=phoneNumber
3. **Step Functions + waitForTaskToken**: Orquesta transferencias sin Lambdas bloqueadas
4. **Red híbrida**: Lambdas de dominio bancario en VPC privada (sin NAT), resto fuera de VPC

## Convenciones

- Código Lambda en `src/lambdas/<nombre>/handler.py`
- Dependencias compartidas en Lambda Layer (`src/shared/`)
- Tests con pytest (unit/integration) + hypothesis (property-based)
- Logs en español colombiano para respuestas al cliente
- Montos en formato COP: $X.XXX.XXX,YY
- Datos sensibles enmascarados (últimos 4 dígitos)

## Estructura de CloudFormation

```
cloudformation/
├── templates/connectai/    # Templates anidados individuales
├── stacks/sandbox/         # Stack raíz compuesto
└── state-machines/         # Definiciones ASL
```

## Región AWS

us-east-1 (sandbox)
