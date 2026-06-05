# Skill: Desplegar al Sandbox

## Descripción
Guía y ejecuta el despliegue del stack de CloudFormation al ambiente sandbox en us-east-1.

## Pre-requisitos

- AWS CLI configurado con credenciales válidas para la cuenta sandbox
- Templates validados con cfn-lint (ejecutar skill `cfn-validate` primero)
- Lambdas empaquetadas como ZIP en S3

## Pasos

1. Validar templates: `cfn-lint cloudformation/**/*.yaml`
2. Verificar credenciales AWS: `aws sts get-caller-identity`
3. Desplegar:
```bash
aws cloudformation deploy \
  --stack-name BTGConnectAI-sandbox \
  --template-file cloudformation/stacks/sandbox/connectai.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides \
    ProjectName=BTGConnectAI \
    Environment=sandbox
```
4. Verificar estado: `aws cloudformation describe-stacks --stack-name BTGConnectAI-sandbox --query "Stacks[0].StackStatus"`
5. Si falla, revisar eventos: `aws cloudformation describe-stack-events --stack-name BTGConnectAI-sandbox --query "StackEvents[?ResourceStatus=='CREATE_FAILED']"`
