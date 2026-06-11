# Implementation Plan: BTG ConnectAI MVP

## Overview

Implementación incremental de un asistente bancario conversacional para WhatsApp usando Twilio (sandbox), Amazon API Gateway, Strands Agent SDK sobre Amazon Bedrock (Claude Haiku 3.5). **Stack 100% Python 3.13** para todas las Lambdas. IaC con **CloudFormation puro (YAML)** siguiendo el patrón del repo `infra`. **Estrategia de red híbrida**: las Lambdas del dominio bancario (`balance_query`, `transfer_breb_validate`, `transfer_breb_execute`, `statement_generator`) corren en subnets privadas con acceso a AWS solo vía VPC Endpoints (sin NAT, cero salida a internet); el resto corre fuera de VPC en la red managed de Lambda (necesitan Twilio/APIs AWS públicas, seguras por IAM).

**Reparto de IaC entre repos (clave de este plan):**

- **`infra` (repo separado) crea TODOS los recursos compartidos**: API Gateway (HTTP API), todas las colas SQS (FIFO inbound + email/sms notifications + DLQs), todos los buckets S3 (Statement, Audio_Temp, Login_Page, artefactos de Lambda), todas las tablas DynamoDB (Consent_Store, Auth_Session, OTP_Store), Bedrock (Agent Core + Guardrails), la red (Security Group `BankingLambdaSG` + VPC Endpoints), Secrets Manager, SNS de alarmas y la observabilidad compartida (Dashboard/Alarms). **Estos recursos NO se crean en este repo.**
- **`BTG-ConnectAI` (este repo) define SOLO cómputo y wiring**: cada Lambda (con su IAM role), la state machine `TransferBrebStateMachine` (con su rol), los Event Source Mappings (SQS de `infra` → Lambda de la app), el wiring de API Gateway (Integration + Route + `Lambda::Permission` usando el `HttpApiId` importado), el `Lambda::Permission` para que el Bedrock Agent invoque las Action Group Lambdas, el código de aplicación Python 3.13 y el contenido de la Login_Page estática.
- **Contrato cross-stack**: la app resuelve los recursos de `infra` mediante CloudFormation `Outputs`/`Export` (`Fn::ImportValue`) y/o SSM Parameter Store (`AWS::SSM::Parameter::Value<String>` / `{{resolve:ssm:...}}`), con convención `${ProjectName}-${Environment}-<Recurso>` y namespace SSM `/btgconnectai/sandbox/...`.
- **PRERREQUISITO / Orden de despliegue**: `infra` se despliega **primero** y publica el contrato (Exports + SSM); luego se despliega `BTG-ConnectAI`. El deploy de la app **falla rápido** si el contrato no está publicado.

**Patrones arquitectónicos clave:**

- **Async Webhook Pattern**: `Webhook_Receiver` (sync, responde 200 a Twilio en <1s) → SQS FIFO → `Message_Processor` (async). Twilio nunca experimenta timeouts.
- **SQS FIFO** con dedup nativa (`MessageDeduplicationId = MessageSid`) — elimina la tabla Dedup custom — y orden por cliente (`MessageGroupId = phoneNumber`).
- **AWS Step Functions** orquesta la transferencia BRE-B (`TransferBrebStateMachine`) usando `waitForTaskToken` para el callback del OTP — sin bloquear Lambdas.
- **Amazon SQS** desacopla las notificaciones (email, SMS post-operación): productores fire-and-forget, consumidores en batch con DLQ.

**Convenciones de empaquetado (CloudFormation, sin CDK/SAM):**

- Código Lambda en `src/lambdas/<nombre>/handler.py`; se zipea y sube al **bucket de artefactos creado por `infra`** (nombre resuelto del contrato); el template referencia `Code: {S3Bucket, S3Key}`.
- Dependencias compartidas (`twilio`, `aws-lambda-powertools`, `strands-agents`, código `src/shared/`) en un **Lambda Layer** común.
- Tests con **pytest** (unit/integration) y **hypothesis** (property-based).

## Tasks

