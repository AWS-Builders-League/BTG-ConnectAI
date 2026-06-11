---
inclusion: fileMatch
fileMatchPattern: "cloudformation/**/*.yaml"
---

# Guía CloudFormation — BTG ConnectAI

## Reparto de IaC entre repos (LEER PRIMERO)

La IaC está **repartida en dos repositorios** con un límite claro de responsabilidad:

- **Repo `infra` (IaC centralizada)** crea TODOS los recursos compartidos: **API Gateway** (HTTP API), **todas las colas SQS** (FIFO inbound + email/sms notifications + DLQs), **todos los buckets S3** (Statement, Audio_Temp, Login_Page, artefactos de Lambda), **todas las tablas DynamoDB** (Consent_Store, Auth_Session, OTP_Store), **Bedrock** (Agent Core + Guardrails), **red** (Security Group `BankingLambdaSG` + VPC Endpoints), **Secrets Manager**, **SNS de alarmas** y **observabilidad compartida** (Dashboard/Alarms).
- **Repo `BTG-ConnectAI` (este repo)** define SOLO **cómputo y wiring**: cada Lambda (con su IAM role), la state machine `TransferBrebStateMachine` (con su rol), los Event Source Mappings (SQS de `infra` → Lambda de la app), el wiring de API Gateway (Integration + Route + `Lambda::Permission` con el `HttpApiId` importado), el `Lambda::Permission` para que el Bedrock Agent invoque las Action Group Lambdas, el código de aplicación Python 3.13 y el contenido de la Login_Page estática.

> **NO crear en este repo** templates de recursos compartidos (DynamoDB, S3, SQS, API Gateway, Bedrock, Guardrails, VPC Endpoints, Security Groups, Secrets, SNS, observabilidad). Esos los crea `infra`. Aquí se **referencian** vía el contrato cross-stack.

## Principios

- CloudFormation puro (YAML). No CDK, no SAM, no Terraform.
- Templates anidados (`AWS::CloudFormation::Stack`) orquestados desde `cloudformation/stacks/sandbox/connectai-app.yaml`
- Empaquetado Lambda: ZIP al **bucket de artefactos creado por `infra`** (nombre resuelto del contrato), referenciado con `Code: {S3Bucket, S3Key}`
- Región: us-east-1
- **Orden de despliegue**: `infra` primero (publica el contrato), luego `BTG-ConnectAI`

## Estructura de Templates (solo cómputo/wiring)

```
cloudformation/
├── templates/
│   └── connectai/
│       ├── iam.yaml                  # IAM roles de todas las Lambdas + rol del state machine
│       ├── lambdas-ingestion.yaml    # Webhook_Receiver + Message_Processor + API GW wiring + ESM inbound
│       ├── lambdas-actions.yaml      # balance-query, transfer-breb-validate/execute/initiator, statement-generator + Bedrock Lambda::Permission
│       ├── lambdas-notify.yaml       # OTP_Service + Email_Service + SMS_Service (+ ESMs email/sms) + message-handler-notify
│       ├── lambda-ai-agent.yaml      # Strands Agent Lambda
│       └── state-machine.yaml        # AWS::StepFunctions::StateMachine
├── stacks/
│   └── sandbox/
│       └── connectai-app.yaml        # Stack raíz: parámetros del contrato + nested stacks de cómputo
└── state-machines/
    └── transfer-breb.asl.json        # Definición Amazon States Language
```

## Contrato Cross-Stack (cómo referenciar recursos de `infra`)

Las Lambdas y la state machine consumen los recursos de `infra` mediante un **contrato explícito** publicado por `infra` y consumido aquí. Dos mecanismos complementarios:

1. **SSM Parameter Store** (recomendado entre repos — desacople): `AWS::SSM::Parameter::Value<String>` en los `Parameters` del stack raíz, o `{{resolve:ssm:...}}`.
2. **CloudFormation `Fn::ImportValue`** (acoplamiento fuerte, misma cuenta/region; protege contra borrado de recursos exportados).

