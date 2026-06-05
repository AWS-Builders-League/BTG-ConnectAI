---
inclusion: fileMatch
fileMatchPattern: "cloudformation/**/*.yaml"
---

# Guía CloudFormation — BTG ConnectAI

## Principios

- CloudFormation puro (YAML). No CDK, no SAM, no Terraform.
- Templates anidados (`AWS::CloudFormation::Stack`) orquestados desde `cloudformation/stacks/sandbox/connectai.yaml`
- Empaquetado Lambda: ZIP a S3, referenciado con `Code: {S3Bucket, S3Key}`
- Región: us-east-1

## Estructura de Templates

```
cloudformation/templates/connectai/
├── data.yaml            # DynamoDB tables
├── storage.yaml         # S3 buckets
├── secrets.yaml         # Secrets Manager + SNS alarms
├── queues.yaml          # SQS (FIFO inbound + Standard notifications)
├── vpc-access.yaml      # Security Group + VPC Endpoints
├── lambdas-ingestion.yaml   # Webhook_Receiver + Message_Processor
├── lambda-auth.yaml     # Auth_Service (Function URL)
├── lambdas-actions.yaml # balance-query, transfer-breb-*, statement-generator
├── lambda-otp.yaml      # OTP_Service
├── lambdas-notify.yaml  # Email_Service + SMS_Service
├── lambda-ai-agent.yaml # Strands Agent + Guardrails
├── state-machine.yaml   # TransferBrebStateMachine
├── api-gateway.yaml     # HTTP API + routes
└── observability.yaml   # Dashboard + Alarms
```

## Convenciones de Naming

- Logical IDs: PascalCase (`InboundMessagesQueue`, `WebhookReceiverFunction`)
- Physical names: kebab-case con prefijo proyecto (`btgconnectai-inbound-messages-queue.fifo`)
- Export names: `${ProjectName}-${Environment}-<ResourceName>` para cross-stack

## Parámetros Estándar (Stack Raíz)

```yaml
Parameters:
  ProjectName:
    Type: String
    Default: BTGConnectAI
  Environment:
    Type: String
    Default: sandbox
    AllowedValues: [sandbox, staging, production]
  TemplatesBucket:
    Type: String
  LambdaArtifactsBucket:
    Type: String
  LambdaCodeVersion:
    Type: String
    Description: Git SHA usado como S3 key prefix
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

Config:
```yaml
VpcConfig:
  SecurityGroupIds:
    - !ImportValue ${ProjectName}-${Environment}-BankingLambdaSGId
  SubnetIds: !Split [",", !ImportValue ${ProjectName}-${Environment}-PrivateSubnetIds]
```

## VPC Endpoints Requeridos

- Gateway Endpoint S3 (gratis) — para statement-generator PutObject
- Gateway Endpoint DynamoDB (gratis) — para futuro core bancario

## Validación

```bash
cfn-lint cloudformation/**/*.yaml
checkov -d cloudformation/
```
