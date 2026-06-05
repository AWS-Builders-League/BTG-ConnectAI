# Buenas Prácticas Python — General & FastAPI

## Principios SOLID

### S — Single Responsibility Principle
- Cada módulo, clase o función debe tener UNA sola razón para cambiar.
- Un handler de Lambda no debe contener lógica de negocio, validación Y acceso a datos — separarlos en capas.

```python
# ❌ Malo — handler hace todo
def handler(event, context):
    data = json.loads(event["body"])
    if not data.get("phone"):
        return {"statusCode": 400}
    result = boto3.client("dynamodb").get_item(...)
    formatted = f"${result['balance']:,.2f}"
    return {"statusCode": 200, "body": formatted}

# ✅ Bueno — separar responsabilidades
def handler(event, context):
    payload = parse_request(event)
    result = balance_service.get_balance(payload.phone)
    return build_response(200, result)
```

### O — Open/Closed Principle
- Clases abiertas para extensión, cerradas para modificación.
- Usar protocolos (Protocol) o clases abstractas para permitir extensibilidad.

```python
from typing import Protocol

class NotificationSender(Protocol):
    def send(self, to: str, message: str) -> bool: ...

class SmsSender:
    def send(self, to: str, message: str) -> bool:
        # Pinpoint implementation
        ...

class EmailSender:
    def send(self, to: str, message: str) -> bool:
        # SES implementation
        ...
```

### L — Liskov Substitution Principle
- Subtipos deben ser sustituibles por su tipo base sin romper el programa.
- Si heredas, no cambies el contrato (precondiciones, postcondiciones, invariantes).

### I — Interface Segregation Principle
- Preferir interfaces pequeñas y específicas sobre interfaces grandes.
- En Python: usar `Protocol` con pocos métodos en lugar de ABCs con muchos.

```python
# ❌ Interfaz gorda
class BankingService(Protocol):
    def get_balance(self, phone: str) -> dict: ...
    def transfer(self, source: str, dest: str, amount: int) -> dict: ...
    def generate_statement(self, phone: str, date: str) -> bytes: ...

# ✅ Interfaces segregadas
class BalanceReader(Protocol):
    def get_balance(self, phone: str) -> dict: ...

class TransferExecutor(Protocol):
    def execute(self, source: str, dest: str, amount: int) -> dict: ...
```

### D — Dependency Inversion Principle
- Los módulos de alto nivel no deben depender de módulos de bajo nivel. Ambos deben depender de abstracciones.
- Inyectar dependencias en lugar de instanciarlas internamente.

```python
# ❌ Acoplamiento directo
class TransferService:
    def __init__(self):
        self.db = boto3.resource("dynamodb").Table("otp-store")

# ✅ Inversión de dependencia
class TransferService:
    def __init__(self, otp_repository: OTPRepository):
        self.otp_repository = otp_repository
```

---

## Estructura de Carpetas (FastAPI / Lambdas)

### Para servicios FastAPI

```
src/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory
│   ├── config.py               # Settings (pydantic-settings)
│   ├── dependencies.py         # Dependency injection
│   ├── api/
│   │   ├── __init__.py
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── router.py       # Agrupa todos los routers v1
│   │   │   ├── endpoints/
│   │   │   │   ├── balance.py
│   │   │   │   ├── transfers.py
│   │   │   │   └── statements.py
│   │   │   └── schemas/        # Request/Response models (Pydantic)
│   │   │       ├── balance.py
│   │   │       ├── transfers.py
│   │   │       └── common.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── security.py         # Auth, tokens, permisos
│   │   ├── exceptions.py       # Excepciones de dominio
│   │   └── logging.py          # Configuración de logging
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── models/             # Entidades de dominio (no Pydantic de API)
│   │   │   ├── client.py
│   │   │   └── transaction.py
│   │   └── services/           # Lógica de negocio
│   │       ├── balance_service.py
│   │       └── transfer_service.py
│   ├── infrastructure/
│   │   ├── __init__.py
│   │   ├── repositories/       # Acceso a datos
│   │   │   ├── dynamo_client_repo.py
│   │   │   └── s3_statement_repo.py
│   │   └── external/           # Integraciones externas
│   │       ├── twilio_client.py
│   │       └── pinpoint_client.py
│   └── shared/
│       ├── masking.py
│       ├── formatting.py
│       └── constants.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
├── pyproject.toml
└── Dockerfile
```

