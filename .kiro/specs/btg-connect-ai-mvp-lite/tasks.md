# Implementation Plan: BTG ConnectAI MVP Lite

## Overview

Implementación incremental de un asistente bancario conversacional para WhatsApp usando Twilio (sandbox), Amazon API Gateway, Strands Agent SDK (Python 3.12) sobre Amazon Bedrock (Claude Haiku 3.5), y funciones Lambda Node.js 24.x para negocio. Las Lambdas corren en subnets privadas de la VPC `IA-Builder-sandbox-networking` con salida a internet via NAT Gateway.

**Patrones arquitectónicos clave:**

- **AWS Step Functions** orquesta la transferencia BRE-B como state machine (`TransferBrebStateMachine`) usando `waitForTaskToken` para el callback del OTP — esto resuelve la espera asíncrona del input del usuario sin bloquear Lambdas.
- **Amazon SQS** desacopla las notificaciones (email, SMS post-operación) del flujo principal: productores publican eventos fire-and-forget, consumidores procesan en batch con reintentos automáticos y DLQ.

## Tasks

- [ ] 1. Set up project structure, shared utilities, and CDK foundation
  - [ ] 1.1 Initialize project structure with TypeScript and Python configuration
    - Create directory structure: `infra/bin/`, `infra/lib/stacks/`, `infra/lib/constructs/`, `infra/lib/state-machines/`, `infra/lib/config/`, `src/lambdas/`, `src/shared/`, `src/login-page/`, `src/tests/unit/`, `src/tests/property/`
    - Initialize `package.json` with dependencies: aws-cdk-lib, constructs, @aws-sdk/client-dynamodb, @aws-sdk/client-s3, @aws-sdk/client-transcribe, @aws-sdk/client-pinpoint, @aws-sdk/client-ses, @aws-sdk/client-sqs, @aws-sdk/client-sfn, @aws-sdk/client-lambda, @aws-lambda-powertools/logger, @aws-lambda-powertools/metrics, @aws-lambda-powertools/batch, twilio, uuid, pdfkit
    - Configure `tsconfig.json` for Node.js 24.x with strict mode
    - Configure `cdk.json` with app entry point
    - Add vitest as test framework with `vitest.config.ts`
    - Add fast-check for property-based testing
    - Create `src/lambdas/ai-agent/requirements.txt` with Python dependencies: strands-agents, boto3, aws-lambda-powertools
    - _Requirements: 15.4, 15.5_

  - [ ] 1.2 Implement shared utilities (logger, masking, constants, types)
    - Create `src/shared/logger.ts` — Lambda Powertools logger configuration with service name and structured JSON output
    - Create `src/shared/masking.ts` — Data masking for phone numbers (last 4), account numbers (last 4), document IDs (last 4)
    - Create `src/shared/constants.ts` — Shared constants (MAX_TWILIO_MESSAGE_LENGTH=1600, AUTH_SESSION_TTL=1800, DEDUP_TTL=600, OTP_TTL=300, TC_VERSION="1.0")
    - Create `src/shared/types.ts` — Shared TypeScript interfaces: TwilioWebhookPayload, ConsentRecord, AuthSession, OTPRecord, ActionGroupRequest, ActionGroupResponse, MockClient, MockProduct, MockTransaction
    - Create `src/shared/formatting.ts` — COP currency formatting ($X.XXX.XXX,YY)
    - _Requirements: 13.1, 14.4, 10.5_

  - [ ]* 1.3 Write property tests for shared utilities
    - **Property 4: Data Masking Correctness** — For any string ≥ 4 chars, masking retains only last 4 visible
    - **Property 15: COP Currency Formatting** — For any non-negative number, produces $X.XXX.XXX,YY pattern
    - _Validates: Requirements 14.4, 10.5_

  - [ ]* 1.4 Write unit tests for shared utilities
    - Test masking edge cases (strings < 4 chars, empty strings)
    - Test COP formatting with 0, integers, large numbers, decimals
    - _Requirements: 14.4, 10.5_

