# Plan de Implementación: BTG ConnectAI (MVP)

## Visión General

Este plan implementa el MVP de BTG ConnectAI sobre AWS, apoyándose en **Amazon Bedrock Agents** como núcleo agentic y reduciendo la superficie de implementación a ~4 funciones Lambda y 2 tablas DynamoDB. El plan está orientado a un timeline de **4 a 6 semanas** para llegar a producción, contra los 4-6 meses del diseño enterprise inicial.

La implementación sigue un orden por capas dependientes: (1) infraestructura base y seguridad, (2) Action Groups (las tools), (3) Bedrock Agent configurado contra las Action Groups, (4) WhatsApp_Gateway que conecta todo, (5) audit pipeline y observabilidad, (6) testing y validación pre go-live.

Las tareas marcadas con `*` son opcionales y pueden saltarse para acelerar el go-live inicial. Los componentes diferidos a Fase 2 (Fast Path, Fallback dedicado, SQS FIFO, provisioned concurrency, etc.) están documentados en `requirements.md` con sus triggers de activación.

## Tareas

- [ ] 1. Configurar la estructura del proyecto y la infraestructura base
  - [ ] 1.1 Crear la estructura de directorios y la configuración del monorepo
    - Crear directorios por Lambda: `lambdas/whatsapp-gateway/` (TypeScript), `lambdas/financial-query/`, `lambdas/submit-operation/`, `lambdas/generate-secure-link/` (Python)
    - Configurar `package.json` para la Lambda TypeScript y `pyproject.toml` para las Lambdas Python
    - Configurar `infrastructure/` con AWS CDK (TypeScript) — un solo stack para MVP
    - Configurar un paquete compartido `lambdas/_shared/` para tipos comunes (`InboundMessage`, `OutboundMessage`, esquema canónico de audit log)
    - Configurar Hypothesis con settings de proyecto (mín 100 iteraciones, deadline 5000ms)
    - Configurar Lambda Powertools en todas las Lambdas (logger, tracer, metrics)
    - _Requisitos: 13.1, 13.3_

  - [ ] 1.2 Definir las tablas DynamoDB en CDK
    - Tabla `Conversations` con `(PK, SK)` como composite key, `ttl` enabled, on-demand billing, KMS CMK, point-in-time recovery enabled
    - Tabla `SecureLinks` con `(PK, SK)`, `ttl` enabled, on-demand billing, KMS CMK
    - GSI-1 sobre `Conversations` (placeholder, sin atributos inicialmente — se activa cuando aparezca la necesidad)
    - _Requisitos: 7.4, 8.3, 14.1_

  - [ ] 1.3 Configurar KMS, Secrets Manager y baseline de seguridad
    - Crear una CMK rotativa anual en KMS para cifrado de DynamoDB, S3 (audit) y CloudWatch Logs
    - Crear secret en Secrets Manager para la llave HMAC de Secure Links (con rotación managed cada 90 días)
    - Crear secret en Secrets Manager para las credenciales OAuth2 de la Bank_Core_API
    - Definir IAM roles por Lambda con least-privilege (solo los permisos puntuales necesarios)
    - _Requisitos: 14.1, 14.2, 14.3, 14.6_

- [ ] 2. Implementar el Action Group `financial-query`
  - [ ] 2.1 Implementar las operaciones de consulta
    - Implementar `getAccountMovements` con paginación (default 30 días, default limit 10)
    - Implementar `getBalance` (saldo disponible + total)
    - Implementar `getSpendingAnalysis` con categorización, top-5 categorías, comparación contra periodo anterior y flag de cambios > 20%
    - Implementar `getProductStatus` y `listActiveProducts` para los 5 tipos de productos (ahorro, corriente, inversión, crédito, CD)
    - Cliente HTTP hacia la Bank_Core_API con OAuth2 token cacheado en memoria del Lambda (refresh antes de expiry)
    - Enmascaramiento de números de cuenta (últimos 4 dígitos) en logs y traces
    - _Requisitos: 4.1, 4.2, 4.3, 4.5, 5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 14.4_

  - [ ] 2.2 Definir el OpenAPI schema del Action Group
    - Definir `openapi.yaml` con los 5 endpoints y sus schemas de request/response
    - Validar que el schema sea compatible con Bedrock Agent Action Groups (OpenAPI 3.0)
    - Versionar el schema (`v1`) para permitir cambios futuros sin breaking
    - _Requisitos: 4.1, 5.1, 6.1_

  - [ ]* 2.3 Unit tests focales
    - Aplicación del rango de fechas por defecto (últimos 30 días) cuando no se especifica
    - Aplicación del periodo por defecto (mes calendario actual) cuando no se especifica
    - Manejo de error cuando la Bank_Core_API responde 5xx
    - Manejo de paginación cuando hay > 10 transacciones
    - Enmascaramiento de números de cuenta en respuestas formateadas
    - Detección de staleness en estado de producto (> 5 min)
    - _Requisitos: 4.2, 4.4, 4.5, 5.2, 6.5, 14.4_