- [ ] 1. Set up project structure, shared layer, and CloudFormation foundation
  - [ ] 1.1 Initialize project structure
    - Create directory structure: `cloudformation/templates/connectai/`, `cloudformation/stacks/sandbox/`, `cloudformation/state-machines/`, `.github/workflows/`, `src/lambdas/`, `src/shared/`, `src/login-page/`, `src/tests/unit/`, `src/tests/property/`, `src/tests/integration/`
    - **NOTA del reparto**: `cloudformation/templates/connectai/` contendrá SOLO templates de cómputo/wiring de la app: `iam.yaml`, `lambdas-ingestion.yaml`, `lambdas-actions.yaml`, `lambdas-notify.yaml`, `lambda-ai-agent.yaml`, `state-machine.yaml`. El stack raíz es `cloudformation/stacks/sandbox/connectai-app.yaml` y la definición ASL `cloudformation/state-machines/transfer-breb.asl.json`. **NO** se crean carpetas/archivos de recursos compartidos (data/storage/queues/secrets/vpc-access/guardrails/api-gateway/observability): esos los crea `infra`
    - Create `src/requirements.txt` (runtime): `twilio`, `aws-lambda-powertools[parser]`, `strands-agents`, `fpdf2` (o `reportlab`)
    - Create `src/requirements-dev.txt` (dev): `pytest`, `hypothesis`, `moto`, `boto3-stubs`, `cfn-lint`, `checkov`
    - Create `pyproject.toml` con config de pytest, ruff/black y target Python 3.13
    - _Requirements: 15.4, 15.5_

  - [ ] 1.2 Implement shared utilities (Lambda Layer)
    - Create `src/shared/logger.py` — config de `aws_lambda_powertools.Logger` con service name y JSON estructurado
    - Create `src/shared/masking.py` — masking de teléfono (últimos 4), cuenta (últimos 4), documento (últimos 4)
    - Create `src/shared/constants.py` — constantes (`MAX_TWILIO_MESSAGE_LENGTH=1600`, `AUTH_SESSION_TTL=1800`, `OTP_TTL=300`, `TC_VERSION="1.0"`)
    - Create `src/shared/types.py` — `TypedDict`s compartidos: `TwilioWebhookPayload`, `ConsentRecord`, `AuthSession`, `OTPRecord`, `MockClient`, `MockProduct`, `MockTransaction`, `EmailNotificationEvent`, `SmsNotificationEvent`
    - Create `src/shared/formatting.py` — formato de moneda COP ($X.XXX.XXX,YY)
    - Empaquetar `src/shared/` + deps de `requirements.txt` como Lambda Layer (`layer/python/`)
    - _Requirements: 13.1, 14.4, 10.5_

  - [ ]* 1.3 Write property tests for shared utilities (hypothesis)
    - **Property 4: Data Masking Correctness** — Para cualquier string ≥ 4 chars, masking deja solo últimos 4 visibles
    - **Property 15: COP Currency Formatting** — Para cualquier número no-negativo, produce patrón $X.XXX.XXX,YY
    - _Validates: Requirements 14.4, 10.5_

  - [ ]* 1.4 Write unit tests for shared utilities (pytest)
    - Test masking edge cases (strings < 4 chars, empty strings)
    - Test COP formatting con 0, enteros, números grandes, decimales
    - _Requirements: 14.4, 10.5_

- [ ] 2. Declare cross-stack contract in the app root stack
  - [ ] 2.1 Create app root stack with contract parameters (`connectai-app.yaml`)
    - Create `cloudformation/stacks/sandbox/connectai-app.yaml` con la sección `Parameters` del **contrato cross-stack**, resueltos vía SSM (`AWS::SSM::Parameter::Value<String>`) y/o `Fn::ImportValue`, siguiendo la tabla de naming del design (Export `${ProjectName}-${Environment}-<Recurso>` / namespace SSM `/btgconnectai/sandbox/...`):
      - API Gateway: `HttpApiId`, `HttpApiEndpoint`
      - SQS: `InboundQueueArn`/`InboundQueueUrl`, `EmailQueueArn`/`EmailQueueUrl`, `SmsQueueArn`/`SmsQueueUrl`
      - DynamoDB: `ConsentTableName`/`ConsentTableArn`, `AuthTableName`/`AuthTableArn`, `OtpTableName`/`OtpTableArn`
      - S3: `StatementBucketName`/`StatementBucketArn`, `AudioTempBucketName`/`AudioTempBucketArn`, bucket de artefactos de Lambda
      - Bedrock: `BedrockAgentArn`/`BedrockAgentId`, `GuardrailId`/`GuardrailVersion`
      - Secrets/SNS: `TwilioSecretArn`, `AlarmsTopicArn`
      - Red: `BankingLambdaSGId`, `PrivateSubnetIds` (CSV → `Fn::Split`)
    - Parámetros propios de la app: `ProjectName=BTGConnectAI`, `Environment=sandbox`, `LambdaCodeKey` (git-sha)
    - Estos valores se pasarán por nested stack a cada template de Lambda/SFN en la Task 15
    - **NOTA de PRERREQUISITO**: el repo `infra` debe estar desplegado y haber publicado el contrato (Exports + parámetros SSM) ANTES de desplegar este stack. Si un parámetro/Export no existe, el deploy falla rápido
    - _Requirements: 15.1, 15.4, 15.5_

- [ ] 3. Checkpoint — `cfn-lint` pasa en el stack raíz con el contrato
  - `cfn-lint cloudformation/stacks/sandbox/connectai-app.yaml`. Verificar que los parámetros del contrato están bien tipados (`AWS::SSM::Parameter::Value<String>`) y que no hay ARNs hardcodeados. Ask the user if questions arise.