- [ ] 2. Implement CDK infrastructure stack (DynamoDB, S3, Secrets)
  - [ ] 2.1 Create DynamoDB tables construct
    - Create `infra/lib/constructs/dynamodb-tables.ts`
    - Consent_Store table: pk (String), PAY_PER_REQUEST, AWS managed encryption
    - Auth_Session table: pk (String), TTL on `ttl`, PAY_PER_REQUEST, AWS managed encryption
    - OTP_Store table: pk (String, phoneNumber), TTL on `ttl` (5 min), PAY_PER_REQUEST, AWS managed encryption (incluye campos `code`, `taskToken`, `executionArn`, `attempts`, `transferContext`)
    - **NOTA**: la tabla Dedup custom NO se crea — la dedup de mensajes entrantes la maneja SQS FIFO con `MessageDeduplicationId`
    - _Requirements: 1.5, 5.3, 6.1, 14.1, 16.1_

  - [ ] 2.2 Create S3 buckets construct
    - Audio_Temp_Bucket: 1-day lifecycle rule, AWS managed encryption, block public access
    - Statement_Bucket: 1-day lifecycle rule, AWS managed encryption, block public access
    - Login_Page_Bucket: static website hosting, public read for assets
    - _Requirements: 9.3, 14.1, 14.7_

  - [ ] 2.3 Create Secrets Manager and SNS alarms construct
    - Create `infra/lib/constructs/security.ts`
    - Secrets Manager secret: twilioAccountSid, twilioAuthToken, twilioWhatsAppNumber, loginPageUrl, authServiceUrl
    - SNS topic for CloudWatch alarms only (no message routing)
    - _Requirements: 14.5, 15.7, 13.4_

  - [ ] 2.4 Create SQS notification queues construct
    - Create `infra/lib/constructs/notification-queues.ts`
    - `email-notification-queue` + `email-dlq` (maxReceiveCount=3, visibilityTimeout=60s, retention=4d, receiveWait=20s, encryption=SQS_MANAGED)
    - `sms-notification-queue` + `sms-dlq` (mismas configuraciones)
    - CloudWatch alarm: `ApproximateNumberOfMessagesVisible > 0` en cada DLQ → SNS alarm topic
    - Export queue URLs y ARNs para uso en otros constructs
    - _Requirements: 17.1, 17.6_

  - [ ] 2.5 Create VPC and Security Group construct
    - Create `infra/lib/constructs/vpc-config.ts`
    - Import VPC via `ec2.Vpc.fromLookup` or `Fn.importValue('IA-Builder-sandbox-networking-VpcId')`
    - Import private subnet IDs via `Fn.importValue('IA-Builder-sandbox-networking-PrivateSubnetIds')`
    - Create Lambda Security Group: no inbound rules, outbound TCP 443 to 0.0.0.0/0
    - Export `vpcConfig` object (vpc, subnets, securityGroups) for use in all Lambda constructs
    - _Requirements: 15.1, 15.2_

  - [ ] 2.6 Create Inbound Messages Queue construct (SQS FIFO)
    - Create `infra/lib/constructs/inbound-messages-queue.ts`
    - SQS FIFO queue `inbound-messages-queue.fifo`:
      - `contentBasedDeduplication: false` (dedup explícita por `MessageDeduplicationId`)
      - `deduplicationScope: messageGroup` (dedup independiente por phoneNumber)
      - `fifoThroughputLimit: perMessageGroupId` (mayor throughput)
      - `visibilityTimeout: 130s` (apenas más que el timeout del Processor)
      - `messageRetentionPeriod: 1 día`
      - `receiveWaitTime: 20s` (long polling)
      - `encryption: SQS_MANAGED`
    - DLQ `inbound-messages-dlq.fifo` con `maxReceiveCount: 3`
    - CloudWatch alarm: `ApproximateNumberOfMessagesVisible > 0` en DLQ → SNS alarm topic
    - CloudWatch alarm: `ApproximateAgeOfOldestMessage > 60s` en la cola activa → indica Processor saturado
    - Export queue URL y ARN
    - _Requirements: 3.3, 3.5, 3.9, 15.8_