### Convención de nombres del contrato

Alineada a `${ProjectName}-${Environment}-<Recurso>` (Export) y a un namespace SSM `/btgconnectai/sandbox/...`:

| Recurso (en `infra`) | Export Name (CFN) | SSM Parameter |
| --- | --- | --- |
| API Gateway HTTP API ID | `BTGConnectAI-sandbox-HttpApiId` | `/btgconnectai/sandbox/api/http-api-id` |
| API Gateway endpoint | `BTGConnectAI-sandbox-HttpApiEndpoint` | `/btgconnectai/sandbox/api/endpoint` |
| Inbound queue ARN / URL | `BTGConnectAI-sandbox-InboundQueueArn` / `...-InboundQueueUrl` | `/btgconnectai/sandbox/sqs/inbound-arn` · `.../inbound-url` |
| Email queue ARN / URL | `BTGConnectAI-sandbox-EmailQueueArn` / `...-EmailQueueUrl` | `/btgconnectai/sandbox/sqs/email-arn` · `.../email-url` |
| SMS queue ARN / URL | `BTGConnectAI-sandbox-SmsQueueArn` / `...-SmsQueueUrl` | `/btgconnectai/sandbox/sqs/sms-arn` · `.../sms-url` |
| Consent_Store name / ARN | `BTGConnectAI-sandbox-ConsentTableName` / `...-ConsentTableArn` | `/btgconnectai/sandbox/ddb/consent-name` · `.../consent-arn` |
| Auth_Session name / ARN | `BTGConnectAI-sandbox-AuthTableName` / `...-AuthTableArn` | `/btgconnectai/sandbox/ddb/auth-name` · `.../auth-arn` |
| OTP_Store name / ARN | `BTGConnectAI-sandbox-OtpTableName` / `...-OtpTableArn` | `/btgconnectai/sandbox/ddb/otp-name` · `.../otp-arn` |
| Statement_Bucket name / ARN | `BTGConnectAI-sandbox-StatementBucketName` / `...-StatementBucketArn` | `/btgconnectai/sandbox/s3/statement-name` · `.../statement-arn` |
| Audio_Temp bucket name / ARN | `BTGConnectAI-sandbox-AudioTempBucketName` / `...-AudioTempBucketArn` | `/btgconnectai/sandbox/s3/audio-temp-name` · `.../audio-temp-arn` |
| Lambda artifacts bucket | `BTGConnectAI-sandbox-LambdaArtifactsBucket` | `/btgconnectai/sandbox/s3/artifacts-bucket` |
| Bedrock Agent ARN / ID | `BTGConnectAI-sandbox-BedrockAgentArn` / `...-BedrockAgentId` | `/btgconnectai/sandbox/bedrock/agent-arn` · `.../agent-id` |
| Bedrock Guardrail ID / version | `BTGConnectAI-sandbox-GuardrailId` / `...-GuardrailVersion` | `/btgconnectai/sandbox/bedrock/guardrail-id` · `.../guardrail-version` |
| Twilio secret ARN | `BTGConnectAI-sandbox-TwilioSecretArn` | `/btgconnectai/sandbox/secrets/twilio-arn` |
| SNS alarms topic ARN | `BTGConnectAI-sandbox-AlarmsTopicArn` | `/btgconnectai/sandbox/sns/alarms-arn` |
| Banking Lambda SG ID | `BTGConnectAI-sandbox-BankingLambdaSGId` | `/btgconnectai/sandbox/vpc/banking-sg-id` |
| Private Subnet IDs (CSV) | `BTGConnectAI-sandbox-PrivateSubnetIds` | `/btgconnectai/sandbox/vpc/private-subnet-ids` |

> **Regla**: ningún ARN/URL/ID de recurso compartido se hardcodea en los templates de la app. Siempre entra como parámetro del contrato (`!Ref`) o `Fn::ImportValue`.

