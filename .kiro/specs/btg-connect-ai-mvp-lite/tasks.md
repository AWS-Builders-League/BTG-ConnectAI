# Implementation Plan: BTG ConnectAI MVP Lite

## Overview

Implementación incremental de un asistente bancario conversacional para WhatsApp usando Twilio (sandbox), Amazon API Gateway, Strands Agent SDK sobre Amazon Bedrock (Claude Haiku 3.5). **Stack 100% Python 3.13** para todas las Lambdas. IaC con **CloudFormation puro (YAML)** siguiendo el patrón del repo `infra`. Las Lambdas corren en subnets privadas de la VPC `IA-Builder-sandbox-networking` con salida a internet via NAT Gateway.

**Patrones arquitectónicos clave:**

- **Async Webhook Pattern**: `Webhook_Receiver` (sync, responde 200 a Twilio en <1s) → SQS FIFO → `Message_Processor` (async). Twilio nunca experimenta timeouts.
- **SQS FIFO** con dedup nativa (`MessageDeduplicationId = MessageSid`) — elimina la tabla Dedup custom — y orden por cliente (`MessageGroupId = phoneNumber`).
- **AWS Step Functions** orquesta la transferencia BRE-B (`TransferBrebStateMachine`) usando `waitForTaskToken` para el callback del OTP — sin bloquear Lambdas.
- **Amazon SQS** desacopla las notificaciones (email, SMS post-operación): productores fire-and-forget, consumidores en batch con DLQ.

**Convenciones de empaquetado (CloudFormation, sin CDK/SAM):**

- Código Lambda en `src/lambdas/<nombre>/handler.py`; se zipea y sube a S3; el template referencia `Code: {S3Bucket, S3Key}`.
- Dependencias compartidas (`twilio`, `aws-lambda-powertools`, `strands-agents`, código `src/shared/`) en un **Lambda Layer** común.
- Tests con **pytest** (unit/integration) y **hypothesis** (property-based).

## Tasks

- [ ] 1. Set up project structure, shared layer, and CloudFormation foundation
  - [ ] 1.1 Initialize project structure
    - Create directory structure: `cloudformation/templates/connectai/`, `cloudformation/stacks/sandbox/`, `cloudformation/state-machines/`, `.github/workflows/`, `src/lambdas/`, `src/shared/`, `src/login-page/`, `src/tests/unit/`, `src/tests/property/`, `src/tests/integration/`
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