- [ ] 3. Checkpoint — Ensure infrastructure constructs compile
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement Webhook_Receiver Lambda (sync, behind API Gateway)
  - [ ] 4.1 Implement Twilio signature validation
    - Create `src/lambdas/webhook-receiver/twilio-signature.ts`
    - `validateTwilioSignature(authToken, signature, url, params)` — usa `twilio.validateRequest` para verificar el header `X-Twilio-Signature`
    - Si la firma no coincide, el handler debe responder 403 sin encolar
    - _Requirements: 3.2_

  - [ ] 4.2 Implement form-urlencoded parser
    - Create `src/lambdas/webhook-receiver/parser.ts`
    - `parseFormUrlencoded(body, isBase64)` — Decodifica el body (Twilio envía `application/x-www-form-urlencoded`)
    - Extrae todos los campos relevantes: `MessageSid`, `From`, `To`, `Body`, `NumMedia`, `MediaUrl0`, `MediaContentType0`, `ButtonPayload`, `ProfileName`
    - _Requirements: 3.1_

  - [ ] 4.3 Implement SQS enqueue logic
    - Create `src/lambdas/webhook-receiver/enqueue.ts`
    - `enqueueMessage(payload, correlationId)` — SendMessage a `inbound-messages-queue.fifo` con:
      - `MessageGroupId: payload.From` (orden por cliente)
      - `MessageDeduplicationId: payload.MessageSid` (dedup gratis)
      - `MessageBody`: JSON.stringify({...payload, correlationId, receivedAt: ISO 8601})
    - _Requirements: 3.3, 3.4_

  - [ ] 4.4 Implement Webhook_Receiver main handler
    - Create `src/lambdas/webhook-receiver/index.ts`
    - Handler signature: `APIGatewayProxyHandlerV2`
    - Generar `correlationId` (UUID v4) ANTES de cualquier operación, propagarlo en logger
    - Pipeline: validar firma Twilio → parsear body → enqueue a SQS → return 200 OK
    - Si validación de firma falla → 403; si SQS falla → 5xx (Twilio reintentará y SQS FIFO descartará el duplicado)
    - Target latency: <1s en p99
    - _Requirements: 3.1, 3.2, 3.3, 13.2_

  - [ ]* 4.5 Write unit tests for Webhook_Receiver
    - Valid Twilio signature is accepted, invalid returns 403
    - Form-urlencoded parser handles all field types correctly
    - SQS SendMessage called with correct MessageGroupId and DeduplicationId
    - 200 response is returned in <1s (synthetic latency test)
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 5. Implement Message_Processor Lambda (async, SQS-triggered)
  - [ ] 5.1 Implement consent flow module
    - Create `src/lambdas/message-processor/consent.ts`
    - `getConsent(phoneNumber)` — GetItem from Consent_Store
    - `storeConsent(phoneNumber, status)` — PutItem with timestamp and tcVersion
    - `handleConsentFlow(payload, consent, phoneNumber)` — ButtonPayload='accept_tc'/'reject_tc' handling; first message sends T&C via Twilio REST API
    - `sendTermsAndConditionsMessage(phoneNumber)` — Mensaje con botones accept/reject via Twilio Content Templates
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 5.2 Write property test for consent gate
    - **Property 5: Existing Consent Skips T&C** — Phone with accepted consent returns accepted=true
    - _Validates: Requirement 1.4_

  - [ ] 5.3 Implement auth session module
    - Create `src/lambdas/message-processor/auth.ts`
    - `getAuthSession(phoneNumber)` — GetItem from Auth_Session table
    - `isExpired(session)` — TTL check
    - `storePendingRequest(phoneNumber, inputText)` — Store original request for post-auth processing
    - `sendLoginLink(phoneNumber)` — Generate callback token, build Login_Page URL, send via Twilio REST API
    - `deriveSessionId(phoneNumber)` — Deterministic session ID for Strands Agent memory
    - _Requirements: 5.1, 5.6, 5.8, 6.1, 6.2, 11.1_

  - [ ]* 5.4 Write property tests for auth and session
    - **Property 3: Session ID Determinism** — Same phone always produces same session ID
    - **Property 6: No Session Triggers Login** — No active session triggers login flow
    - **Property 7: Active Session Allows Actions** — Active session with future TTL allows actions
    - _Validates: Requirements 11.1, 5.1, 5.8, 6.1_

  - [ ] 5.5 Implement audio transcription module
    - Create `src/lambdas/message-processor/transcription.ts`
    - `transcribeAudio(twilioMediaUrl, phoneNumber)` — Download audio from Twilio Media URL (with Basic Auth using Twilio credentials), upload to S3 temp, start Transcribe job (es-CO, OGG), poll result (max 30s — sin presión de tiempo gracias al async), cleanup
    - _Requirements: 2.2, 2.3, 2.6_

  - [ ] 5.6 Implement Twilio messaging module
    - Create `src/lambdas/message-processor/messaging.ts`
    - `sendTwilioMessage(to, body)` — Send text message via Twilio REST API. Split si > 1600 chars
    - `splitMessage(text, maxLength)` — Split at newlines/spaces, never exceed maxLength
    - `sendWelcomeMessage(phoneNumber)` — Welcome message listando servicios disponibles
    - `sendTwilioDocument(phoneNumber, s3Bucket, s3Key)` — Genera presigned URL (5min) y envía mensaje con `mediaUrl` vía Twilio
    - _Requirements: 3.7, 3.10, 4.1, 4.2, 9.4_

  - [ ]* 5.7 Write property test for message splitting
    - **Property 1: Message Splitting Round-Trip** — Split + join produces original, every chunk ≤ 1600 chars
    - _Validates: Requirement 3.10_

  - [ ] 5.8 Implement OTP callback handler (priority routing)
    - Create `src/lambdas/message-processor/otp-callback.ts`
    - Cuando hay registro activo en `OTP_Store` para el phoneNumber, ese flujo toma prioridad sobre el del Strands Agent
    - Ver Task 9 para implementación detallada
    - _Requirements: 16.4, 16.5, 16.6, 16.7_

  - [ ] 5.9 Implement Message_Processor main handler
    - Create `src/lambdas/message-processor/index.ts`
    - Handler signature: `SQSHandler` con `SQSBatchResponse` para `reportBatchItemFailures`
    - Pipeline por mensaje del batch (siempre 1 por config):
      - Parsear `record.body` → `TwilioWebhookPayload` con `correlationId`
      - Setear logger correlationId desde el mensaje (NO regenerar — viene del Webhook_Receiver)
      - OTP callback check (si hay OTP pendiente, manejar y continuar)
      - Consent check → handle si falta
      - Determinar tipo (text/audio/button/unsupported)
      - Auth check → enviar login link si falta sesión
      - Invocar Strands_Agent
      - Si la respuesta incluye S3 key de PDF → `sendTwilioDocument`
      - Enviar respuesta de texto vía Twilio
    - Errores: push a `batchItemFailures` para reintento SQS sin afectar otros mensajes
    - _Requirements: 2.1, 2.4, 2.5, 3.5, 3.6, 3.9, 5.1, 13.1, 13.2_

  - [ ]* 5.10 Write property test for unsupported message format
    - **Property 16: Unsupported Format Rejection** — Mensajes que no son text/audio/button retornan unsupported error
    - _Validates: Requirement 2.5_

  - [ ]* 5.11 Write unit tests for Message_Processor modules
    - Test consent flow: first message sends T&C, accept_tc stores consent
    - Test auth: expired session, valid session, no session triggers login link
    - Test message routing: text, audio (NumMedia>0), button (ButtonPayload), unsupported
    - Test OTP callback priority: cuando hay OTP pendiente, NO se invoca al Strands Agent
    - Test partial batch failure: solo el mensaje fallido se reporta a SQS
    - _Requirements: 1.1, 1.2, 1.3, 2.5, 3.5, 5.1, 6.5, 16.4_

  - [ ] 5.12 Checkpoint — Ensure Webhook_Receiver + Message_Processor tests pass
    - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement Auth_Service Lambda and Login_Page
  - [ ] 6.1 Implement Auth_Service Lambda
    - Create `src/lambdas/auth-service/index.ts` — Handler for POST /authenticate (API Gateway Function URL)
    - Create `src/lambdas/auth-service/users.ts` — Hardcoded test users (carlos.rodriguez, maria.lopez, juan.garcia) with credentials, phoneNumber, name, documentId
    - Create `src/lambdas/auth-service/types.ts` — AuthenticateRequest, AuthenticateResponse interfaces
    - Logic: validate callback token → find user by username+password → verify phone matches → PutItem Auth_Session (TTL 30min) → return success/failure
    - Add CORS headers for Login_Page origin
    - _Requirements: 5.2, 5.3, 5.5, 5.7, 6.1_

  - [ ]* 6.2 Write property tests for Auth_Service
    - **Property 8: Invalid Credentials Rejection** — Wrong username/password returns success=false, no session created
    - _Validates: Requirement 5.5_

  - [ ]* 6.3 Write unit tests for Auth_Service
    - Valid credentials create session with correct TTL
    - Invalid username returns error; valid credentials wrong phone returns error
    - Invalid callback token returns error
    - _Requirements: 5.3, 5.5, 5.7_

  - [ ] 6.4 Implement Login_Page (S3 static site)
    - Create `src/login-page/index.html` — Login form, BTG Pactual branding, responsive
    - Create `src/login-page/styles.css` — BTG colors, mobile-first
    - Create `src/login-page/app.js` — Extract phone/token from URL params, POST to Auth_Service, show success/error
    - _Requirements: 5.2_

