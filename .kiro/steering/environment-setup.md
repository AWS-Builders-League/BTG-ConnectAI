---
inclusion: manual
---

# Configuración del Ambiente de Desarrollo

## Pre-requisitos

| Herramienta | Versión Mínima | Propósito |
|-------------|----------------|-----------|
| Python | 3.13 | Runtime de Lambdas |
| pip / uv | última | Gestión de dependencias |
| AWS CLI v2 | 2.15+ | Despliegue y gestión de recursos |
| cfn-lint | 0.87+ | Validación de templates CloudFormation |
| checkov | 3.2+ | Security scanning de IaC |
| ruff | 0.5+ | Linter + formatter Python |
| pytest | 8.0+ | Testing |
| Git | 2.40+ | Control de versiones |

## Setup Inicial

```bash
# 1. Clonar el repositorio
git clone <repo-url>
cd BTG-ConnectAI

# 2. Crear virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate

# 3. Instalar dependencias de desarrollo
pip install -r src/requirements-dev.txt

# 4. Instalar dependencias de runtime (para desarrollo local)
pip install -r src/requirements.txt

# 5. Configurar AWS CLI
aws configure --profile btg-sandbox
# Region: us-east-1
# Output: json

# 6. Copiar archivo de variables de entorno
copy .env.example .env
# Editar .env con los valores reales
```

## Configuración AWS

### Perfil de AWS CLI

```ini
# ~/.aws/credentials
[btg-sandbox]
aws_access_key_id = AKIA...
aws_secret_access_key = ...

# ~/.aws/config
[profile btg-sandbox]
region = us-east-1
output = json
```

### IAM — Permisos Necesarios para Desarrollo

El desarrollador necesita acceso a:
- CloudFormation (deploy/describe/delete)
- Lambda (invoke, update-function-code)
- DynamoDB (CRUD en tablas del proyecto)
- S3 (get/put en buckets del proyecto)
- SQS (send/receive en colas del proyecto)
- Step Functions (start/describe/send-task)
- Secrets Manager (get-secret-value)
- CloudWatch Logs (read)

## Estructura del Virtual Environment

```
.venv/
├── Lib/site-packages/
│   ├── twilio/
│   ├── aws_lambda_powertools/
│   ├── strands/
│   ├── fpdf2/
│   ├── pytest/
│   ├── hypothesis/
│   ├── moto/
│   ├── ruff/
│   └── ...
└── Scripts/ (o bin/)
```

## Ejecutar Localmente

```bash
# Validar templates
cfn-lint cloudformation/**/*.yaml

# Lint Python
ruff check src/
ruff format --check src/

# Tests
pytest src/tests/ -v

# Tests con coverage
pytest src/tests/ --cov=src/lambdas --cov=src/shared --cov-report=html
```

## CI/CD (GitHub Actions)

El workflow `.github/workflows/cfn-deploy.yml` hace:
1. Checkout
2. Setup Python 3.13
3. Install dependencies
4. Run cfn-lint
5. Run pytest
6. Build Lambda Layer (zip shared/ + deps)
7. Zip cada Lambda individualmente
8. Upload artefactos a S3
9. CloudFormation deploy (OIDC auth)