- [ ] 3. Implementar el Action Group `submit-operation`
  - [ ] 3.1 Implementar la generación de Operational_Request con idempotencia inline
    - Implementar cómputo de `idempotency_key` determinístico (`hash(client_id + operation_params + day_bucket)`)
    - Implementar el flujo: conditional `PutItem` → si duplicado, retornar resultado guardado → si no, validar saldo, llamar a Bank_Core_API, persistir resultado
    - Pasar la `idempotency_key` como header a la Bank_Core_API transaccional
    - Retornar `confirmationNumber` y `estimatedProcessingTime` en respuesta exitosa
    - Manejar fallas de envío con error estructurado para que el Bedrock Agent pueda comunicar la razón
    - _Requisitos: 7.1, 7.3, 7.4, 7.5_

  - [ ] 3.2 Definir el OpenAPI schema del Action Group
    - Definir `openapi.yaml` con el endpoint `submitOperationalRequest` y su schema de request/response
    - Incluir todos los campos requeridos (`sourceAccount`, `destinationAccount`, `amount`, `currency`, `description`, `operationType`)
    - _Requisitos: 7.1_

  - [ ]* 3.3 Unit tests focales
    - Cálculo de `idempotency_key` determinístico (mismos params → misma key)
    - `PutItem` condicional con success y con `ConditionalCheckFailedException` (duplicado)
    - Validación de saldo: aprobar si `requested ≤ available`, rechazar con razón si no
    - Retorno del resultado guardado para envíos duplicados sin llamar a la Bank_Core_API
    - Inclusión de la `idempotency_key` en la respuesta de confirmación
    - _Requisitos: 7.3, 7.4, 7.5_

- [ ] 4. Implementar el Action Group `generate-secure-link`
  - [ ] 4.1 Implementar la generación HMAC-SHA256 y el almacenamiento de metadata
    - Recuperar la llave secreta de Secrets Manager y cachearla en memoria del Lambda
    - Generar `linkId` único (UUID v4) y `expiresAt = now + 10 min`
    - Codificar `operation_context` (tipo, parámetros, `session_id`) en base64url
    - Calcular HMAC-SHA256 sobre `(operation_context || expiresAt)` con la llave secreta
    - Construir la URL final con `ctx`, `exp`, `sig` como query params
    - Persistir en tabla `SecureLinks` con `status='active'` y TTL de 10 min
    - _Requisitos: 8.1, 8.2, 8.3_

  - [ ] 4.2 Implementar la operación de actualización de status (`used`/`expired`)
    - Endpoint que el portal del banco invoca tras validar el link, para marcarlo como `used`
    - Conditional update sobre `status='active'` → `status='used'` para idempotencia
    - Manejo de links expirados (consultados tras `expiresAt`): retornar mensaje al cliente vía Bedrock Agent
    - _Requisitos: 8.4, 8.5_

  - [ ] 4.3 Definir el OpenAPI schema del Action Group
    - Definir `generateSecureLink` con request `{operationType, operationParameters}` y response `{url, expiresAt, linkId}`
    - _Requisitos: 8.1_

  - [ ]* 4.4 Unit tests focales
    - Estructura del link generado (contiene `ctx`, `exp`, `sig`)
    - Expiración exacta a 10 minutos desde la generación
    - Status transitions: `active → used`, `active → expired` (por TTL)
    - Conditional update idempotente sobre marcado como `used`
    - _Requisitos: 8.3, 8.4_