### Para AWS Lambda (este proyecto)

```
src/
├── lambdas/
│   ├── <nombre>/
│   │   ├── __init__.py
│   │   ├── handler.py          # Entry point — solo orquestación
│   │   ├── service.py          # Lógica de negocio
│   │   ├── repository.py       # Acceso a datos (DynamoDB, S3)
│   │   └── models.py           # TypedDicts / dataclasses locales
│   └── ...
├── shared/                     # Lambda Layer
│   ├── logger.py
│   ├── masking.py
│   ├── formatting.py
│   ├── constants.py
│   ├── types.py
│   └── errors.py
└── tests/
```

---

## FastAPI — Buenas Prácticas

### App Factory

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: inicializar recursos
    yield
    # Shutdown: liberar recursos

def create_app() -> FastAPI:
    app = FastAPI(
        title="BTG ConnectAI",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    return app
```

### Schemas (Pydantic v2)

```python
from pydantic import BaseModel, Field, field_validator
from datetime import datetime

class TransferRequest(BaseModel):
    """Solicitud de transferencia BRE-B."""
    source_account: str = Field(..., min_length=10, max_length=20)
    destination_account: str = Field(..., min_length=10, max_length=20)
    amount: int = Field(..., gt=0, le=50_000_000)
    concept: str = Field(default="", max_length=100)

    @field_validator("source_account", "destination_account")
    @classmethod
    def validate_account_format(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("La cuenta debe contener solo dígitos")
        return v

class TransferResponse(BaseModel):
    transaction_id: str
    status: str
    executed_at: datetime
    amount_formatted: str
```

### Dependency Injection

```python
from functools import lru_cache
from fastapi import Depends

@lru_cache
def get_settings() -> Settings:
    return Settings()

def get_dynamo_table(settings: Settings = Depends(get_settings)):
    import boto3
    resource = boto3.resource("dynamodb", region_name=settings.aws_region)
    return resource.Table(settings.table_name)

def get_balance_service(table = Depends(get_dynamo_table)) -> BalanceService:
    return BalanceService(repository=DynamoBalanceRepo(table))
```

### Exception Handlers

```python
from fastapi import Request
from fastapi.responses import JSONResponse

class DomainError(Exception):
    def __init__(self, message: str, code: str, status_code: int = 400):
        self.message = message
        self.code = code
        self.status_code = status_code

async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )
```

### Middleware y CORS

```python
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
```

---

## Buenas Prácticas de Logging

### Reglas Fundamentales

1. **Structured logging** — Siempre JSON, nunca texto plano en producción.
2. **Niveles correctos**:
   - `DEBUG`: Detalles internos (solo en desarrollo)
   - `INFO`: Eventos de negocio relevantes (inicio/fin de operación)
   - `WARNING`: Situaciones recuperables pero inesperadas
   - `ERROR`: Fallos que requieren atención
   - `CRITICAL`: Sistema inutilizable
3. **Nunca loguear datos sensibles** — Usar masking.
4. **Incluir contexto** — correlation_id, request_id, user_id (enmascarado).
5. **No loguear dentro de loops** — Loguear resumen al final.

### Ejemplo con Lambda Powertools

```python
from aws_lambda_powertools import Logger

logger = Logger(service="transfer-service")

@logger.inject_lambda_context(correlation_id_path="body.correlationId")
def handler(event, context):
    phone = event["phoneNumber"]
    logger.info(
        "Iniciando transferencia",
        extra={
            "phone_masked": mask_phone(phone),
            "amount": event["amount"],
            "destination_masked": mask_account(event["destination"]),
        },
    )
```

### Ejemplo con structlog (FastAPI)

```python
import structlog

logger = structlog.get_logger()

async def transfer_endpoint(request: TransferRequest):
    log = logger.bind(
        correlation_id=request.state.correlation_id,
        phone_masked=mask_phone(request.phone),
    )
    log.info("transfer.initiated", amount=request.amount)
    try:
        result = await service.execute(request)
        log.info("transfer.completed", transaction_id=result.id)
        return result
    except InsufficientFundsError:
        log.warning("transfer.insufficient_funds")
        raise
```

### Anti-patrones de Logging

```python
# ❌ Loguear datos sensibles
logger.info(f"Usuario {phone_number} transfirió a {account_number}")

# ✅ Enmascarar
logger.info("Transferencia exitosa", extra={
    "phone": mask_phone(phone_number),  # ***4567
    "destination": mask_account(account_number),  # ****6544
})

# ❌ Loguear excepciones sin contexto
except Exception as e:
    logger.error(str(e))

# ✅ Loguear con stack trace y contexto
except Exception:
    logger.exception("Error al ejecutar transferencia", extra={"correlation_id": cid})

# ❌ Loguear dentro de un loop
for item in items:
    logger.info(f"Processing {item.id}")

# ✅ Loguear resumen
logger.info("Batch procesado", extra={"total": len(items), "failures": failed_count})
```

---

## Código Limpio — Reglas Generales

### Funciones

- Máximo 20-30 líneas (si es más grande, extraer funciones auxiliares)
- Máximo 3-4 parámetros (si necesitas más, usar dataclass/TypedDict)
- Un solo nivel de abstracción por función
- Nombres descriptivos: `validate_transfer_amount` no `check`

### Naming

```python
# ❌ Nombres vagos
def process(d):
    r = get(d["k"])
    return r

# ✅ Nombres descriptivos
def process_transfer_request(request: TransferRequest) -> TransferResult:
    balance = get_client_balance(request.source_account)
    return execute_transfer(request, balance)
```

### Error Handling

```python
# ❌ Catch genérico que oculta errores
try:
    result = transfer()
except Exception:
    return None

# ✅ Excepciones específicas con contexto
try:
    result = execute_transfer(request)
except InsufficientFundsError as e:
    logger.warning("Fondos insuficientes", extra={"available": e.available, "requested": e.requested})
    raise
except InvalidDestinationError:
    raise DomainError("Cuenta destino no existe", code="INVALID_DESTINATION")
```

### Type Hints

```python
from typing import TypedDict

class TransferContext(TypedDict):
    phone_number: str
    source_account: str
    destination_account: str
    amount: int
    concept: str

def validate_transfer(context: TransferContext) -> bool:
    """Valida que la transferencia sea posible."""
    ...
```

### Guard Clauses (Early Return)

```python
# ❌ Nesting profundo
def process(request):
    if request.is_valid():
        if request.user.is_authenticated():
            if request.user.has_permission():
                return do_work(request)
            else:
                raise PermissionError()
        else:
            raise AuthError()
    else:
        raise ValidationError()

# ✅ Guard clauses
def process(request):
    if not request.is_valid():
        raise ValidationError()
    if not request.user.is_authenticated():
        raise AuthError()
    if not request.user.has_permission():
        raise PermissionError()
    return do_work(request)
```

### Inmutabilidad

```python
from dataclasses import dataclass, field
from typing import FrozenSet

# Preferir dataclasses inmutables para entidades de dominio
@dataclass(frozen=True)
class TransferReceipt:
    transaction_id: str
    amount: int
    source: str
    destination: str
    executed_at: str
    status: str = "COMPLETED"
```