- [ ] 4. Implement Webhook_Receiver Lambda (sync, behind API Gateway)
  - [ ] 4.1 Implement Twilio signature validation
    - Create `src/lambdas/webhook_receiver/twilio_signature.py`
    - `validate_twilio_signature(auth_token, signature, url, params)` — usa `twilio.request_validator.RequestValidator`
    - Si la firma no coincide, el handler responde 403 sin encolar
    - _Requirements: 3.2_

  - [ ] 4.2 Implement form-urlencoded parser
    - Create `src/lambdas/webhook_receiver/parser.py`
    - `parse_form_urlencoded(body, is_base64)` — usa `urllib.parse.parse_qs`
    - Extrae `MessageSid`, `From`, `To`, `Body`, `NumMedia`, `MediaUrl0`, `MediaContentType0`, `ButtonPayload`, `ProfileName`
    - _Requirements: 3.1_

  - [ ] 4.3 Implement SQS enqueue logic
    - Create `src/lambdas/webhook_receiver/enqueue.py`
    - `enqueue_message(payload, correlation_id)` — boto3 `sqs.send_message` sobre la **inbound queue de `infra`** (URL en env, resuelta del contrato) con `MessageGroupId=From`, `MessageDeduplicationId=MessageSid`, body JSON con `correlationId` y `receivedAt`
    - _Requirements: 3.3, 3.4_

  - [ ] 4.4 Implement Webhook_Receiver main handler
    - Create `src/lambdas/webhook_receiver/handler.py`
    - Decorador `@logger.inject_lambda_context`; genera `correlation_id` (UUID v4) antes de todo
    - Pipeline: validar firma → parsear body → enqueue → `{"statusCode": 200, "body": ""}`
    - Firma inválida → 403; fallo SQS → 5xx (Twilio reintenta, SQS FIFO descarta duplicado)
    - Target latency: <1s p99
    - _Requirements: 3.1, 3.2, 3.3, 13.2_

  - [ ]* 4.5 Write unit tests for Webhook_Receiver (pytest + moto)
    - Firma válida aceptada, inválida → 403
    - Parser maneja todos los campos
    - `send_message` llamado con MessageGroupId y DeduplicationId correctos
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 5. Implement Message_Processor Lambda (async, SQS-triggered)
  - [ ] 5.1 Implement consent flow module
    - Create `src/lambdas/message_processor/consent.py`
    - `get_consent`, `store_consent`, `handle_consent_flow` (ButtonPayload accept_tc/reject_tc), `send_terms_and_conditions_message` (Twilio Content Template)
    - Acceso a Consent_Store de `infra` (nombre de tabla resuelto del contrato vía env)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 5.2 Write property test for consent gate (hypothesis)
    - **Property 5: Existing Consent Skips T&C**
    - _Validates: Requirement 1.4_

  - [ ] 5.3 Implement auth session module
    - Create `src/lambdas/message_processor/auth.py`
    - `get_auth_session`, `is_expired`, `store_pending_request`, `send_login_link` (Twilio REST), `derive_session_id` (determinista)
    - Acceso a Auth_Session de `infra` (nombre de tabla del contrato)
    - _Requirements: 5.1, 5.6, 5.8, 6.1, 6.2, 11.1_

  - [ ]* 5.4 Write property tests for auth and session (hypothesis)
    - **Property 3: Session ID Determinism**, **Property 6: No Session → Login**, **Property 7: Active Session → Proceed**
    - _Validates: Requirements 11.1, 5.1, 5.8, 6.1_

  - [ ] 5.5 Implement audio transcription module
    - Create `src/lambdas/message_processor/transcription.py`
    - `transcribe_audio(twilio_media_url, phone_number)` — descarga audio de Twilio Media URL (Basic Auth con creds Twilio), sube al Audio_Temp bucket de `infra` (nombre del contrato), `transcribe.start_transcription_job` (es-CO, OGG), polling (max 30s), cleanup
    - _Requirements: 2.2, 2.3, 2.6_

  - [ ] 5.6 Implement Twilio messaging module
    - Create `src/lambdas/message_processor/messaging.py`
    - `send_twilio_message` (split > 1600), `split_message`, `send_welcome_message`, `send_twilio_document` (presigned URL 5min + `media_url`)
    - _Requirements: 3.7, 3.10, 4.1, 4.2, 9.4_

  - [ ]* 5.7 Write property test for message splitting (hypothesis)
    - **Property 1: Message Splitting Round-Trip** — chunks ≤ 1600 chars
    - _Validates: Requirement 3.10_

  - [ ] 5.8 Implement OTP callback handler (priority routing) — ver Task 9
    - Create `src/lambdas/message_processor/otp_callback.py`
    - Si hay registro activo en `OTP_Store` (tabla de `infra`), este flujo tiene prioridad sobre el Strands Agent
    - _Requirements: 16.4, 16.5, 16.6, 16.7_

  - [ ] 5.9 Implement Message_Processor main handler
    - Create `src/lambdas/message_processor/handler.py`
    - Usar `aws_lambda_powertools.utilities.batch.process_partial_response` con `BatchProcessor(EventType.SQS)`
    - `record_handler` por mensaje: parse body → set `correlation_id` del mensaje (NO regenerar) → OTP callback check → consent → tipo (text/audio/button/unsupported) → auth → invoke Strands_Agent → enviar respuesta (texto + PDF si aplica) vía Twilio
    - Errores levantan excepción → Powertools reporta como `batchItemFailure` para reintento SQS individual
    - _Requirements: 2.1, 2.4, 2.5, 3.5, 3.6, 3.9, 5.1, 13.1, 13.2_

  - [ ]* 5.10 Write property test for unsupported message format (hypothesis)
    - **Property 16: Unsupported Format Rejection**
    - _Validates: Requirement 2.5_

  - [ ]* 5.11 Write unit tests for Message_Processor (pytest + moto)
    - Consent flow, auth (expirada/válida/ausente), routing (text/audio/button/unsupported)
    - OTP callback priority: con OTP pendiente NO se invoca el Strands Agent
    - Partial batch failure: solo el mensaje fallido se reporta
    - _Requirements: 1.1, 1.2, 1.3, 2.5, 3.5, 5.1, 6.5, 16.4_

  - [ ] 5.12 Checkpoint — Webhook_Receiver + Message_Processor tests pasan
    - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement Auth_Service Lambda and Login_Page
  - [ ] 6.1 Implement Auth_Service Lambda
    - Create `src/lambdas/auth_service/handler.py` — POST /authenticate (Function URL)
    - Create `src/lambdas/auth_service/users.py` — usuarios de prueba hardcodeados (carlos.rodriguez, maria.lopez, juan.garcia) con credenciales, phone_number, name, document_id, email
    - Logic: validar callback token → buscar user por username+password → verificar phone → `put_item` Auth_Session (tabla de `infra`, TTL 30min) → success/failure
    - CORS headers para Login_Page origin
    - _Requirements: 5.2, 5.3, 5.5, 5.7, 6.1_

  - [ ]* 6.2 Write property tests for Auth_Service (hypothesis)
    - **Property 8: Invalid Credentials Rejection**
    - _Validates: Requirement 5.5_

  - [ ]* 6.3 Write unit tests for Auth_Service (pytest)
    - Credenciales válidas crean sesión con TTL correcto; username inválido/phone incorrecto/token inválido → error
    - _Requirements: 5.3, 5.5, 5.7_

  - [ ] 6.4 Implement Login_Page (S3 static site)
    - Create `src/login-page/index.html` — formulario de login, branding BTG, responsive
    - Create `src/login-page/styles.css` — colores BTG, mobile-first
    - Create `src/login-page/app.js` — **JavaScript de navegador** (no Lambda): extrae phone/token de URL params, POST a Auth_Service, muestra success/error
    - **NOTA**: el bucket `Login_Page` lo crea `infra`; aquí solo se produce el contenido estático que el pipeline sube a ese bucket
    - _Requirements: 5.2_