- [ ] 5. Configurar el Bedrock Agent (Conversational_Agent)
  - [ ] 5.1 Crear el Bedrock Agent y conectar las Action Groups
    - Crear el Bedrock Agent vía CDK con foundation model `anthropic.claude-3-5-haiku-20241022-v1:0`
    - Configurar `idleSessionTTLInSeconds = 1800` (30 minutos)
    - Configurar `maxTokens = 2048` y `maxHopsPerTurn = 5`
    - Adjuntar los 3 Action Groups apuntando a los `openapi.yaml` y las Lambdas correspondientes
    - Configurar IAM role del Agent con permisos para invocar las 3 Lambdas
    - Crear alias `prod` (para producción) y `staging` (para validación pre go-live)
    - _Requisitos: 2.1, 3.1, 3.3, 9.1, 9.4_

  - [ ] 5.2 Definir el system prompt (instructions) del Agent
    - Restringir respuestas al dominio bancario/financiero de BTG Pactual
    - Instruir el disclaimer en respuestas con datos financieros
    - Instruir el flujo de confirmación explícita para operaciones
    - Instruir la invocación de `generate-secure-link` cuando se requiera autenticación formal
    - Instruir el comportamiento ante ambigüedad (preguntar antes de actuar)
    - Manejar expresiones coloquiales colombianas y terminología bancaria local
    - _Requisitos: 2.2, 2.3, 7.2, 9.4, 11.1, 11.2, 11.6_

  - [ ] 5.3 Configurar Bedrock Guardrails
    - Crear un Guardrail con `denied_topics`: inversión-recomendaciones, advice financiero personalizado, statements prospectivos de mercado
    - Configurar `content_filters` con thresholds altos en hate/violence/insult y bajos en misconduct/prompt_attack
    - Habilitar `pii_detection` con `redact` para `EMAIL`, `PHONE`, `CREDIT_DEBIT_NUMBER` (en outputs)
    - Adjuntar el Guardrail al Bedrock Agent
    - _Requisitos: 11.1, 11.3, 11.4, 11.5, 14.4_

  - [ ]* 5.4 Validación manual del comportamiento del Agent
    - Probar 20+ frases en español colombiano de cada tipo: consultas de saldo, movimientos, análisis de gasto, estado de producto, transferencias
    - Probar mensajes off-topic ("recomiéndame qué acción comprar") → verificar redirección por Guardrails
    - Probar ambigüedad ("hazme una transferencia") → verificar pregunta de aclaración
    - Probar multi-intent ("dame mi saldo y haz una transferencia de 100 a la cuenta X") → verificar manejo secuencial
    - Probar flujo completo de operación con confirmación
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 7.1, 7.2, 11.1, 11.2_

- [ ] 6. Checkpoint - Validar Action Groups + Agent end-to-end vía Bedrock Console
  - Invocar el Bedrock Agent desde la consola con prompts de prueba
  - Verificar que las Action Groups respondan correctamente
  - Verificar que los Guardrails bloqueen contenido fuera de banca
  - Verificar que los traces del Agent aparezcan en CloudWatch
  - Preguntar al usuario antes de avanzar si surgen dudas

- [ ] 7. Implementar el WhatsApp Gateway
  - [ ] 7.1 Implementar el handler de mensajes entrantes (trigger SNS)
    - Parsear la notificación SNS desde AWS End User Messaging Social
    - Normalizar el payload a la interface `InboundMessage`
    - Computar `sessionId = hash(phoneNumber)` (estable mientras dure la sesión)
    - Generar `correlation_id` único por mensaje entrante
    - Deduplicación inline: `PutItem` condicional sobre `Conversations` con `PK=WHATSAPP_MSG#{messageId}`, TTL 5 min. Si `ConditionalCheckFailedException`, descartar como duplicado
    - Manejar mensajes de números no registrados con welcome message + prompt de verificación
    - _Requisitos: 1.1, 1.4, 1.5_

  - [ ] 7.2 Implementar la invocación al Bedrock Agent
    - Llamar a `BedrockAgentRuntime.invokeAgent` con `agentId`, `agentAliasId='prod'`, `sessionId`, `inputText`
    - Configurar `sessionAttributes` con `clientId`, `phoneNumber`, `correlationId` para que las Action Groups los reciban
    - Configurar timeout del SDK call a 15s (Bedrock Agent puede tomar hasta este tiempo en flujos complejos)
    - Recibir la respuesta del Agent
    - _Requisitos: 1.1, 9.1_

  - [ ] 7.3 Implementar la entrega de mensajes salientes
    - Serializar `OutboundMessage` al payload de AWS End User Messaging Social
    - Soportar texto, botones interactivos y listas interactivas
    - Llamar a la SendMessage API
    - _Requisitos: 1.2, 1.3_

  - [ ] 7.4 Implementar el handling de fallas degradadas
    - Si la invocación a Bedrock falla (5xx o timeout), responder al cliente con mensaje degradado gentil: "Nuestro asistente está temporalmente no disponible. Por favor intenta en unos minutos."
    - Loggear la falla con `event_type=bedrock_unavailable` para que la alarma de error rate la capture
    - Configurar DLQ sobre el async path del Gateway (para mensajes que no se pudieron procesar)
    - _Requisitos: 12.5_

  - [ ]* 7.5 Unit tests focales
    - Parseo de SNS con varios tipos de mensaje (texto, botón, lista)
    - Deduplicación: dos mensajes con mismo `whatsapp_message_id` → segundo descartado
    - Mensaje desde número no registrado → welcome message
    - Bedrock unavailable → mensaje degradado
    - Serialización de `OutboundMessage` a payload de WhatsApp por cada tipo
    - _Requisitos: 1.1, 1.3, 1.4, 1.5, 12.5_

