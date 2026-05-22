# BTG ConnectAI MVP Lite

Asistente bancario conversacional por WhatsApp para BTG Pactual Colombia, impulsado por Amazon Bedrock Agent con Claude Haiku 3.5.

A diferencia de un chatbot tradicional basado en menús, BTG ConnectAI utiliza inteligencia artificial conversacional que entiende lenguaje natural (texto y notas de voz) para ejecutar servicios bancarios en español colombiano.

## Características

- **Entrada multimodal** — Texto y notas de voz (audio OGG/Opus transcrito automáticamente)
- **IA conversacional** — El usuario solicita servicios en lenguaje natural, sin menús
- **Flujo de consentimiento** — Términos y condiciones obligatorios antes de usar el servicio
- **Autenticación vía enlace web** — Login mediante enlace en WhatsApp con sesión temporal (30 min)
- **Servicios bancarios:**
  - 💰 Consulta de saldos (Fondos de Inversión y Cuenta Corriente)
  - 💸 Transferencias BRE-B entre cuentas
  - 📄 Generación de extractos bancarios (PDF adjunto en WhatsApp)
- **Guardrails de IA** — Respuestas restringidas al dominio bancario con Bedrock Guardrails

## Arquitectura

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Canal WhatsApp                               │
│  Bank_Client ↔ WhatsApp ↔ AWS End User Messaging Social            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ SNS
┌──────────────────────────────▼──────────────────────────────────────┐
│                      WhatsApp_Gateway Lambda                         │
│  Dedup → Consent → Audio Transcription → Auth Check → Agent Call    │
└───┬──────────┬──────────┬──────────────────────────────┬────────────┘
    │          │          │                              │
    ▼          ▼          ▼                              ▼
┌────────┐ ┌────────┐ ┌──────────┐          ┌─────────────────────┐
│DynamoDB│ │DynamoDB│ │ Amazon   │          │  Amazon Bedrock     │
│ Dedup  │ │Consent │ │Transcribe│          │  Agent + Guardrails │
│  Auth  │ │ Store  │ │ (es-CO)  │          │  (Claude Haiku 3.5) │
└────────┘ └────────┘ └──────────┘          └──────────┬──────────┘
                                                       │
                                    ┌──────────────────┼──────────────┐
                                    ▼                  ▼              ▼
                             ┌────────────┐  ┌──────────────┐  ┌───────────┐
                             │balance-query│  │transfer-breb │  │statement- │
                             │   Lambda   │  │   Lambda     │  │generator  │
                             └────────────┘  └──────────────┘  └─────┬─────┘
                                                                     │
                                                                     ▼
                                                              ┌─────────────┐
                                                              │ S3 (PDFs)   │
                                                              └─────────────┘
```

### Stack Tecnológico

| Componente | Tecnología |
|-----------|-----------|
| Runtime | TypeScript (Node.js 20.x) |
| IaC | AWS CDK (TypeScript) |
| AI Engine | Amazon Bedrock Agent + Claude Haiku 3.5 |
| Canal WhatsApp | AWS End User Messaging Social |
| Audio | Amazon Transcribe (español colombiano) |
| Base de datos | DynamoDB (PAY_PER_REQUEST) |
| Almacenamiento | S3 (extractos PDF, audio temporal) |
| Observabilidad | Lambda Powertools + CloudWatch |
| Seguridad | IAM roles + AWS managed keys |

### Principios Arquitectónicos

- **Zero VPC** — Lambdas sin VPC, acceso directo a servicios AWS vía endpoints públicos
- **Stateless Lambdas** — Estado conversacional en Bedrock Agent, auth/consent en DynamoDB
- **Serverless completo** — Lambda, DynamoDB, S3, SNS — sin servidores que administrar
- **Cifrado por defecto** — AWS managed keys en reposo, TLS 1.2+ en tránsito
- **Mínimo privilegio** — IAM roles con permisos específicos por Lambda

## Estructura del Proyecto

```
├── infra/                          # AWS CDK Infrastructure
│   ├── bin/app.ts                  # CDK App entry point
│   ├── lib/
│   │   ├── stacks/                 # CDK Stacks
│   │   ├── constructs/             # CDK Constructs (por componente)
│   │   └── config/                 # Configuración de entorno
│   └── cdk.json
├── src/
│   ├── lambdas/
│   │   ├── whatsapp-gateway/       # Punto de entrada del sistema
│   │   ├── auth-service/           # Autenticación mock
│   │   ├── balance-query/          # Action Group: consulta de saldos
│   │   ├── transfer-breb/          # Action Group: transferencias
│   │   └── statement-generator/    # Action Group: extractos PDF
│   ├── shared/                     # Utilidades compartidas
│   │   ├── logger.ts
│   │   ├── masking.ts
│   │   ├── formatting.ts
│   │   ├── constants.ts
│   │   └── types.ts
│   ├── login-page/                 # Página de login (S3 static)
│   └── tests/
│       ├── unit/
│       └── property/               # Property-based tests (fast-check)
├── .kiro/specs/                    # Spec documents
└── README.md
```

## Requisitos Previos

- Node.js 20.x
- AWS CLI configurado con credenciales
- AWS CDK CLI (`npm install -g aws-cdk`)
- Cuenta AWS con acceso a Amazon Bedrock (Claude Haiku 3.5)
- WhatsApp Business Account configurado con AWS End User Messaging Social

## Instalación

```bash
# Clonar el repositorio
git clone <repo-url>
cd BTG-ConnectAI

