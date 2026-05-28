# BTG ConnectAI MVP

Asistente bancario conversacional por WhatsApp para BTG Pactual Colombia. El cliente habla en lenguaje natural — texto o nota de voz — y el asistente entiende y ejecuta operaciones bancarias sin menús ni opciones numeradas.

## ¿Qué puede hacer el asistente?

- **Consultar saldos** — "¿Cuánto tengo en mi cuenta?" o "Muéstrame mis fondos"
- **Transferir dinero** — "Quiero transferir 500 mil a la cuenta 1009876544" (con autorización OTP vía SMS)
- **Generar extractos** — "Necesito mi extracto de noviembre" (llega como PDF adjunto en WhatsApp)
- **Entender notas de voz** — El cliente puede hablar en lugar de escribir; el sistema transcribe automáticamente

El asistente responde siempre en español colombiano natural, formatea montos en COP y declina cualquier consulta fuera del dominio bancario.

## Flujo de uso

1. El cliente envía su primer mensaje → recibe los Términos y Condiciones con botón de aceptar
2. Acepta → recibe mensaje de bienvenida con los servicios disponibles
3. Solicita un servicio → el sistema pide autenticación vía enlace en WhatsApp
4. Se autentica en la página de login → sesión activa por 30 minutos
5. Solicita una transferencia → recibe un OTP por SMS para autorizar la operación
6. Ingresa el OTP → la transferencia se ejecuta y recibe confirmación por WhatsApp y email

## Tecnología

A grandes rasgos, esto es lo que habilita cada capacidad del asistente:

| Capacidad | Servicio |
| --------- | -------- |
| Conversación por WhatsApp | Twilio |
| Inteligencia conversacional | Amazon Bedrock (Claude Haiku 3.5) con Strands Agent |
| Notas de voz | Amazon Transcribe (español colombiano) |
| Código OTP por SMS | AWS Pinpoint |
| Correos de confirmación | Amazon SES |
| Datos y documentos | DynamoDB y S3 |
| Cómputo | AWS Lambda (Python) |

El sistema es 100% serverless y se despliega en AWS. Los detalles técnicos de arquitectura, red e infraestructura están documentados en [.kiro/specs/](.kiro/specs/).

## Estructura del proyecto

```text
├── cloudformation/                 # IaC — CloudFormation puro (YAML)
│   ├── templates/connectai/        # Templates anidados (data, queues, lambdas, state-machine, etc.)
│   ├── stacks/sandbox/             # Stack raíz compuesto
│   └── state-machines/             # Definición ASL del TransferBrebStateMachine
├── src/
│   ├── lambdas/                    # Todas en Python 3.13
│   │   ├── webhook_receiver/       # Sync, detrás de API Gateway — responde 200 a Twilio
│   │   ├── message_processor/      # Async, SQS-triggered — hace el trabajo pesado
│   │   ├── ai_agent/               # Strands Agent — motor conversacional
│   │   ├── auth_service/           # Autenticación vía enlace web (mock para el demo)
│   │   ├── otp_service/            # Generación de OTP (Pinpoint SMS) con task token
│   │   ├── email_service/          # SQS-triggered — envío vía SES
│   │   ├── sms_service/            # SQS-triggered — SMS de confirmación vía Pinpoint
│   │   ├── balance_query/          # Tool: consulta de saldos
│   │   ├── transfer_breb/          # initiator + validate + execute (Step Functions)
│   │   ├── statement_generator/    # Tool: extracto PDF a S3 (entregado por WhatsApp)
│   │   └── message_handler_notify/ # Lambda llamada por Step Functions para responder al cliente
│   ├── shared/                     # Utilidades compartidas (Lambda Layer)
│   ├── login-page/                 # Página de login (sitio estático en S3, browser JS)
│   ├── tests/                      # pytest (unit/property/integration)
│   └── requirements.txt
├── .github/workflows/cfn-deploy.yml  # CI/CD: build + zip Lambdas → S3 + cloudformation deploy
└── .kiro/specs/                    # Documentos de especificación
```

## Instalación y despliegue

```bash
# Dependencias de desarrollo
pip install -r src/requirements-dev.txt

# Validar templates
cfn-lint cloudformation/**/*.yaml

# Desplegar (vía GitHub Actions con OIDC, o manualmente):
aws cloudformation deploy \
  --stack-name BTGConnectAI-sandbox \
  --template-file cloudformation/stacks/sandbox/connectai.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

Después del despliegue, configurar la URL del API Gateway (`POST /webhook/twilio`) como webhook en la consola de Twilio Sandbox.

## Pruebas

```bash
# Tests unitarios y de propiedades (pytest + hypothesis)
pytest src/tests/unit src/tests/property -v

# Validación de infraestructura
cfn-lint cloudformation/**/*.yaml
checkov -d cloudformation/
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
| Core bancario real | Conectar el asistente al core bancario de BTG en lugar de los datos simulados |
| Canal WhatsApp productivo | Migrar del sandbox de Twilio a un número de WhatsApp Business aprobado |
| Autenticación real | Integración con el proveedor de identidad corporativo del banco |
| Servicios adicionales | Pagos, apertura de productos, consulta de TRM |
| Auditoría regulatoria | Retención de operaciones a largo plazo para cumplimiento |
| Seguridad reforzada | Llaves de cifrado propias y trazabilidad avanzada |

---

Proyecto interno BTG Pactual Colombia — Hackathon 2026.