- [ ] 2. Implement data, storage, queues & security CloudFormation templates
  - [ ] 2.1 Create DynamoDB tables template
    - Create `cloudformation/templates/connectai/data.yaml`
    - Consent_Store: `pk` (String), PAY_PER_REQUEST, SSE AWS managed
    - Auth_Session: `pk` (String), TTL en `ttl`, PAY_PER_REQUEST, SSE
    - OTP_Store: `pk` (String, phoneNumber), TTL en `ttl` (5 min), PAY_PER_REQUEST, SSE (campos `code`, `taskToken`, `executionArn`, `attempts`, `transferContext`)
    - **NOTA**: NO se crea tabla Dedup — la dedup la maneja SQS FIFO con `MessageDeduplicationId`
    - _Requirements: 1.5, 5.3, 6.1, 14.1, 16.1_

  - [ ] 2.2 Create S3 buckets template
    - Create `cloudformation/templates/connectai/storage.yaml`
    - Audio_Temp_Bucket: lifecycle 1 día, SSE, BlockPublicAccess (4 settings)
    - Statement_Bucket: lifecycle 1 día, SSE, BlockPublicAccess
    - Login_Page_Bucket: static website hosting + bucket policy de lectura pública para assets
    - Bucket para artefactos de deploy (código Lambda zipeado) si no se reusa el de `infra`
    - _Requirements: 9.3, 14.1, 14.7_

  - [ ] 2.3 Create Secrets Manager & SNS alarms template
    - Create `cloudformation/templates/connectai/secrets.yaml`
    - `AWS::SecretsManager::Secret`: twilioAccountSid, twilioAuthToken, twilioWhatsAppNumber, twilioTcTemplateSid, loginPageUrl, authServiceUrl
    - `AWS::SNS::Topic` para alarmas de CloudWatch
    - _Requirements: 14.5, 15.7, 13.4_

  - [ ] 2.4 Create notification queues template (SQS)
    - Create `cloudformation/templates/connectai/queues.yaml` (parte 1)
    - `email-notification-queue` + `email-dlq` (maxReceiveCount=3, visibilityTimeout=60s, retention=4d, receiveWait=20s, SSE-SQS)
    - `sms-notification-queue` + `sms-dlq` (misma config)
    - `AWS::CloudWatch::Alarm`: `ApproximateNumberOfMessagesVisible > 0` en cada DLQ → SNS alarm topic
    - Outputs con URLs y ARNs (Export para nested stacks)
    - _Requirements: 17.1, 17.6_

  - [ ] 2.5 Create Lambda Security Group template (importa networking)
    - Create `cloudformation/templates/connectai/security-group.yaml`
    - Importar `VpcId` vía `Fn::ImportValue: IA-Builder-sandbox-networking-VpcId`
    - `AWS::EC2::SecurityGroup` para Lambdas: sin reglas de ingress, egress TCP 443 a 0.0.0.0/0
    - Las subnets privadas se importan vía `Fn::ImportValue: IA-Builder-sandbox-networking-PrivateSubnetIds` (split con `Fn::Split`)
    - Output: SecurityGroupId + SubnetIds para uso en los templates de Lambdas
    - _Requirements: 15.1, 15.2, 15.7_

  - [ ] 2.6 Create Inbound Messages Queue (SQS FIFO)
    - Create `cloudformation/templates/connectai/queues.yaml` (parte 2) o `inbound-queue.yaml`
    - `AWS::SQS::Queue` `inbound-messages-queue.fifo`:
      - `FifoQueue: true`, `ContentBasedDeduplication: false`, `DeduplicationScope: messageGroup`, `FifoThroughputLimit: perMessageGroupId`
      - `VisibilityTimeout: 130`, `MessageRetentionPeriod: 86400` (1 día), `ReceiveMessageWaitTimeSeconds: 20`, `SqsManagedSseEnabled: true`
    - DLQ `inbound-messages-dlq.fifo` con `RedrivePolicy maxReceiveCount: 3`
    - Alarmas: DLQ `MessagesVisible > 0`; `ApproximateAgeOfOldestMessage > 60s` en la cola activa
    - _Requirements: 3.3, 3.5, 3.9, 15.8_

- [ ] 3. Checkpoint — `cfn-lint` pasa en los templates base
  - `cfn-lint cloudformation/templates/connectai/*.yaml`. Ask the user if questions arise.

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
    - `enqueue_message(payload, correlation_id)` — boto3 `sqs.send_message` con `MessageGroupId=From`, `MessageDeduplicationId=MessageSid`, body JSON con `correlationId` y `receivedAt`
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
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 5.2 Write property test for consent gate (hypothesis)
    - **Property 5: Existing Consent Skips T&C**
    - _Validates: Requirement 1.4_

  - [ ] 5.3 Implement auth session module
    - Create `src/lambdas/message_processor/auth.py`
    - `get_auth_session`, `is_expired`, `store_pending_request`, `send_login_link` (Twilio REST), `derive_session_id` (determinista)
    - _Requirements: 5.1, 5.6, 5.8, 6.1, 6.2, 11.1_

  - [ ]* 5.4 Write property tests for auth and session (hypothesis)
    - **Property 3: Session ID Determinism**, **Property 6: No Session → Login**, **Property 7: Active Session → Proceed**
    - _Validates: Requirements 11.1, 5.1, 5.8, 6.1_

  - [ ] 5.5 Implement audio transcription module
    - Create `src/lambdas/message_processor/transcription.py`
    - `transcribe_audio(twilio_media_url, phone_number)` — descarga audio de Twilio Media URL (Basic Auth con creds Twilio), sube a S3 temp, `transcribe.start_transcription_job` (es-CO, OGG), polling (max 30s), cleanup
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
    - Si hay registro activo en `OTP_Store`, este flujo tiene prioridad sobre el Strands Agent
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
    - Logic: validar callback token → buscar user por username+password → verificar phone → `put_item` Auth_Session (TTL 30min) → success/failure
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
    - Logic: validar fecha (pasada) → datos cliente → generar PDF → `put_object` S3 → **publicar `statement_delivery` a `email-notification-queue`** (fire-and-forget) → retornar `{s3Bucket, s3Key, fileName}`
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
    - Genera código 6 dígitos, `put_item` en `OTP_Store` `{pk, code, taskToken, executionArn, attempts: 0, transferContext, ttl: now+300}`
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
    - Estados: `ValidateTransfer` → `GenerateOTP` (`waitForTaskToken`, HeartbeatSeconds=300) → `ValidateOTP` (Choice) → `ExecuteTransfer` → `PublishNotifications` (Parallel: SQS sendMessage a email + sms) → `NotifyUserSuccess`
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
    - Create `src/lambdas/email_service/handler.py` — `process_partial_response` con `BatchProcessor(EventType.SQS)` sobre `email-notification-queue`
    - Por mensaje, rutear por `type`:
      - `transfer_confirmation` → `send_transfer_confirmation(to, receipt)` vía SES `send_email` (HTML template)
      - `statement_delivery` → `send_statement_email(...)` — descarga PDF de S3, `send_raw_email` (MIME con adjunto)
    - Masking de campos sensibles antes de incluirlos
    - Excepción por mensaje → reintento SQS individual
    - _Requirements: 17.2, 17.3, 17.4, 17.5, 17.7_

  - [ ]* 11.2 Write unit tests for Email_Service (pytest + moto)
    - transfer_confirmation con masking; statement adjunta PDF vía send_raw_email; partial batch failure
    - _Requirements: 17.4, 17.7_