- [ ] 8. Implementar el Audit Pipeline (managed, todo en CDK)
  - [ ] 8.1 Configurar Lambda Powertools en todas las Lambdas
    - Estructurar los logs según el esquema canónico de audit (`correlation_id`, `session_id`, `event_type`, `component`, `payload`, `duration_ms`, `status`)
    - Configurar `Logger`, `Tracer` (X-Ray) y `Metrics` (custom CloudWatch metrics)
    - Habilitar la propagación de `correlation_id` a través de los Lambda invocations
    - _Requisitos: 10.1, 10.2, 10.3, 10.6, 13.1, 13.3_

  - [ ] 8.2 Configurar el pipeline CloudWatch → Firehose → S3
    - Crear S3 bucket `btg-connect-ai-audit` con KMS encryption (mismo CMK)
    - Crear Kinesis Data Firehose delivery stream con destino S3, formato Parquet vía Glue Schema Registry
    - Configurar particionamiento del S3 por fecha: `year=YYYY/month=MM/day=DD/`
    - Configurar buffer hints de Firehose: 60s o 5MB (lo que ocurra primero)
    - Subscription filter en cada log group de las 4 Lambdas + log group del Bedrock Agent → enruta al Firehose
    - _Requisitos: 10.1, 10.2, 10.3_

  - [ ] 8.3 Configurar lifecycle policy de S3 (compliance 7 años)
    - Transición Standard → Glacier Instant Retrieval a los 30 días
    - Transición Glacier Instant Retrieval → Glacier Deep Archive a 1 año
    - Expiración a 7 años (2,557 días)
    - Habilitar S3 Object Lock con governance mode para compliance regulatorio
    - _Requisitos: 10.4_

  - [ ] 8.4 Configurar Glue Data Catalog y Athena workgroup
    - Crear Glue database `btg_connect_ai_audit`
    - Crear tabla particionada apuntando al bucket S3 con el schema del audit record
    - Crear Athena workgroup `compliance` con encryption habilitado y output a un bucket de queries
    - Documentar queries de ejemplo en `docs/audit-queries.md` (filtrar por cliente, por sesión, por tipo de evento, por rango de fechas)
    - _Requisitos: 10.5_

  - [ ]* 8.5 Validación del audit pipeline
    - Generar 10 conversaciones de prueba en staging
    - Verificar que los logs aparezcan en CloudWatch dentro de 1 minuto
    - Verificar que el Firehose entregue a S3 dentro de 5 minutos
    - Ejecutar query Athena sobre los datos archivados y verificar completitud
    - Verificar que `masked_fields` esté correctamente poblado (últimos 4 dígitos de cuenta, montos omitidos)
    - _Requisitos: 10.1, 10.2, 10.3, 14.4_