- [ ] 7. Implement Action Group / Tool Lambdas (Python 3.13)
  - [ ] 7.1 Implement balance-query Lambda
    - Create `src/lambdas/balance_query/handler.py` — recibe JSON del Strands Agent (Lambda invoke), rutea a `get_balance`
    - Create `src/lambdas/balance_query/mock_data.py` — Mock_Core: 3 clientes con fondos + cuentas corrientes
    - Logic: buscar cliente por phone → filtrar por product_type → todos si no hay filtro → error si no existe
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 7.2 Write property tests for balance-query (hypothesis)
    - **Property 9: Balance Query Correctness**, **Property 10: Unknown Client Error**
    - _Validates: Requirements 7.1-7.4_

  - [ ] 7.3 Implement transfer-breb Lambdas (initiator, validate, execute)
    - Create `src/lambdas/transfer_breb/initiator.py` — Tool del Strands Agent. `stepfunctions.start_execution` sobre `TransferBrebStateMachine` con `name = correlationId` (idempotencia), retorna `{executionArn, status: "STARTED"}` inmediatamente
    - Create `src/lambdas/transfer_breb/validate.py` — estado `ValidateTransfer`. Valida contra Mock_Core; lanza `InsufficientFundsError` / `InvalidDestinationError`; sino retorna `{valid: True, ...}`
    - Create `src/lambdas/transfer_breb/execute.py` — estado `ExecuteTransfer`. Actualiza saldos Mock_Core, genera `receipt` con transactionId único
    - Create `src/lambdas/transfer_breb/mock_data.py` — Mock_Core compartido (o importado del Layer)
    - Errores custom en `src/shared/errors.py` (`InsufficientFundsError(Exception)`, etc.)
    - _Requirements: 8.3, 8.4, 8.10, 8.11, 8.12, 18.7_

  - [ ]* 7.4 Write property tests for transfer-breb validate/execute (hypothesis)
    - **Property 11: Valid Transfer Produces Receipt**
    - **Property 12: Insufficient Funds Rejection** — amount > available_balance lanza `InsufficientFundsError`, balance sin cambios
    - **Property 19: Idempotent StartExecution** — mismo `correlationId` → una sola ejecución (colisión de `name` rechazada por AWS)
    - _Validates: Requirements 8.3, 8.10, 18.7_

  - [ ] 7.5 Implement statement-generator Lambda
    - Create `src/lambdas/statement_generator/handler.py` — recibe del Strands Agent
    - Create `src/lambdas/statement_generator/pdf_generator.py` — PDF con `fpdf2`/`reportlab` (nombre, cuenta enmascarada, período, movimientos, saldo final)
    - Create `src/lambdas/statement_generator/mock_data.py`
    - Logic: validar fecha (pasada) → datos cliente → generar PDF → `put_object` al Statement_Bucket de `infra` (nombre del contrato, vía Gateway Endpoint S3) → retornar `{s3Bucket, s3Key, fileName}` para que `Message_Processor` lo entregue al cliente vía WhatsApp (Twilio Media). NO publica a `email-notification-queue` — el extracto se entrega solo por WhatsApp.
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 17.3_

  - [ ]* 7.6 Write property tests for statement-generator (hypothesis)
    - **Property 13: Future Date Rejection**, **Property 14: Valid Statement Returns S3 Reference**
    - _Validates: Requirements 9.2, 9.3, 14.7_

  - [ ]* 7.7 Write unit tests for Action Group Lambdas (pytest)
    - balance-query (todos/filtrado/no encontrado), transfer (válido/fondos insuficientes/destino inválido), statement (válido/fecha futura/sin movimientos)
    - _Requirements: 7.1-7.4, 8.4, 8.7, 8.8, 9.2, 9.3_