- [ ] 12. Implement SMS_Service Lambda (SQS Triggered)
  - [ ] 12.1 Implement SQS-triggered SMS service
    - Create `src/lambdas/sms_service/handler.py` — `BatchProcessor` sobre `sms-notification-queue`
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
    - Create `src/lambdas/ai_agent/agent.py` — Strands Agent con Claude Haiku 3.5 (Bedrock), Guardrails aplicados, memoria de sesión por sessionId
    - Create `src/lambdas/ai_agent/prompts.py` — system prompt: reglas, formato COP, español colombiano, disclaimer, instrucción de NO esperar OTP síncronamente
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 11.1, 11.2, 12.1, 12.2, 12.3_

  - [ ]* 13.2 Write unit tests for Strands_Agent (pytest)
    - Routing de tools; transfer retorna inmediato con executionArn; fuera de dominio declina; guardrails bloquean
    - _Requirements: 10.3, 12.2, 12.6_

- [ ] 14. Checkpoint — Todas las Lambdas + tests pasan
  - Ejecutar `pytest`. Ask the user if questions arise.

- [ ] 15. Implement compute & wiring CloudFormation templates
  - [ ] 15.1 Create ingestion Lambdas template (Webhook_Receiver + Message_Processor)
    - Create `cloudformation/templates/connectai/lambdas-ingestion.yaml`
    - **WebhookReceiverFunction**: `Runtime: python3.13`, MemorySize 256, Timeout 10, `VpcConfig` (subnets privadas + SG importados), `Code: {S3Bucket, S3Key}`, `Layers: [SharedLayer]`. IAM: `sqs:SendMessage` (inbound) + `secretsmanager:GetSecretValue` (Twilio). Env: `INBOUND_QUEUE_URL`, `TWILIO_SECRET_ARN`
    - **MessageProcessorFunction**: `Runtime: python3.13`, MemorySize 512, Timeout 120, VpcConfig. IAM: SQS consume (inbound) + DynamoDB (consent/auth/otp) + Lambda Invoke (ai-agent) + Step Functions SendTask* + Transcribe + S3 + Secrets
    - `AWS::Lambda::EventSourceMapping` sobre `inbound-messages-queue.fifo`: `BatchSize: 1`, `FunctionResponseTypes: [ReportBatchItemFailures]`, `ScalingConfig.MaximumConcurrency: 10`
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.9, 15.1, 15.2, 15.9, 15.10, 16.5, 16.7_

  - [ ] 15.2 Create Auth_Service Lambda template
    - Create `cloudformation/templates/connectai/lambda-auth.yaml`
    - `Runtime: python3.13`, 128MB, 10s, VpcConfig, Function URL con CORS; IAM: DynamoDB Auth_Session PutItem
    - _Requirements: 5.3, 14.3, 15.1_

  - [ ] 15.3 Create action group / state-machine task Lambdas template
    - Create `cloudformation/templates/connectai/lambdas-actions.yaml`
    - `balance-query` (128MB/15s, IAM solo Logs), `transfer-breb-initiator` (128MB/10s, IAM Step Functions StartExecution), `transfer-breb-validate` (128MB/10s, solo Logs), `transfer-breb-execute` (128MB/15s, solo Logs), `statement-generator` (256MB/30s, IAM S3 PutObject + SQS SendMessage email), `message-handler-notify` (128MB/10s, IAM Secrets Twilio). Todas `python3.13` + VpcConfig + Layer
    - _Requirements: 14.3, 15.1, 17.3, 18.7_

  - [ ] 15.4 Create OTP_Service Lambda template
    - Create `cloudformation/templates/connectai/lambda-otp.yaml`
    - `python3.13`, 128MB, 10s, VpcConfig; IAM: DynamoDB OTP_Store PutItem + Pinpoint SendMessages. Invocada solo por Step Functions (waitForTaskToken)
    - _Requirements: 16.1, 16.2, 16.3_

  - [ ] 15.5 Create Email_Service & SMS_Service Lambda templates (SQS triggered)
    - Create `cloudformation/templates/connectai/lambdas-notify.yaml`
    - **EmailService**: `python3.13`, 256MB, 30s, VpcConfig; EventSourceMapping sobre `email-notification-queue` (`BatchSize: 10`, `MaximumBatchingWindowInSeconds: 5`, `FunctionResponseTypes: [ReportBatchItemFailures]`); IAM: SES Send*, S3 GetObject, SQS consume
    - **SmsService**: `python3.13`, 128MB, 15s, VpcConfig; EventSourceMapping sobre `sms-notification-queue`; IAM: Pinpoint SendMessages, SQS consume
    - _Requirements: 17.2, 17.3, 17.4, 17.8_

  - [ ] 15.6 Create Strands_Agent Lambda + Guardrails template
    - Create `cloudformation/templates/connectai/lambda-ai-agent.yaml` y `guardrails.yaml`
    - **AiAgentFunction**: `python3.13`, 512MB, 60s, VpcConfig; IAM: Bedrock InvokeModel + ApplyGuardrail + Lambda Invoke (tools)
    - `AWS::Bedrock::Guardrail`: content filtering (HATE, VIOLENCE, MISCONDUCT, PROMPT_ATTACK), topic policies (investment-advice DENY, non-banking DENY, competitor-info DENY), blocked messages en español
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 14.3_

  - [ ] 15.7 Create State Machine template
    - Create `cloudformation/templates/connectai/state-machine.yaml`
    - `AWS::StepFunctions::StateMachine` tipo STANDARD; `DefinitionS3Location` o `DefinitionString` con `DefinitionSubstitutions` (ARNs de Lambdas, URLs de colas)
    - Logging CloudWatch `LogLevel: ALL`, retention 90 días; IAM role: Lambda Invoke (tasks) + SQS SendMessage (colas)
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6_

  - [ ] 15.8 Create API Gateway template
    - Create `cloudformation/templates/connectai/api-gateway.yaml`
    - `AWS::ApiGatewayV2::Api` (HTTP API) + `AWS::ApiGatewayV2::Route` `POST /webhook/twilio` → integración Lambda proxy con WebhookReceiverFunction + `AWS::Lambda::Permission`
    - _Requirements: 3.1, 15.5_

  - [ ] 15.9 Create root composite stack
    - Create `cloudformation/stacks/sandbox/connectai.yaml` — `AWS::CloudFormation::Stack` anidados en orden de dependencias: security-group → data/storage/queues/secrets → guardrails → lambdas-actions/otp/ai-agent → state-machine → lambdas-ingestion/notify → api-gateway → observability
    - Parámetros: `ProjectName=BTGConnectAI`, `Environment=sandbox`, `TemplatesBucket`, `LambdaArtifactsBucket`, `LambdaCodeKey` (git-sha)
    - Importa networking vía `Fn::ImportValue` (VpcId, PrivateSubnetIds)
    - _Requirements: 15.1, 15.4, 15.5_

  - [ ] 15.10 Create CI/CD workflow
    - Create `.github/workflows/cfn-deploy.yml` (mismo patrón que `infra`): OIDC, build Layer + zip Lambdas → S3, `cfn-lint`, sync templates a S3, `aws cloudformation deploy --capabilities CAPABILITY_NAMED_IAM`
    - _Requirements: 15.4_

