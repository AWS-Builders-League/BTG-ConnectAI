# BTG ConnectAI MVP Lite

Asistente bancario conversacional por WhatsApp para BTG Pactual Colombia. El cliente habla en lenguaje natural — texto o nota de voz — y el asistente entiende y ejecuta operaciones bancarias sin menús ni opciones numeradas.

## ¿Qué puede hacer el asistente?

- **Consultar saldos** — "¿Cuánto tengo en mi cuenta?" o "Muéstrame mis fondos"
- **Transferir dinero** — "Quiero transferir 500 mil a la cuenta 1009876544" (con autorización OTP vía SMS)
- **Generar extractos** — "Necesito mi extracto de noviembre" (llega como PDF adjunto en WhatsApp y por email)
- **Entender notas de voz** — El cliente puede hablar en lugar de escribir; el sistema transcribe automáticamente

El asistente responde siempre en español colombiano natural, formatea montos en COP y declina cualquier consulta fuera del dominio bancario.

## Flujo de uso

1. El cliente envía su primer mensaje → recibe los Términos y Condiciones con botón de aceptar
2. Acepta → recibe mensaje de bienvenida con los servicios disponibles
3. Solicita un servicio → el sistema pide autenticación vía enlace en WhatsApp
4. Se autentica en la página de login → sesión activa por 30 minutos
5. Solicita una transferencia → recibe un OTP por SMS para autorizar la operación
6. Ingresa el OTP → la transferencia se ejecuta y recibe confirmación por WhatsApp y email

## Ambiente de despliegue

El proyecto se despliega en la cuenta AWS sandbox en la región **us-east-1**, dentro de la VPC `IA-Builder-sandbox-networking` (CIDR 10.0.0.0/16). Todas las funciones Lambda corren en las **subnets privadas** (10.0.11.0/24 · us-east-1a y 10.0.12.0/24 · us-east-1b), sin exposición directa a internet. El tráfico saliente hacia servicios externos (Twilio, Bedrock, Transcribe) pasa por un **NAT Gateway** ubicado en la subnet pública. El único punto de entrada público es el **API Gateway** que recibe los webhooks de Twilio.

## Patrón async — cómo escala el sistema

El flujo de mensajes está diseñado para que **Twilio nunca espere** al backend más de un segundo:

```text
Twilio → API Gateway → Webhook_Receiver (responde 200 OK en <1s)
                            ↓
                       SQS FIFO inbound-messages-queue
                       MessageGroupId = phoneNumber  (orden por cliente)
                       MessageDeduplicationId = MessageSid  (dedup gratis)
                            ↓
                       Message_Processor (sin presión de tiempo)
                            ├─ Transcribe audio si aplica
                            ├─ Strands Agent (Bedrock)
                            └─ Twilio REST API (respuesta al cliente)
```

Beneficios reales: spikes de tráfico se absorben en la cola, los retries de Twilio se descartan automáticamente, audio que tarda 15s en transcribir no rompe nada, y mañana podemos agregar consumidores (analytics, auditoría) sin tocar el Receiver.

## Tecnología

| Capa | Servicio |
| ---- | ------- |
| Canal de mensajería | Twilio (WhatsApp Sandbox) |
| Punto de entrada | Amazon API Gateway (HTTP API público, expuesto a Twilio) |
| Motor de IA | Strands Agent SDK + Amazon Bedrock Agent Core (Claude Haiku 3.5) |
| Orquestación de transacciones | AWS Step Functions (transferencias BRE-B con OTP) |
| Bus de eventos asíncronos | Amazon SQS (notificaciones de email y SMS con DLQ) |
| Transcripción de voz | Amazon Transcribe (español colombiano) |
| OTP transaccional | AWS Pinpoint (SMS) |
| Notificaciones email | Amazon SES |
| Funciones serverless | AWS Lambda — Node.js 24.x (negocio) · Python 3.12 (IA) |
| Base de datos | DynamoDB (sesiones, consentimiento, deduplicación, OTP) |
| Documentos PDF | S3 (generación y entrega de extractos) |
| Observabilidad | CloudWatch + Lambda Powertools |
| Infraestructura como código | AWS CDK (TypeScript) |