- [ ] 8. Implement OTP_Service Lambda (Task Token Pattern)
  - [ ] 8.1 Implement OTP generate-and-wait
    - Create `src/lambdas/otp_service/handler.py` — invocada por Step Functions con `lambda:invoke.waitForTaskToken`
    - Recibe `{operation: "generate-and-wait", phoneNumber, transferAmount, destinationAccount, taskToken}`
    - Genera código 6 dígitos, `put_item` en `OTP_Store` de `infra` (nombre del contrato) `{pk, code, taskToken, executionArn, attempts: 0, transferContext, ttl: now+300}`
    - Envía SMS vía Pinpoint (`pinpoint.send_messages`) con monto + cuenta destino enmascarada + código
    - Retorna `{"ok": True}` — la Lambda termina; Step Functions queda esperando el taskToken
    - _Requirements: 16.1, 16.2, 16.3_

  - [ ]* 8.2 Write unit tests for OTP_Service (pytest + moto)
    - Genera código numérico de 6 dígitos; put_item incluye taskToken y executionArn; SMS incluye monto + destino enmascarado
    - _Requirements: 16.1, 16.2_

- [ ] 9. Implement OTP Callback in Message_Processor
  - [ ] 9.1 Implement OTP callback handler
    - Create `src/lambdas/message_processor/otp_callback.py`
    - Cuando el Message_Processor consume un mensaje y existe `OTP_Store` activo para el phone, prioriza este flujo
    - `validate_and_callback(phone_number, code)`:
      - `get_item` OTP_Store; si no existe/expiró → ignorar (Step Functions maneja timeout)
      - código correcto → `stepfunctions.send_task_success(taskToken, output={"valid": True})` + `delete_item`
      - incorrecto y `attempts < 2` → `update_item` attempts+1 + mensaje "Código incorrecto" vía Twilio
      - tercer intento fallido → `stepfunctions.send_task_failure(taskToken, error="OTPBlockedError")` + `delete_item`
    - _Requirements: 16.4, 16.5, 16.6, 16.7_

  - [ ]* 9.2 Write property tests for OTP Callback (hypothesis)
    - **Property 17: OTP Expiry** — OTP con TTL vencido es ignorado
    - **Property 18: Brute Force Block** — 3 intentos fallidos → `send_task_failure` con `OTPBlockedError`
    - _Validates: Requirements 16.6, 16.7_

  - [ ]* 9.3 Write unit tests for OTP Callback (pytest + moto)
    - Código válido → send_task_success + delete; inválido → attempts++ + retry msg; tercer fallo → send_task_failure; expirado → ignorado
    - _Requirements: 16.4-16.7_

- [ ] 10. Implement TransferBrebStateMachine (Step Functions)
  - [ ] 10.1 Define state machine in ASL
    - Create `cloudformation/state-machines/transfer-breb.asl.json` con la definición Amazon States Language
    - Estados: `ValidateTransfer` → `GenerateOTP` (`waitForTaskToken`, HeartbeatSeconds=300) → `ValidateOTP` (Choice) → `ExecuteTransfer` → `PublishNotifications` (Parallel: SQS sendMessage a las colas email + sms **de `infra`**, ARNs/URLs inyectados vía `DefinitionSubstitutions`) → `NotifyUserSuccess`
    - Estados de error: `NotifyValidationFailed`, `NotifyOTPExpired`, `NotifyOTPBlocked`, `NotifyTransferFailed`
    - Retry en Tasks: `BackoffRate: 2.0, IntervalSeconds: 2, MaxAttempts: 2` para `Lambda.ServiceException`, `Lambda.AWSLambdaException`, `Lambda.SdkClientException`
    - Catch para errores de dominio (`InsufficientFundsError`, `InvalidDestinationError`, `OTPBlockedError`, `States.Timeout`)
    - _Requirements: 18.1, 18.2, 18.3, 18.4_

  - [ ] 10.2 Implement message-handler-notify Lambda
    - Create `src/lambdas/message_handler_notify/handler.py` — invocada desde los estados terminales del state machine
    - Recibe `{phoneNumber, messageType, receipt?, error?}` y envía el mensaje apropiado en español vía Twilio REST
    - `transfer_success` incluye comprobante con disclaimer "información referencial"
    - _Requirements: 8.4, 8.7, 8.8, 8.9, 8.10, 8.11_

- [ ] 11. Implement Email_Service Lambda (SQS Triggered)
  - [ ] 11.1 Implement SQS-triggered email service
    - Create `src/lambdas/email_service/handler.py` — `process_partial_response` con `BatchProcessor(EventType.SQS)` consumiendo la `email-notification-queue` **de `infra`**
    - Por mensaje, rutear por `type`:
      - `transfer_confirmation` → `send_transfer_confirmation(to, receipt)` vía SES `send_email` (HTML template)
    - Masking de campos sensibles antes de incluirlos
    - Excepción por mensaje → reintento SQS individual
    - _Requirements: 17.2, 17.3, 17.4, 17.5, 17.7_

  - [ ]* 11.2 Write unit tests for Email_Service (pytest + moto)
    - transfer_confirmation con masking; partial batch failure
    - _Requirements: 17.4, 17.7_