- [ ] 7. Implement Action Group Lambdas (Node.js 24)
  - [ ] 7.1 Implement balance-query Lambda
    - Create `src/lambdas/balance-query/index.ts` — Handler receiving JSON from Strands Agent (via Lambda invoke), routing to getBalance
    - Create `src/lambdas/balance-query/mock-data.ts` — Mock_Core data: 3 clients with fondos de inversión + cuentas corrientes
    - Create `src/lambdas/balance-query/types.ts` — BalanceRequest, BalanceResponse, ProductBalance
    - Logic: find client by phoneNumber → filter by productType if given → return all if no filter → 404 if not found
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 7.2 Write property tests for balance-query
    - **Property 9: Balance Query Correctness** — Existing client returns all products with correct fields
    - **Property 10: Unknown Client Error** — Non-existent phone returns error
    - _Validates: Requirements 7.1-7.4_

  - [ ] 7.3 Implement transfer-breb Lambdas (split en 3: initiator, validator, executor)
    - Create `src/lambdas/transfer-breb-initiator/index.ts` — Tool del Strands Agent. Recibe los datos de la transferencia, invoca `StartExecution` sobre `TransferBrebStateMachine` con `name = correlationId` (idempotencia), retorna `{executionArn, status: "STARTED"}` al agent inmediatamente
    - Create `src/lambdas/transfer-breb-validate/index.ts` — Estado `ValidateTransfer` del state machine. Recibe params, valida contra Mock_Core (cuenta origen existe + pertenece al cliente + saldo suficiente + cuenta destino existe). Throw `InsufficientFundsError` o `InvalidDestinationError` según corresponda. Sin estos errores, retorna `{valid: true, sourceAccount, destAccount, availableBalance}`
    - Create `src/lambdas/transfer-breb-execute/index.ts` — Estado `ExecuteTransfer` del state machine. Actualiza saldos en Mock_Core, genera comprobante `TransferReceipt` con transactionId único, retorna `{receipt}` para que el state machine lo propague al estado `PublishNotifications`
    - Create `src/lambdas/transfer-breb-shared/mock-data.ts` — Mock_Core compartido por validator y executor
    - Create `src/lambdas/transfer-breb-shared/types.ts` — TransferRequest, TransferReceipt, errores custom (`InsufficientFundsError extends Error`)
    - _Requirements: 8.3, 8.4, 8.10, 8.11, 8.12, 18.7_

  - [ ]* 7.4 Write property tests for transfer-breb validate/execute
    - **Property 11: Valid Transfer Produces Receipt** — Valid params produce receipt with all required fields
    - **Property 12: Insufficient Funds Rejection** — amount > availableBalance throws `InsufficientFundsError`, balance unchanged
    - **Property 19: Idempotent StartExecution** — Same `correlationId` invoked twice creates only one execution (StateMachine `name` collision rejected by AWS)
    - _Validates: Requirements 8.3, 8.10, 18.7_

  - [ ] 7.5 Implement statement-generator Lambda
    - Create `src/lambdas/statement-generator/index.ts` — Handler from Strands Agent
    - Create `src/lambdas/statement-generator/pdf-generator.ts` — PDF with pdfkit (client name, masked account, period, transactions, final balance)
    - Create `src/lambdas/statement-generator/mock-data.ts`
    - Create `src/lambdas/statement-generator/types.ts`
    - Logic: validate cutoff date (past) → get client data → generate PDF → PutObject S3 → **publicar evento `statement_delivery` a `email-notification-queue`** vía SQS SendMessage (fire-and-forget) → retornar `{s3Bucket, s3Key, fileName}` al Strands Agent para envío inmediato por WhatsApp
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 17.3_

  - [ ]* 7.6 Write property tests for statement-generator
    - **Property 13: Future Date Rejection** — Today or future date returns error
    - **Property 14: Valid Statement Returns S3 Reference** — Past date + existing client produces {s3Bucket, s3Key, fileName}
    - _Validates: Requirements 9.2, 9.3, 14.7_

  - [ ]* 7.7 Write unit tests for Action Group Lambdas
    - balance-query: all products, filtered, client not found
    - transfer-breb: valid, insufficient funds, invalid destination
    - statement-generator: valid, future date, empty transactions
    - _Requirements: 7.1-7.4, 8.4, 8.7, 8.8, 9.2, 9.3_