# Instalar dependencias
npm install

# Compilar TypeScript
npm run build
```

## Despliegue

```bash
# Bootstrap CDK (primera vez)
cd infra
npx cdk bootstrap

# Sintetizar template CloudFormation
npx cdk synth

# Desplegar
npx cdk deploy
```

## Testing

```bash
# Unit tests + Property-based tests
npx vitest --run

# CDK snapshot tests
cd infra && npx jest --run
```

## Usuarios de Prueba

| Usuario | Contraseña | Teléfono |
|---------|-----------|----------|
| carlos.rodriguez | Btg2024*Test | +573001234567 |
| maria.lopez | Btg2024*Demo | +573009876543 |
| juan.garcia | Btg2024*Hack | +573005551234 |

## Flujo de Uso

1. **Primer mensaje** → El sistema envía Términos y Condiciones (botones aceptar/rechazar)
2. **Acepta T&C** → Mensaje de bienvenida con servicios disponibles
3. **Solicita servicio** → El sistema pide autenticación vía enlace web
4. **Se autentica** → Sesión activa por 30 minutos
5. **Usa servicios** → Consulta saldos, transfiere, genera extractos en lenguaje natural

## Servicios Disponibles

### Consulta de Saldos
> "¿Cuánto tengo en mi cuenta?" / "Muéstrame mis saldos" / "Cuánta plata tengo"

Retorna saldos de Fondos de Inversión y Cuenta Corriente en COP.

### Transferencias BRE-B
> "Quiero transferir 500 mil a la cuenta 1009876544" / "Pásame plata a otra cuenta"

Solicita confirmación explícita antes de ejecutar. Genera comprobante.

### Extractos Bancarios
> "Necesito mi extracto de noviembre" / "Genera mi estado de cuenta"

Genera PDF y lo envía como documento adjunto directamente en WhatsApp.

## Observabilidad

- **Logs estructurados** — JSON via Lambda Powertools con correlation_id
- **CloudWatch Dashboard** — Invocaciones, errores, latencia p50/p90 por Lambda
- **Alarmas** — Error rate > 10% en ventana de 5 minutos → notificación SNS
- **Retención** — 7 días en CloudWatch Logs

## Seguridad

- Cifrado en reposo con AWS managed keys (DynamoDB, S3)
- Cifrado en tránsito con TLS 1.2+
- IAM roles con mínimo privilegio por Lambda
- Data sensible enmascarada en logs (últimos 4 dígitos)
- Secretos en AWS Secrets Manager
- Bedrock Guardrails para control de contenido

## Enfoque MVP

Este es un MVP para demo de hackathon:
- Datos bancarios mock (hardcodeados en las Lambdas)
- Autenticación mock con 3 usuarios de prueba
- Sin integración con core bancario real
- Sin VPC (acceso directo a servicios AWS)

### Path a Producción

| Extensión | Descripción |
|-----------|-------------|
| Core bancario real | VPC + conectividad privada al core |
| Autenticación real | OAuth2/OIDC con proveedor de identidad corporativo |
| Servicios adicionales | Pagos, apertura de productos, TRM |
| Auditoría | Kinesis Firehose → S3 (retención 7 años) |
| KMS custom | CMK con rotación anual |
| Observabilidad avanzada | X-Ray tracing, métricas custom |

## Licencia

Proyecto interno BTG Pactual Colombia — Hackathon 2024.