- [ ] 16. Implement Observability template
  - [ ] 16.1 Create observability template
    - Create `cloudformation/templates/connectai/observability.yaml`
    - `AWS::CloudWatch::Dashboard`: invocations/errors/latency p50/p90 por Lambda (Webhook_Receiver, Message_Processor, Auth_Service, Strands_Agent, OTP_Service, Email_Service, SMS_Service, balance-query, transfer-breb-validate, transfer-breb-execute, statement-generator, message-handler-notify); widget de Webhook_Receiver p99 con threshold 1000ms; métricas Step Functions y SQS
    - `AWS::CloudWatch::Alarm`:
      - Webhook_Receiver latency p99 > 1000ms (rompe SLO async)
      - error rate > 10% en 5min por Lambda (math expression)
      - `ExecutionsFailed > 5` en 5min sobre TransferBrebStateMachine
      - DLQ `MessagesVisible > 0` (inbound-messages-dlq, email-dlq, sms-dlq)
      - `ApproximateAgeOfOldestMessage > 60s` en inbound-queue
    - `AWS::Logs::LogGroup` retention: 7 días Lambdas, 90 días state machine
    - _Requirements: 3.3, 3.9, 13.1, 13.3, 13.4, 17.6, 18.5, 18.6_

- [ ] 17. Final checkpoint — validación y deploy
  - `pytest src/tests` (unit + property) en verde
  - `cfn-lint cloudformation/**/*.yaml` y `checkov -d cloudformation/` sin findings críticos
  - `aws stepfunctions validate-state-machine-definition --definition file://cloudformation/state-machines/transfer-breb.asl.json`
  - Verificar que todos los `AWS::Lambda::Function` tienen `Runtime: python3.13` y `VpcConfig` con subnets privadas IA-Builder
  - Verificar que `inbound-messages-queue.fifo` existe con dedup por `MessageDeduplicationId`; NO existe tabla DynamoDB Dedup
  - Verificar EventSourceMappings (Message_Processor, Email_Service, SMS_Service) con `FunctionResponseTypes: [ReportBatchItemFailures]`
  - `aws cloudformation deploy` del stack raíz en sandbox; configurar webhook URL del API Gateway en Twilio Sandbox
  - Smoke test E2E: WhatsApp → 200 de Twilio en <1s → mensaje en SQS → Processor lo procesa → respuesta al cliente
  - Ask the user if questions arise.