- [ ] 9. Implementar Observabilidad y Alarmas
  - [ ] 9.1 Publicar métricas custom de CloudWatch
    - `MessagesProcessed` (count) por componente
    - `InvocationLatency` (ms, percentiles p50/p95/p99) por componente
    - `ErrorRate` (count de eventos con status=failure / total)
    - `ActiveSessions` (gauge basado en items con `SK=META` y `ttl > now`)
    - _Requisitos: 13.2_

  - [ ] 9.2 Configurar alarmas críticas
    - Alarma `HighErrorRate`: `ErrorRate > 5%` sostenido por 5 minutos → SNS topic a equipo de ops
    - Alarma `HighLatency`: `p95(InvocationLatency) > 5s` sostenido por 5 minutos → SNS topic
    - Alarma `BedrockUnavailable`: `event_type=bedrock_unavailable` count > 10 en 5 min → SNS topic
    - Alarma `AuditPipelineFailure`: Firehose `DeliveryToS3.Records` failure → SNS topic crítico
    - Alarma `DLQNotEmpty`: mensajes en DLQ del Gateway > 0 → SNS topic
    - _Requisitos: 13.4, 13.5_

  - [ ] 9.3 Crear el CloudWatch Dashboard operativo
    - Widget de throughput de mensajes (líneas por componente)
    - Widget de latencia p50/p95/p99 (líneas por componente)
    - Widget de error rate (porcentaje a lo largo del tiempo)
    - Widget de sesiones activas (gauge)
    - Widget de estado del audit pipeline (Firehose delivery rate, S3 puts)
    - _Requisitos: 13.6_

- [ ] 10. Property tests críticas (Hypothesis)
  - [ ] 10.1 Property 1: Validez de firma del Secure Link
    - Generar `operation_context` arbitrarios y verificar que la firma HMAC-SHA256 sea consistente
    - Verificar que recalcular la firma con el mismo secret reproduzca el mismo valor
    - _Valida: Requisitos 8.1, 8.4_

  - [ ] 10.2 Property 2: Rechazo de Secure Links manipulados/expirados
    - Generar links válidos, luego manipular bytes del `ctx` o `exp` y verificar que la validación los rechace
    - Generar links con `exp ≤ now` y verificar rechazo
    - _Valida: Requisitos 8.3, 8.5_

  - [ ] 10.3 Property 3: Validación de saldo
    - Generar pares `(requested, available)` y verificar que la validación apruebe sii `requested ≤ available`
    - _Valida: Requisito 7.3_

  - [ ] 10.4 Property 4: Idempotencia exactly-once
    - Generar N envíos (N ≥ 1) con misma `idempotency_key` y verificar que solo el primero invoca la Bank_Core_API
    - Verificar que envíos subsiguientes retornen el resultado original sin re-ejecución
    - _Valida: Requisito 7.4_

  - [ ] 10.5 Property 5: Deduplicación de mensajes entrantes
    - Generar mensajes con mismo `whatsapp_message_id` enviados dentro de 5 minutos y verificar que el segundo se descarte
    - _Valida: Requisito 1.5_

  - [ ] 10.6 Property 6: Disclaimer en respuestas con datos financieros
    - Generar respuestas del Agent que contengan datos financieros (saldos, montos) y verificar que el disclaimer esté presente
    - Generar respuestas sin datos financieros (saludos) y verificar que el disclaimer NO se incluye (opcional)
    - _Valida: Requisito 11.6_

  - [ ] 10.7 Property 7: Enmascaramiento de data sensible en logs
    - Generar números de cuenta arbitrarios y verificar que la representación en logs solo muestre los últimos 4 dígitos
    - Verificar que montos no aparezcan literalmente en logs (solo presencia)
    - _Valida: Requisito 14.4_

  - [ ] 10.8 Property 8: Expiración del session token a máximo 30 minutos
    - Generar tokens y verificar `expires_at ≤ issued_at + 30min`
    - _Valida: Requisito 14.5_

  - [ ] 10.9 Property 9: Correlation ID único por sesión
    - Generar pares de sesiones distintas y verificar `correlation_id` diferentes
    - Verificar que todos los logs de una misma sesión compartan el mismo `correlation_id`
    - _Valida: Requisito 10.6_

  - [ ] 10.10 Property 10: Completitud del audit record
    - Generar eventos de cada `event_type` y verificar presencia de los campos canónicos (`correlation_id`, `session_id`, `timestamp`, `event_type`, `component`, `duration_ms`, `status`)
    - Verificar que eventos `operational_request` contengan parámetros sanitizados y `confirmation_number`
    - _Valida: Requisitos 10.1, 10.2, 10.3_

