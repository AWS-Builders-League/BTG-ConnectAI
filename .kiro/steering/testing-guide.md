---
inclusion: fileMatch
fileMatchPattern: "src/tests/**/*.py"
---

# Guía de Testing — BTG ConnectAI

## Estructura

```
src/tests/
├── unit/               # Tests unitarios (mocking con moto/unittest.mock)
├── property/           # Tests de propiedades (hypothesis)
├── integration/        # Tests de integración (moto full stack)
└── conftest.py         # Fixtures compartidos
```

## Ejecutar Tests

```bash
# Todos los tests
pytest src/tests/ -v

# Solo unitarios
pytest src/tests/unit/ -v

# Solo property-based
pytest src/tests/property/ -v

# Con coverage
pytest src/tests/ --cov=src/lambdas --cov=src/shared --cov-report=html
```

## Convenciones

- Archivo: `test_<module>_<scenario>.py`
- Función: `test_<función>_<escenario>_<resultado_esperado>()`
- Fixtures en `conftest.py` por directorio cuando son reutilizables
- Mocking de AWS con `moto` (decorador `@mock_aws`)

## Property-Based Tests (hypothesis)

Propiedades definidas en el plan de implementación:

| # | Propiedad | Módulo |
|---|-----------|--------|
| 1 | Message Splitting Round-Trip | messaging |
| 3 | Session ID Determinism | auth |
| 4 | Data Masking Correctness | masking |
| 5 | Existing Consent Skips T&C | consent |
| 6 | No Session → Login | auth |
| 7 | Active Session → Proceed | auth |
| 8 | Invalid Credentials Rejection | auth_service |
| 9 | Balance Query Correctness | balance_query |
| 10 | Unknown Client Error | balance_query |
| 11 | Valid Transfer Produces Receipt | transfer_breb |
| 12 | Insufficient Funds Rejection | transfer_breb |
| 13 | Future Date Rejection | statement_generator |
| 14 | Valid Statement Returns S3 Reference | statement_generator |
| 15 | COP Currency Formatting | formatting |
| 16 | Unsupported Format Rejection | message_processor |
| 17 | OTP Expiry | otp_callback |
| 18 | Brute Force Block | otp_callback |
| 19 | Idempotent StartExecution | transfer_breb |

## Plantilla Property Test

```python
from hypothesis import given, strategies as st, settings

@settings(max_examples=200)
@given(amount=st.integers(min_value=0, max_value=999_999_999_999))
def test_cop_formatting_always_matches_pattern(amount: int):
    """Property 15: Para cualquier número no-negativo, format_cop produce $X.XXX.XXX,YY."""
    result = format_cop(amount)
    assert result.startswith("$")
    # Validar patrón regex
```

## Plantilla Unit Test con moto

```python
import boto3
import pytest
from moto import mock_aws

@mock_aws
def test_store_consent_creates_item():
    """Almacenar consentimiento crea registro en DynamoDB."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="consent-store",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # ... test logic
```

## Variables de Entorno para Tests

Configurar en `conftest.py` o `pyproject.toml`:
```python
import os
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["CONSENT_TABLE_NAME"] = "consent-store"
os.environ["AUTH_SESSION_TABLE_NAME"] = "auth-session"
os.environ["OTP_TABLE_NAME"] = "otp-store"
os.environ["POWERTOOLS_SERVICE_NAME"] = "test"
```