## Convenciones de Naming

- Logical IDs: PascalCase (`WebhookReceiverFunction`, `TransferBrebStateMachine`)
- Physical names: kebab-case con prefijo proyecto (`btgconnectai-webhook-receiver`)
- Export names: `${ProjectName}-${Environment}-<ResourceName>` para cross-stack

## Parámetros Estándar (Stack Raíz `connectai-app.yaml`)

```yaml
Parameters:
  ProjectName:
    Type: String
    Default: BTGConnectAI
  Environment:
    Type: String
    Default: sandbox
    AllowedValues: [sandbox, staging, production]
  LambdaCodeVersion:
    Type: String
    Description: Git SHA usado como S3 key prefix
  # --- Contrato cross-stack (resuelto desde infra vía SSM) ---
  InboundQueueArn:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /btgconnectai/sandbox/sqs/inbound-arn
  BankingLambdaSGId:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /btgconnectai/sandbox/vpc/banking-sg-id
  PrivateSubnetIds:
    Type: AWS::SSM::Parameter::Value<String>
    Default: /btgconnectai/sandbox/vpc/private-subnet-ids
  # ... (resto de parámetros del contrato según la tabla de arriba)
```

## Tags Obligatorios

```yaml
Tags:
  - Key: Project
    Value: !Ref ProjectName
  - Key: Environment
    Value: !Ref Environment
  - Key: ManagedBy
    Value: CloudFormation
```

## Lambdas en VPC (Dominio Bancario)

Solo estas Lambdas van en VPC privada:
- `balance-query`
- `transfer-breb-validate`
- `transfer-breb-execute`
- `statement-generator`

El Security Group y las subnets son propiedad de `infra`; aquí se referencian del contrato:
```yaml
VpcConfig:
  SecurityGroupIds:
    - !Ref BankingLambdaSGId            # del contrato (SSM / Fn::ImportValue)
  SubnetIds: !Split [",", !Ref PrivateSubnetIds]
```

> El resto de Lambdas (Webhook_Receiver, Message_Processor, Auth_Service, OTP_Service, Email_Service, SMS_Service, Strands_Agent, transfer-breb-initiator, message-handler-notify) van **fuera de VPC**.

## Wiring que cruza el límite entre repos (se define AQUÍ)

| Wiring | Recurso de `infra` | Definición en este repo |
| --- | --- | --- |
| API Gateway → Webhook_Receiver | `HttpApiId` | `AWS::ApiGatewayV2::Integration` (`AWS_PROXY`) + `Route` (`POST /webhook/twilio`) + `AWS::Lambda::Permission` |
| SQS inbound → Message_Processor | `InboundQueueArn` | `AWS::Lambda::EventSourceMapping` (`BatchSize: 1`, `ReportBatchItemFailures`) |
| SQS email/sms → Email/SMS_Service | `EmailQueueArn` / `SmsQueueArn` | `AWS::Lambda::EventSourceMapping` (`BatchSize: 10`) |
| Bedrock Agent → Action Group Lambdas | `BedrockAgentArn` | `AWS::Lambda::Permission` (`bedrock.amazonaws.com`, `SourceArn = BedrockAgentArn`) |
| State machine → SQS notificaciones | `EmailQueueArn` / `SmsQueueArn` | `DefinitionSubstitutions` + `sqs:SendMessage` en el rol |

> **Regla general**: cada trigger/integration/permiso se define en el repo dueño del cómputo (este repo), usando como entrada los identificadores del recurso compartido (que vienen de `infra`).

## Validación

```bash
# Solo templates de la app (recursos compartidos los valida el pipeline de infra)
cfn-lint cloudformation/templates/connectai/*.yaml cloudformation/stacks/sandbox/*.yaml
checkov -d cloudformation/

# Verificar que NO hay ARNs/URLs/IDs de recursos compartidos hardcodeados
# (todos deben venir de parámetros del contrato o Fn::ImportValue)
```