- [ ] 11. Validación manual end-to-end en staging
  - [ ] 11.1 Setup del entorno staging
    - Desplegar el stack CDK contra cuenta AWS de staging
    - Configurar un mock de la Bank_Core_API (o sandbox del banco si existe)
    - Conectar un número de WhatsApp Business de prueba
    - _Requisitos: 12.4_

  - [ ] 11.2 Escenarios funcionales
    - Flujo de consulta: "cuál es mi saldo" → respuesta con saldo + disclaimer
    - Flujo multi-step: "haz una transferencia de 500 mil a la cuenta X" → recolección → confirmación → secure link → completion
    - Expiración de sesión: esperar 30 min de inactividad → siguiente interacción inicia sesión fresca
    - Multi-intent: "dame mi saldo y los últimos 5 movimientos" → manejo secuencial
    - _Requisitos: 1.1, 3.3, 4.1, 7.1, 7.2, 8.1, 9.1_

  - [ ] 11.3 Escenarios de error y degradación
    - Bank_Core_API mock retorna 500 → mensaje degradado al cliente, sesión continúa
    - Bedrock mock retorna error → fallback message del Gateway al cliente
    - Mensaje off-topic ("recomiéndame una inversión") → redirección por Guardrails
    - Patrón sospechoso de fraude → sesión terminada + alerta de seguridad en CloudWatch
    - Mensaje duplicado (mismo `whatsapp_message_id`) → descartado sin re-procesar
    - _Requisitos: 11.2, 11.4, 11.5, 12.5, 1.5_

  - [ ] 11.4 Validación de observabilidad y compliance
    - Verificar que las 4 alarmas críticas disparen ante condiciones simuladas
    - Verificar que el CloudWatch Dashboard muestre las métricas correctamente
    - Ejecutar query Athena sobre la sesión de pruebas y validar completitud del audit trail
    - Verificar enmascaramiento de números de cuenta en todos los logs
    - _Requisitos: 10.5, 13.4, 13.5, 13.6, 14.4_

- [ ] 12. Go-live checklist
  - Confirmar revisión de seguridad por el equipo de InfoSec
  - Confirmar revisión de compliance por el equipo legal (Ley 1581, retención 7 años)
  - Aprobar el runbook operativo: cómo responder a cada alarma crítica
  - Aprobar el plan de rollback (vía CDK + Bedrock Agent aliases)
  - Desplegar a producción vía pipeline CDK
  - Smoke test post-deploy con un número de WhatsApp interno
  - Monitorear las primeras 24 horas con el equipo de ops on-call

## Notas

- Las tareas marcadas con `*` son opcionales para acelerar el MVP inicial. Pueden agregarse después del go-live.
- Cada tarea referencia requisitos específicos de `requirements.md` para trazabilidad.
- Los componentes diferidos a **Fase 2** están en `requirements.md` con sus triggers explícitos — no implementar antes de que el trigger se cumpla.
- Lambdas Python: `financial-query`, `submit-operation`, `generate-secure-link`.
- Lambda TypeScript: `whatsapp-gateway`.
- Recurso managed sin código custom: Bedrock Agent (`Conversational_Agent`).
- Property-based testing con Hypothesis (Python), mínimo 100 iteraciones por property (200 para las críticas: 1, 2, 3, 4).
- Infrastructure-as-code en AWS CDK (TypeScript) — un solo stack `BtgConnectAiStack` con todos los recursos.
- Tablas DynamoDB: `Conversations` (single-table: dedup + idempotencia + sesión hot) y `SecureLinks`.
- Audit pipeline 100% managed: CloudWatch Logs → Kinesis Data Firehose → S3 (Parquet, KMS, particionado por fecha) → Athena on-demand.

## Gráfico de Dependencias de Tareas

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "4.3"] },
    { "id": 3, "tasks": ["2.3", "3.3", "4.4"] },
    { "id": 4, "tasks": ["5.1", "5.2", "5.3"] },
    { "id": 5, "tasks": ["5.4", "6"] },
    { "id": 6, "tasks": ["7.1", "7.2", "7.3", "7.4"] },
    { "id": 7, "tasks": ["7.5", "8.1"] },
    { "id": 8, "tasks": ["8.2", "8.3", "8.4"] },
    { "id": 9, "tasks": ["8.5", "9.1", "9.2", "9.3"] },
    { "id": 10, "tasks": ["10.1", "10.2", "10.3", "10.4", "10.5", "10.6", "10.7", "10.8", "10.9", "10.10"] },
    { "id": 11, "tasks": ["11.1"] },
    { "id": 12, "tasks": ["11.2", "11.3", "11.4"] },
    { "id": 13, "tasks": ["12"] }
  ]
}
```