- [ ] 12. Implement SMS_Service Lambda (SQS Triggered)
  - [ ] 12.1 Implement SQS-triggered SMS service
    - Create `src/lambdas/sms_service/handler.py` — `BatchProcessor` consumiendo la `sms-notification-queue` **de `infra`**
    - Procesar `transfer_confirmation`: SMS vía Pinpoint con monto + destino enmascarado
    - NOTA: independiente del OTP_Service (que es síncrono en el workflow); este es solo confirmación post-operación
    - _Requirements: 17.2, 17.8_

- [ ] 13. Implement Strands_Agent Lambda (Python 3.13)
  - [ ] 13.1 Implement Strands Agent with tools
    - Create `src/lambdas/ai_agent/handler.py` — recibe `{sessionId, inputText, phoneNumber, correlationId}` del Message_Processor
    - Create `src/lambdas/ai_agent/tools.py` — tools con `@tool`:
      - `query_balance(phone_number, product_type=None)` → invoke `balance-query`
      - `initiate_transfer_breb(...)` → invoke `transfer-breb-initiator` (dispara Step Functions); retorna inmediatamente con executionArn + "Te envié un código OTP por SMS"
      - `generate_statement(phone_number, account_id, cutoff_date)` → invoke `statement-generator`
    - Create `src/lambdas/ai_agent/agent.py` — Strands Agent con Claude Haiku 3.5 (Bedrock), Guardrails de `infra` aplicados (GuardrailId/version del contrato), memoria de sesión por sessionId
    - Create `src/lambdas/ai_agent/prompts.py` — system prompt: reglas, formato COP, español colombiano, disclaimer, instrucción de NO esperar OTP síncronamente
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 11.1, 11.2, 12.1, 12.2, 12.3_

  - [ ]* 13.2 Write unit tests for Strands_Agent (pytest)
    - Routing de tools; transfer retorna inmediato con executionArn; fuera de dominio declina; guardrails bloquean
    - _Requirements: 10.3, 12.2, 12.6_

- [ ] 14. Checkpoint — Todas las Lambdas + tests pasan
  - Ejecutar `pytest`. Ask the user if questions arise.