- [ ] 8. Implement OTP_Service Lambda (Task Token Pattern)
  - [ ] 8.1 Implement OTP generate-and-wait
    - Create `src/lambdas/otp-service/index.ts` — Handler invocado por Step Functions con `arn:aws:states:::lambda:invoke.waitForTaskToken`
    - Recibe payload `{operation: "generate-and-wait", phoneNumber, transferAmount, destinationAccount, taskToken}`
    - Generate 6-digit code, PutItem en `OTP_Store` `{pk: phoneNumber, code, taskToken, executionArn, attempts: 0, transferContext, ttl: now+300s}`
    - Send SMS via AWS Pinpoint con mensaje claro: monto + cuenta destino enmascarada + código
    - Return `{ok: true}` — la Lambda termina, pero Step Functions queda esperando el callback con el taskToken
    - _Requirements: 16.1, 16.2, 16.3_

  - [ ]* 8.2 Write unit tests for OTP_Service
    - Generates 6-digit numeric code
    - PutItem includes taskToken and executionArn
    - Pinpoint SMS includes amount and masked destination
    - _Requirements: 16.1, 16.2_

- [ ] 9. Implement OTP Callback in Message_Processor
  - [ ] 9.1 Implement OTP callback handler
    - Create `src/lambdas/message-processor/otp-callback.ts`
    - When `Message_Processor` consume un mensaje de SQS y existe registro en `OTP_Store` para ese `phoneNumber` con TTL activo, prioriza este flujo sobre el normal del Strands Agent
    - `validateAndCallback(phoneNumber, code)`:
      - GetItem `OTP_Store`. Si no existe o expiró → ignorar (Step Functions manejará timeout)
      - Si `code === stored.code` → `sfnClient.send(new SendTaskSuccessCommand({taskToken: stored.taskToken, output: JSON.stringify({valid: true})}))` + DeleteItem en `OTP_Store`
      - Si `code !== stored.code` y `attempts < 2` → UpdateItem `attempts += 1` + enviar mensaje "Código incorrecto" via Twilio
      - Si `attempts === 2` (tercer intento fallido) → `sfnClient.send(new SendTaskFailureCommand({taskToken, error: "OTPBlockedError", cause: "..."}))` + DeleteItem
    - Solo proceder con el flujo normal del agent cuando NO hay OTP pendiente
    - _Requirements: 16.4, 16.5, 16.6, 16.7_

  - [ ]* 9.2 Write property tests for OTP Callback
    - **Property 17: OTP Expiry** — OTP con TTL vencido es ignorado (no callback ni delete)
    - **Property 18: Brute Force Block** — Después de 3 intentos fallidos consecutivos, se invoca `SendTaskFailure` con `OTPBlockedError`
    - _Validates: Requirements 16.6, 16.7_

  - [ ]* 9.3 Write unit tests for OTP Callback
    - Valid code calls SendTaskSuccess and deletes record
    - Invalid code increments attempts and sends retry message
    - Third invalid call triggers SendTaskFailure
    - Expired OTP record is ignored
    - _Requirements: 16.4-16.7_

- [ ] 10. Implement TransferBrebStateMachine (Step Functions)
  - [ ] 10.1 Define state machine in ASL
    - Create `infra/lib/state-machines/transfer-breb.asl.json` con la definición Amazon States Language completa
    - Estados: `ValidateTransfer` → `GenerateOTP` (`waitForTaskToken`, HeartbeatSeconds=300) → `ValidateOTP` (Choice) → `ExecuteTransfer` → `PublishNotifications` (Parallel: SQS sendMessage a email-queue y sms-queue) → `NotifyUserSuccess`
    - Estados de error: `NotifyValidationFailed`, `NotifyOTPExpired`, `NotifyOTPBlocked`, `NotifyTransferFailed`
    - Retry policies en estados Task: `BackoffRate: 2.0, IntervalSeconds: 2, MaxAttempts: 2` para `Lambda.ServiceException`, `Lambda.AWSLambdaException`, `Lambda.SdkClientException`
    - Catch handlers para errores de dominio (`InsufficientFundsError`, `InvalidDestinationError`, `OTPBlockedError`, `States.Timeout`)
    - _Requirements: 18.1, 18.2, 18.3, 18.4_

  - [ ] 10.2 Implement message-handler-notify Lambda (estados de notificación del state machine)
    - Create `src/lambdas/message-handler-notify/index.ts` — Lambda invocada desde los estados terminales del state machine (success y todos los failure paths)
    - Recibe `{phoneNumber, messageType: "transfer_success" | "validation_failed" | "otp_expired" | "otp_blocked" | "transfer_failed", receipt?, error?}`
    - Construye el mensaje apropiado en español colombiano natural y lo envía via Twilio REST API
    - Para `transfer_success` incluye el comprobante con disclaimer "información referencial"
    - _Requirements: 8.4, 8.7, 8.8, 8.9, 8.10, 8.11_