## Estructura del proyecto

```text
├── infra/                          # Infraestructura CDK
│   ├── bin/app.ts
│   └── lib/
│       ├── stacks/
│       ├── constructs/
│       └── config/
├── src/
│   ├── lambdas/
│   │   ├── webhook-receiver/       # Sync, detrás de API Gateway — responde 200 a Twilio
│   │   ├── message-processor/      # Async, SQS-triggered — hace el trabajo pesado
│   │   ├── ai-agent/               # Strands Agent (Python 3.12) — motor conversacional
│   │   ├── auth-service/           # Autenticación vía enlace web (mock para el demo)
│   │   ├── otp-service/            # Generación de OTP (Pinpoint SMS) con task token
│   │   ├── email-service/          # SQS-triggered — envío vía SES
│   │   ├── sms-service/            # SQS-triggered — SMS de confirmación vía Pinpoint
│   │   ├── balance-query/          # Tool: consulta de saldos
│   │   ├── transfer-breb-initiator/   # Tool: dispara TransferBrebStateMachine
│   │   ├── transfer-breb-validate/    # Task de Step Functions
│   │   ├── transfer-breb-execute/     # Task de Step Functions
│   │   ├── statement-generator/    # Tool: extracto PDF, publica a SQS email
│   │   └── message-handler-notify/ # Lambda llamada por Step Functions para responder al cliente
│   ├── shared/                     # Utilidades compartidas (Node.js)
│   └── login-page/                 # Página de login (sitio estático en S3)
└── .kiro/specs/                    # Documentos de especificación
```

## Instalación y despliegue

```bash
# Instalar dependencias
npm install

# Compilar
npm run build

# Desplegar (primera vez: npx cdk bootstrap primero)
cd infra && npx cdk deploy
```

Después del despliegue, configurar la URL del API Gateway (`POST /webhook/twilio`) como webhook en la consola de Twilio Sandbox.

## Pruebas

```bash
# Tests unitarios y de propiedades
npx vitest --run

# Tests de snapshot CDK
cd infra && npx jest --run
```

## Usuarios de prueba

| Usuario | Contraseña | Teléfono |
| ------- | ---------- | -------- |
| carlos.rodriguez | Btg2024*Test | +573001234567 |
| maria.lopez | Btg2024*Demo | +573009876543 |
| juan.garcia | Btg2024*Hack | +573005551234 |

## Seguridad y privacidad

- Cifrado en reposo y en tránsito en todos los servicios
- Datos sensibles enmascarados en logs (solo últimos 4 dígitos de cuentas y teléfonos)
- Credenciales de Twilio y API keys en AWS Secrets Manager
- Guardrails de IA que evitan respuestas fuera del dominio bancario
- OTP con TTL de 5 minutos y bloqueo tras 3 intentos fallidos
- Control de acceso de mínimo privilegio por componente (IAM)

## Alcance del MVP

Este es un demo para hackathon: los datos bancarios son simulados y la autenticación usa usuarios de prueba. No hay integración con el core bancario real.

### Camino a producción

| Extensión | Qué implica |
| --------- | ----------- |
| Core bancario real | Lambdas ya en subnets privadas + NAT Gateway ya desplegado. Agregar conectividad privada al core bancario (PrivateLink o VPN) |
| Canal WhatsApp productivo | Migrar de Twilio Sandbox a número de WhatsApp Business aprobado (Twilio o AWS EUMS) |
| Autenticación real | Integración con el proveedor de identidad corporativo (OAuth2/OIDC) |
| Servicios adicionales | Pagos, apertura de productos, consulta de TRM |
| Auditoría regulatoria | Pipeline de retención 7 años (Kinesis → S3) |
| Cifrado gestionado | Llaves propias (CMK) con rotación anual |
| Observabilidad avanzada | Trazabilidad distribuida con X-Ray |

---

Proyecto interno BTG Pactual Colombia — Hackathon 2026.