## Notes

- Tasks marcadas con `*` son opcionales y pueden saltarse para un MVP más rápido
- Cada task referencia requisitos específicos para trazabilidad
- Los checkpoints aseguran validación incremental
- **Stack 100% Python 3.13** en todas las Lambdas; tests con **pytest** + **hypothesis** (property-based)
- **CloudFormation puro (YAML)** — sin CDK ni SAM; nested stacks + GitHub Actions OIDC, igual que el repo `infra`
- Código Lambda zipeado a S3 + dependencias compartidas en Lambda Layer
- Todas las Lambdas en subnets privadas (10.0.11.0/24, 10.0.12.0/24) via NAT Gateway
- Mock data inline en las Action Group Lambdas — sin base de datos para datos bancarios
- Credenciales Twilio en Secrets Manager, nunca hardcodeadas
- **Async Webhook Pattern**: Webhook_Receiver responde 200 en <1s; Message_Processor consume async. Twilio nunca timeoutea
- **SQS FIFO dedup nativa** elimina la tabla Dedup; orden por cliente vía MessageGroupId
- **Step Functions** orquesta transferencias con `waitForTaskToken`; **SQS** desacopla notificaciones fire-and-forget con DLQ
- Para el MVP mock, el happy path es prioritario; los caminos de error del state machine se implementan pero no se prueban exhaustivamente

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6"] },
    { "id": 3, "tasks": ["4.1", "4.2", "4.3", "5.1", "5.3", "5.5", "5.6"] },
    { "id": 4, "tasks": ["4.4", "4.5", "5.2", "5.4", "5.7", "5.8", "5.9"] },
    { "id": 5, "tasks": ["5.10", "5.11", "5.12", "6.1", "6.4", "7.1", "7.3", "7.5"] },
    { "id": 6, "tasks": ["6.2", "6.3", "7.2", "7.4", "7.6", "7.7", "8.1", "9.1"] },
    { "id": 7, "tasks": ["8.2", "9.2", "9.3", "10.1", "10.2", "11.1", "12.1"] },
    { "id": 8, "tasks": ["11.2", "13.1"] },
    { "id": 9, "tasks": ["13.2", "15.1", "15.2", "15.3", "15.4", "15.5", "15.6", "15.7", "15.8"] },
    { "id": 10, "tasks": ["15.9", "15.10", "16.1"] }
  ]
}
```