- [ ] 11. Implement Email_Service Lambda (SQS Triggered)
  - [ ] 11.1 Implement SQS-triggered email service
    - Create `src/lambdas/email-service/index.ts` — Handler signature `SQSHandler` con event source mapping en `email-notification-queue`
    - Usar `@aws-lambda-powertools/batch` con `BatchProcessor(EventType.SQS)` para `reportBatchItemFailures` — fallos individuales no afectan al batch completo
    - Por cada mensaje, parsear el `EmailNotificationEvent` y rutear por `type`:
      - `transfer_confirmation` → `sendTransferConfirmation(to, receipt)` via SES `SendEmail` con HTML template
      - `statement_delivery` → `sendStatementEmail(to, s3Bucket, s3Key, fileName)` — descarga PDF de S3, envía via `SendRawEmail` (MIME con adjunto)
    - Aplicar masking a campos sensibles antes de incluirlos en el cuerpo del email
    - Errores propagados al BatchProcessor para que SQS reintente solo los mensajes fallidos
    - _Requirements: 17.2, 17.3, 17.4, 17.5, 17.7_

  - [ ]* 11.2 Write unit tests for Email_Service
    - Transfer confirmation sends correct fields with masking
    - Statement email attaches PDF correctly via SendRawEmail
    - Partial batch failure: only failed messages reported to SQS for retry
    - _Requirements: 17.4, 17.7_

- [ ] 12. Implement SMS_Service Lambda (SQS Triggered)
  - [ ] 12.1 Implement SQS-triggered SMS service
    - Create `src/lambdas/sms-service/index.ts` — Handler signature `SQSHandler` con event source mapping en `sms-notification-queue`
    - Usar `BatchProcessor` igual que Email_Service
    - Procesar `transfer_confirmation` event: enviar SMS via Pinpoint con monto + destino enmascarado
    - NOTA: este servicio es independiente del OTP_Service (que es síncrono dentro del workflow). Este es solo para SMS post-operación de confirmación
    - _Requirements: 17.2, 17.8_

- [ ] 13. Implement Strands_Agent Lambda (Python 3.12)
  - [ ] 13.1 Implement Strands Agent with tools
    - Create `src/lambdas/ai-agent/handler.py` — Lambda handler receiving `{sessionId, inputText, phoneNumber, correlationId}` from Message_Handler
    - Create `src/lambdas/ai-agent/tools.py` — Define Strands tools using `@tool` decorator:
      - `query_balance(phone_number, product_type=None)` → invoke `balance-query` Lambda via boto3
      - `initiate_transfer_breb(source_account, destination_account, amount, concept, phone_number)` → invoke `transfer-breb-initiator` Lambda (que dispara Step Functions). Retorna immediatamente con `executionArn` y mensaje "Te envié un código OTP por SMS para confirmar la operación"
      - `generate_statement(phone_number, account_id, cutoff_date)` → invoke `statement-generator` Lambda
    - Create `src/lambdas/ai-agent/agent.py` — Configure Strands Agent with Claude Haiku 3.5 via Bedrock, system prompt in Spanish, Bedrock Guardrails applied, session memory using sessionId
    - Create `src/lambdas/ai-agent/prompts.py` — System prompt: banking assistant rules, COP formatting, Colombian Spanish, disclaimer template, instruction to NOT wait for OTP synchronously (Strands tools return immediately, Step Functions maneja el resto)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 11.1, 11.2, 12.1, 12.2, 12.3_

  - [ ]* 13.2 Write unit tests for Strands_Agent (pytest)
    - Test tool routing: banking request invokes correct tool
    - Test transfer tool returns immediately with executionArn (no wait for OTP)
    - Test out-of-domain: non-banking request returns declination
    - Test guardrails: blocked topics never return content
    - _Requirements: 10.3, 12.2, 12.6_

- [ ] 14. Checkpoint — Ensure all Lambda tests pass
  - Ensure all tests pass (vitest + pytest), ask the user if questions arise.