- [ ] 15. Implement compute, wiring & IAM CloudFormation templates (consumen el contrato de `infra`)
  - [ ] 15.1 Create IAM roles template (`iam.yaml`)
    - Create `cloudformation/templates/connectai/iam.yaml` — IAM roles de TODAS las Lambdas + el rol del state machine
    - **Regla clave del reparto**: los ARNs de los recursos provienen del **contrato** (`Fn::ImportValue` / parámetro SSM), NO de `!GetAtt`/`!Ref` local. Least privilege con ARNs concretos (evitar `Resource: "*"` salvo APIs sin resource-level, p. ej. Transcribe)
    - Roles fuera de VPC (`AWSLambdaBasicExecutionRole`): Webhook_Receiver (`sqs:SendMessage` sobre `InboundQueueArn` + Secrets `TwilioSecretArn`), Message_Processor (SQS consume `InboundQueueArn` + DynamoDB `ConsentTableArn`/`AuthTableArn`/`OtpTableArn` + Lambda Invoke ai-agent local + Step Functions SendTask* local + Transcribe + S3 `AudioTempBucketArn`/`StatementBucketArn` + Secrets), Auth_Service, otp-service, email-service, sms-service, strands-agent (Bedrock InvokeModel/ApplyGuardrail con `GuardrailId`/`BedrockAgentArn` del contrato), transfer-breb-initiator, message-handler-notify
    - Roles en VPC (`AWSLambdaVPCAccessExecutionRole`, dominio bancario): balance-query, transfer-breb-validate/execute (solo Logs), statement-generator (S3 PutObject sobre `StatementBucketArn`)
    - Rol del state machine: Lambda InvokeFunction (tasks locales) + `sqs:SendMessage` sobre `EmailQueueArn`/`SmsQueueArn` del contrato
    - _Requirements: 14.2, 14.3, 15.1_

  - [ ] 15.2 Create ingestion Lambdas template + API Gateway wiring (`lambdas-ingestion.yaml`)
    - Create `cloudformation/templates/connectai/lambdas-ingestion.yaml`
    - **WebhookReceiverFunction**: `Runtime: python3.13`, MemorySize 256, Timeout 10, **SIN VpcConfig**, `Code: {S3Bucket: <artefactos-infra>, S3Key}`, `Layers: [SharedLayer]`. Env: `INBOUND_QUEUE_URL` (del contrato), `TWILIO_SECRET_ARN` (del contrato)
    - **MessageProcessorFunction**: `Runtime: python3.13`, MemorySize 512, Timeout 120, **SIN VpcConfig**. Env: nombres de tablas/buckets del contrato. `AWS::Lambda::EventSourceMapping` usando el **`InboundQueueArn` importado**: `BatchSize: 1`, `FunctionResponseTypes: [ReportBatchItemFailures]`, `ScalingConfig.MaximumConcurrency: 10`
    - **AuthServiceFunction**: `Runtime: python3.13`, 128MB, 10s, **SIN VpcConfig**, Function URL con CORS; usa Auth_Session del contrato
    - **API Gateway wiring** (el HTTP API lo crea `infra`): `AWS::ApiGatewayV2::Integration` (`AWS_PROXY` → ARN de WebhookReceiverFunction) + `AWS::ApiGatewayV2::Route` (`POST /webhook/twilio`) + `AWS::Lambda::Permission` (apigateway), todo usando el **`HttpApiId` importado** del contrato
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.9, 5.3, 14.3, 15.1, 15.2, 15.5, 15.9, 15.10, 16.5, 16.7_

  - [ ] 15.3 Create action group / state-machine task Lambdas template (`lambdas-actions.yaml`)
    - Create `cloudformation/templates/connectai/lambdas-actions.yaml`
    - **EN VPC** (dominio bancario, `python3.13` + Layer; `VpcConfig.SecurityGroupIds: [!Ref BankingLambdaSGId]`, `SubnetIds: !Split [",", !Ref PrivateSubnetIds]` — ambos del contrato):
      - `balance-query` (128MB/15s)
      - `transfer-breb-validate` (128MB/10s)
      - `transfer-breb-execute` (128MB/15s)
      - `statement-generator` (256MB/30s; escribe al Statement_Bucket de `infra` vía Gateway Endpoint S3. NO publica a SQS — el extracto se entrega por WhatsApp)
    - **FUERA de VPC** (`python3.13` + Layer, sin VpcConfig):
      - `transfer-breb-initiator` (128MB/10s; StartExecution sobre la state machine local)
      - `message-handler-notify` (128MB/10s; Secrets Twilio del contrato)
    - **`AWS::Lambda::Permission`** para que `bedrock.amazonaws.com` (con `SourceArn = BedrockAgentArn` del contrato) invoque las Action Group Lambdas (`balance-query`, `transfer-breb-initiator`, `statement-generator`)
    - _Requirements: 12.1, 14.3, 15.1, 15.4, 17.3, 18.7_

  - [ ] 15.4 Create notification Lambdas template (`lambdas-notify.yaml`)
    - Create `cloudformation/templates/connectai/lambdas-notify.yaml`
    - **OtpServiceFunction**: `python3.13`, 128MB, 10s, **SIN VpcConfig**; usa OTP_Store del contrato + Pinpoint. Invocada solo por Step Functions (waitForTaskToken)
    - **EmailServiceFunction**: `python3.13`, 256MB, 30s, **SIN VpcConfig**; `AWS::Lambda::EventSourceMapping` usando el **`EmailQueueArn` importado** (`BatchSize: 10`, `MaximumBatchingWindowInSeconds: 5`, `FunctionResponseTypes: [ReportBatchItemFailures]`)
    - **SmsServiceFunction**: `python3.13`, 128MB, 15s, **SIN VpcConfig**; EventSourceMapping usando el **`SmsQueueArn` importado**
    - **NOTA del reparto**: las colas email/sms (y sus DLQs) las crea `infra`; aquí solo se crean las Lambdas y sus Event Source Mappings sobre los ARN importados
    - _Requirements: 16.1, 16.2, 16.3, 17.2, 17.3, 17.4, 17.8_

  - [ ] 15.5 Create Strands_Agent Lambda template (`lambda-ai-agent.yaml`)
    - Create `cloudformation/templates/connectai/lambda-ai-agent.yaml`
    - **AiAgentFunction**: `python3.13`, 512MB, 60s, **SIN VpcConfig** (solo Bedrock + Lambda invoke); usa `GuardrailId`/`GuardrailVersion` y `BedrockAgentId` del contrato; IAM Bedrock InvokeModel + ApplyGuardrail + Lambda Invoke (tools)
    - **NOTA del reparto**: el Bedrock Agent Core y los Guardrails los crea `infra`; aquí solo se define la Lambda del agente Strands y se referencia el contrato
    - _Requirements: 12.1, 12.2, 12.3, 14.3_

  - [ ] 15.6 Create State Machine template (`state-machine.yaml`)
    - Create `cloudformation/templates/connectai/state-machine.yaml`
    - `AWS::StepFunctions::StateMachine` tipo STANDARD; `DefinitionS3Location` o `DefinitionString` con `DefinitionSubstitutions` (ARNs de Lambdas locales + **URLs/ARNs de las colas email/sms importados** del contrato)
    - Logging CloudWatch `LogLevel: ALL`, retention 90 días; usa el rol definido en `iam.yaml`
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6_

  - [ ] 15.7 Complete root composite stack wiring (`connectai-app.yaml`)
    - Update `cloudformation/stacks/sandbox/connectai-app.yaml` — añadir los `AWS::CloudFormation::Stack` anidados en orden de dependencias: `iam` → `lambdas-actions`/`lambda-ai-agent`/`lambdas-notify` → `state-machine` → `lambdas-ingestion` (API GW wiring + ESM)
    - Pasar a cada nested stack los parámetros del contrato declarados en la Task 2.1 (ARNs/URLs/IDs/SG/subnets) + `LambdaArtifactsBucket` + `LambdaCodeKey`
    - **NOTA**: ya NO hay nested stacks de recursos compartidos (data/storage/queues/secrets/vpc-access/guardrails/api-gateway/observability) — los crea `infra`
    - _Requirements: 15.1, 15.4, 15.5_

  - [ ] 15.8 Create CI/CD workflow (`cfn-deploy.yml`)
    - Create `.github/workflows/cfn-deploy.yml` (mismo patrón que `infra`): OIDC, build Layer + zip Lambdas → **subir los ZIP al bucket de artefactos de `infra`** (nombre resuelto del contrato SSM/Export), `cfn-lint`, sync templates a S3, `aws cloudformation deploy --capabilities CAPABILITY_NAMED_IAM`
    - **Orden de despliegue**: este pipeline corre **DESPUÉS** del pipeline de `infra`; resuelve el contrato (Exports/SSM) y falla rápido si no está publicado
    - _Requirements: 15.4, 15.5_

