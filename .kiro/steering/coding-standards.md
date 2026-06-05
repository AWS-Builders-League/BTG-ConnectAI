# Estándares de Código — BTG ConnectAI

## Python

- **Versión**: 3.13 estricto
- **Formatter**: ruff (format) + ruff (lint)
- **Type hints**: Obligatorios en funciones públicas. Usar `TypedDict` para estructuras de datos compartidas (definidas en `src/shared/types.py`)
- **Docstrings**: Google-style en español para funciones públicas
- **Imports**: Ordenados con ruff (isort compatible)
- **Naming**: snake_case para funciones y variables, PascalCase para clases, UPPER_CASE para constantes

## Lambda Handlers

- Decorar con `@logger.inject_lambda_context(correlation_id_path="body.correlationId")` de Lambda Powertools
- Usar `correlation_id` propagado desde Webhook_Receiver (nunca regenerar downstream)
- Retornar siempre un dict con `statusCode` para Lambdas detrás de API Gateway
- Imports lazy de boto3 clients (fuera del handler, nivel de módulo) para reutilización de conexiones

## CloudFormation

- **YAML** exclusivamente (no JSON para templates)
- Usar `!Ref`, `!Sub`, `!GetAtt`, `!ImportValue` (no `Fn::` largo salvo en casos ambiguos)
- Naming: PascalCase para Logical IDs (`WebhookReceiverFunction`)
- Tags obligatorios: `Project: BTGConnectAI`, `Environment: !Ref Environment`
- Outputs con `Export.Name` para composición entre nested stacks
- Descripciones en español para cada recurso

## Tests

- **Framework**: pytest
- **Property-based**: hypothesis
- **Mocking AWS**: moto
- **Estructura**:
  - `src/tests/unit/` — tests unitarios por Lambda
  - `src/tests/property/` — tests de propiedades (hypothesis)
  - `src/tests/integration/` — tests de integración (con moto)
- **Naming**: `test_<module>_<scenario>_<expected_outcome>.py`

## Seguridad

- Nunca loguear datos completos de cuentas, teléfonos o documentos — usar `src/shared/masking.py`
- Credenciales SIEMPRE desde Secrets Manager, nunca hardcodeadas (excepto usuarios mock de prueba)
- IAM de mínimo privilegio: cada Lambda solo tiene los permisos que necesita
- Validar firma Twilio antes de procesar cualquier webhook

## Respuestas al Cliente

- Español colombiano natural y amigable
- Montos formateados: $10.000 COP (usar `src/shared/formatting.py`)
- Datos sensibles enmascarados en respuestas y logs
- Disclaimer en consultas financieras: "Información referencial"