- [ ] 15. Implement CDK Lambda constructs and wiring
  - [ ] 15.1 Create Webhook_Receiver Lambda construct (sync, behind API Gateway)
    - Create `infra/lib/constructs/webhook-receiver.ts`
    - Lambda: Node.js 24.x, **256MB**, **10s timeout** (resuelve <1s), VPC config (private subnets + Lambda SG)
    - Trigger: API Gateway HTTP API, route `POST /webhook/twilio`
    - IAM minimalista:
      - `sqs:SendMessage` sobre `inbound-messages-queue.fifo`
      - `secretsmanager:GetSecretValue` sobre el secreto Twilio (solo para validar firma con `TWILIO_AUTH_TOKEN`)
      - CloudWatch Logs
    - Environment variables: `INBOUND_QUEUE_URL`, `TWILIO_SECRET_ARN`
    - Considerar **Provisioned Concurrency** para producción (no requerido en hackathon)
    - _Requirements: 3.1, 3.2, 3.3, 15.1, 15.9_

  - [ ] 15.1B Create Message_Processor Lambda construct (async, SQS-triggered)
    - Create `infra/lib/constructs/message-processor.ts`
    - Lambda: Node.js 24.x, 512MB, **120s timeout**, VPC config
    - Trigger: **SQS Event Source Mapping** sobre `inbound-messages-queue.fifo` con:
      - `batchSize: 1`
      - `reportBatchItemFailures: true`
      - `maximumConcurrency: 10` (limita concurrency por orden FIFO)
    - IAM:
      - SQS ReceiveMessage+DeleteMessage+ChangeMessageVisibility+GetQueueAttributes sobre `inbound-messages-queue.fifo`
      - DynamoDB (Consent_Store read+write, Auth_Session read, OTP_Store read+update+delete)
      - Lambda InvokeFunction sobre Strands_Agent
      - **Step Functions SendTaskSuccess+SendTaskFailure sobre TransferBrebStateMachine ARN**
      - S3 GetObject (Statement_Bucket), S3 PutObject+GetObject+DeleteObject (Audio_Temp_Bucket)
      - Transcribe StartTranscriptionJob+GetTranscriptionJob
      - Secrets Manager GetSecretValue (Twilio creds para REST API)
      - CloudWatch Logs
    - Environment variables: table names, bucket names, ai-agent Lambda ARN, Twilio secret ARN, TransferBrebStateMachine ARN
    - _Requirements: 3.5, 3.6, 3.7, 3.9, 15.1, 15.2, 15.10, 16.5, 16.7_

  - [ ] 15.2 Create Auth_Service Lambda construct
    - Create `infra/lib/constructs/auth-service.ts`
    - Lambda: Node.js 24.x, 128MB, 10s timeout, VPC config
    - Trigger: Lambda Function URL with CORS
    - IAM: DynamoDB Auth_Session PutItem, CloudWatch Logs
    - _Requirements: 5.3, 14.3, 15.1_

  - [ ] 15.3 Create Action Group + Step Functions task Lambda constructs
    - `infra/lib/constructs/balance-query.ts` — Node.js 24.x, 128MB, 15s timeout, VPC config, IAM: CloudWatch Logs
    - `infra/lib/constructs/transfer-breb-initiator.ts` — Node.js 24.x, 128MB, 10s timeout, VPC config, IAM: **Step Functions StartExecution on TransferBrebStateMachine ARN**, CloudWatch Logs
    - `infra/lib/constructs/transfer-breb-validate.ts` — Node.js 24.x, 128MB, 10s timeout, VPC config, IAM: CloudWatch Logs (invocada solo por Step Functions, sin acceso externo)
    - `infra/lib/constructs/transfer-breb-execute.ts` — Node.js 24.x, 128MB, 15s timeout, VPC config, IAM: CloudWatch Logs (Mock_Core en memoria; en producción agregar acceso al core real)
    - `infra/lib/constructs/statement-generator.ts` — Node.js 24.x, 256MB, 30s timeout, VPC config, IAM: S3 Statement_Bucket PutObject, **SQS SendMessage on email-notification-queue**, CloudWatch Logs
    - `infra/lib/constructs/message-handler-notify.ts` — Node.js 24.x, 128MB, 10s timeout, VPC config, IAM: Secrets Manager GetSecretValue (Twilio creds), CloudWatch Logs
    - _Requirements: 14.3, 15.1, 17.3, 18.7_

  - [ ] 15.4 Create OTP_Service Lambda construct
    - Create `infra/lib/constructs/otp-service.ts`
    - Lambda: Node.js 24.x, 128MB, 10s timeout, VPC config
    - IAM: DynamoDB OTP_Store PutItem, Pinpoint SendMessages, CloudWatch Logs
    - Lambda invocada únicamente por Step Functions con `waitForTaskToken` (no es invocada por Message_Handler ni por API Gateway)
    - _Requirements: 16.1, 16.2, 16.3_

  - [ ] 15.5 Create Email_Service Lambda construct (SQS triggered)
    - Create `infra/lib/constructs/email-service.ts`
    - Lambda: Node.js 24.x, 256MB, 30s timeout, VPC config
    - Trigger: **SQS Event Source Mapping** sobre `email-notification-queue` con `batchSize: 10`, `maxBatchingWindow: 5s`, `reportBatchItemFailures: true`
    - IAM: SES SendEmail+SendRawEmail, S3 Statement_Bucket GetObject, SQS ReceiveMessage+DeleteMessage+ChangeMessageVisibility on email-notification-queue, CloudWatch Logs
    - _Requirements: 17.3, 17.4_

  - [ ] 15.6 Create SMS_Service Lambda construct (SQS triggered)
    - Create `infra/lib/constructs/sms-service.ts`
    - Lambda: Node.js 24.x, 128MB, 15s timeout, VPC config
    - Trigger: SQS Event Source Mapping sobre `sms-notification-queue` con `batchSize: 10`, `reportBatchItemFailures: true`
    - IAM: Pinpoint SendMessages, SQS ReceiveMessage+DeleteMessage on sms-notification-queue, CloudWatch Logs
    - _Requirements: 17.2, 17.8_

  - [ ] 15.7 Create Strands_Agent Lambda construct
    - Create `infra/lib/constructs/ai-agent.ts`
    - Lambda: Python 3.12, 512MB, 60s timeout, VPC config
    - IAM: Bedrock InvokeModel (Claude Haiku 3.5), Bedrock ApplyGuardrail, Lambda InvokeFunction (balance-query, transfer-breb-initiator, statement-generator), CloudWatch Logs
    - Bedrock Guardrails resource: content filtering (HATE, VIOLENCE, MISCONDUCT, PROMPT_ATTACK), topic policies (investment-advice DENY, non-banking DENY), blocked messages in Spanish
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 14.3_

  - [ ] 15.8 Create TransferBrebStateMachine construct
    - Create `infra/lib/constructs/transfer-breb-state-machine.ts`
    - Cargar ASL definition desde `infra/lib/state-machines/transfer-breb.asl.json`
    - Sustituir placeholders (`${EmailNotificationQueueUrl}`, `${SmsNotificationQueueUrl}`, ARNs de Lambdas) usando `DefinitionBody.fromString` o `StateMachine.fromDefinitionSubstitutions`
    - Tipo: `StateMachineType.STANDARD`
    - Logging: CloudWatch Logs LogLevel.ALL, retention 90 días
    - IAM Role del state machine: InvokeFunction sobre las Lambdas de tasks, SQS SendMessage sobre las dos colas
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6_

  - [ ] 15.9 Create main CDK stack wiring all constructs
    - Create `infra/lib/stacks/btg-connectai-stack.ts` — Instantiate all constructs en orden de dependencias: vpc-config → dynamodb-tables → s3 → secrets → notification-queues → state-machine → lambdas → api-gateway → observability
    - Create `infra/bin/app.ts` — CDK App entry point
    - Create `infra/lib/config/environment.ts` — Region us-east-1, account, VPC stack name
    - Verify all Lambdas have VpcConfig pointing to IA-Builder private subnets
    - _Requirements: 15.1, 15.4, 15.5_