- [ ] 16. Final checkpoint — validación y deploy
  - `pytest src/tests` (unit + property) en verde
  - `cfn-lint cloudformation/**/*.yaml` y `checkov -d cloudformation/` sin findings críticos **sobre los templates de la app** (la validación de recursos compartidos — S3 BPA, DynamoDB encryption, FIFO, no-NAT, VPC Endpoints — es responsabilidad del pipeline de `infra`)
  - `aws stepfunctions validate-state-machine-definition --definition file://cloudformation/state-machines/transfer-breb.asl.json`
  - **Verificar el contrato cross-stack**: todos los recursos compartidos se resuelven vía parámetros SSM (`AWS::SSM::Parameter::Value<String>`) y/o `Fn::ImportValue` — NO hay ARNs/URLs/IDs hardcodeados en los templates de la app
  - Verificar que todos los `AWS::Lambda::Function` tienen `Runtime: python3.13`; que SOLO las 4 Lambdas bancarias (balance-query, transfer-breb-validate/execute, statement-generator) tienen `VpcConfig`, y que ese `VpcConfig` usa `BankingLambdaSGId` + `PrivateSubnetIds` del contrato (no IDs hardcodeados)
  - Verificar el wiring definido por la app: API Gateway Integration+Route+Permission usando el `HttpApiId` importado; EventSourceMappings (Message_Processor sobre `InboundQueueArn`, Email_Service sobre `EmailQueueArn`, SMS_Service sobre `SmsQueueArn`) con `FunctionResponseTypes: [ReportBatchItemFailures]`; `Lambda::Permission` para que el Bedrock Agent (`BedrockAgentArn`) invoque las Action Group Lambdas
  - **Orden de deploy**: confirmar que `infra` está desplegado y el contrato publicado; luego `aws cloudformation deploy` del stack `connectai-app.yaml` en sandbox; configurar webhook URL del API Gateway (de `infra`) en Twilio Sandbox
  - Smoke test E2E: WhatsApp → 200 de Twilio en <1s → mensaje en SQS → Processor lo procesa → respuesta al cliente
  - Ask the user if questions arise.

## Notes

- Tasks marcadas con `*` son opcionales y pueden saltarse para un MVP más rápido
- Cada task referencia requisitos específicos para trazabilidad
- Los checkpoints aseguran validación incremental
- **Reparto de IaC**: `infra` crea los recursos compartidos (API GW, SQS, S3, DynamoDB, Bedrock, red, Secrets, SNS, observabilidad); `BTG-ConnectAI` define solo cómputo, wiring, IAM y código. El contrato cross-stack (Exports/`Fn::ImportValue` + SSM `/btgconnectai/sandbox/...`) los une. **Orden: `infra` primero, luego la app**
- **Stack 100% Python 3.13** en todas las Lambdas; tests con **pytest** + **hypothesis** (property-based)
- **CloudFormation puro (YAML)** — sin CDK ni SAM; nested stacks + GitHub Actions OIDC, igual que el repo `infra`
- Código Lambda zipeado y subido al **bucket de artefactos de `infra`** + dependencias compartidas en Lambda Layer
- **Estrategia de red híbrida**: solo el dominio bancario (balance-query, transfer-breb-validate/execute, statement-generator) en subnets privadas con VPC Endpoints y cero salida a internet (SG + endpoints son de `infra`, referenciados vía contrato); el resto fuera de VPC. **Sin NAT Gateway**
- Mock data inline en las Action Group Lambdas — sin base de datos para datos bancarios
- Credenciales Twilio en Secrets Manager (de `infra`), nunca hardcodeadas
- **Async Webhook Pattern**: Webhook_Receiver responde 200 en <1s; Message_Processor consume async. Twilio nunca timeoutea
- **SQS FIFO dedup nativa** (cola de `infra`) elimina la tabla Dedup; orden por cliente vía MessageGroupId
- **Step Functions** orquesta transferencias con `waitForTaskToken`; **SQS** desacopla notificaciones fire-and-forget con DLQ
- Para el MVP mock, el happy path es prioritario; los caminos de error del state machine se implementan pero no se prueban exhaustivamente

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["1.3", "1.4"] },
    { "id": 3, "tasks": ["4.1", "4.2", "4.3", "5.1", "5.3", "5.5", "5.6"] },
    { "id": 4, "tasks": ["4.4", "4.5", "5.2", "5.4", "5.7", "5.8", "5.9"] },
    { "id": 5, "tasks": ["5.10", "5.11", "5.12", "6.1", "6.4", "7.1", "7.3", "7.5"] },
    { "id": 6, "tasks": ["6.2", "6.3", "7.2", "7.4", "7.6", "7.7", "8.1", "9.1"] },
    { "id": 7, "tasks": ["8.2", "9.2", "9.3", "10.1", "10.2", "11.1", "12.1"] },
    { "id": 8, "tasks": ["11.2", "13.1"] },
    { "id": 9, "tasks": ["13.2", "15.1", "15.2", "15.3", "15.4", "15.5", "15.6"] },
    { "id": 10, "tasks": ["15.7", "15.8"] }
  ]
}
```