- [ ] 16. Implement Observability stack
  - [ ] 16.1 Create observability construct
    - Create `infra/lib/constructs/observability.ts`
    - CloudWatch Dashboard widgets:
      - Lambdas: invocations, errors, latency p50/p90 para cada Lambda (Webhook_Receiver, Message_Processor, Auth_Service, Strands_Agent, OTP_Service, Email_Service, SMS_Service, balance-query, transfer-breb-initiator, transfer-breb-validate, transfer-breb-execute, statement-generator, message-handler-notify)
      - **Webhook_Receiver latency**: widget dedicado mostrando p50/p95/p99 con threshold visual a 1000ms (SLO)
      - **Step Functions**: ExecutionsStarted, ExecutionsSucceeded, ExecutionsFailed, ExecutionTime p50/p95 sobre `TransferBrebStateMachine`
      - **SQS**: ApproximateNumberOfMessagesVisible y ApproximateAgeOfOldestMessage para cada cola (inbound-messages, email-notification, sms-notification) y todos los DLQ
    - Alarmas:
      - **Webhook_Receiver latency p99 > 1000ms** en 5min → SNS alarm topic (rompe SLO de respuesta async)
      - Error rate > 10% en 5min por Lambda → SNS alarm topic
      - `ExecutionsFailed > 5 en 5min` sobre TransferBrebStateMachine → SNS alarm topic
      - `ApproximateNumberOfMessagesVisible > 0` en cualquier DLQ (inbound-messages-dlq, email-dlq, sms-dlq) → SNS alarm topic
      - `ApproximateAgeOfOldestMessage > 60s` en `inbound-messages-queue.fifo` → Processor saturado o caído
      - `ApproximateAgeOfOldestMessage > 300s` en email/sms queues → consumer atrasado
    - CloudWatch Logs retention: 7 días para Lambda log groups, 90 días para state machine
    - _Requirements: 3.3, 3.9, 13.1, 13.3, 13.4, 17.6, 18.5, 18.6_

- [ ] 17. Final checkpoint — CDK synth and full test suite
  - Run full test suite (vitest + pytest)
  - Run `cdk synth` — validate CloudFormation template
  - Verify all Lambdas have VpcConfig defined y pointing to IA-Builder-sandbox-networking subnets
  - Verify TransferBrebStateMachine ASL definition validates con `cdk-validation-pipeline`
  - Verify `inbound-messages-queue.fifo` exists con `MessageGroupId` y `MessageDeduplicationId` correctos en mensajes de prueba
  - Verify Webhook_Receiver, Email_Service y SMS_Service tienen SQS Event Source Mappings configurados con `reportBatchItemFailures: true` (Message_Processor) y permisos correctos
  - Verify NO existe tabla DynamoDB Dedup (debe estar reemplazada por SQS FIFO dedup)
  - Configure Twilio Sandbox webhook URL al endpoint API Gateway (POST /webhook/twilio)
  - Smoke test end-to-end: enviar mensaje desde WhatsApp → confirmar respuesta de Twilio 200 en <1s → verificar mensaje aparece en SQS → verificar Processor lo procesa → respuesta llega al cliente
  - Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP delivery
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation before proceeding
- Property tests validate universal correctness (fast-check for Node.js, hypothesis for Python)
- Unit tests validate specific examples and edge cases
- Node.js 24.x for all business Lambdas; Python 3.12 exclusively for Strands_Agent
- All Lambdas deployed in VPC private subnets (10.0.11.0/24, 10.0.12.0/24) via NAT Gateway
- Mock data inline in Action Group Lambdas — no external database for banking data
- Twilio credentials stored in Secrets Manager, never hardcoded
- **Async Webhook Pattern**: `Webhook_Receiver` Lambda síncrono responde 200 a Twilio en <1s y publica a SQS FIFO; `Message_Processor` consume async sin presión de tiempo. Twilio NUNCA experimenta timeouts del lado del backend
- **SQS FIFO con dedup nativa** (`MessageDeduplicationId = MessageSid`): elimina la tabla Dedup custom. Twilio retries son descartados automáticamente en ventana de 5 min
- **Order guarantee per cliente** vía `MessageGroupId = phoneNumber`: dos mensajes seguidos del mismo cliente se procesan en orden, pero distintos clientes se procesan en paralelo
- **Step Functions Standard Workflow** orquesta transferencias BRE-B con patrón `waitForTaskToken` — Lambdas NUNCA esperan al usuario, el state machine maneja la suspensión
- **SQS-based async notifications** — Email_Service y SMS_Service son fire-and-forget, productores no esperan respuesta. DLQ después de 3 fallos
- Para el MVP en modo mock, el happy path es prioritario. Los caminos de error del state machine (`NotifyOTPExpired`, `NotifyTransferFailed`, etc.) deben estar implementados pero no exhaustivamente probados — son red de seguridad

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
    { "id": 9, "tasks": ["13.2", "15.1", "15.1B", "15.2", "15.3", "15.4", "15.5", "15.6", "15.7", "15.8"] },
    { "id": 10, "tasks": ["15.9", "16.1"] }
  ]
}
```
