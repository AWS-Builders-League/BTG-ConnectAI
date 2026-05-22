# Implementation Plan: BTG ConnectAI MVP Lite

## Overview

Implementación incremental de un asistente bancario conversacional serverless para WhatsApp usando Amazon Bedrock Agent con Claude Haiku 3.5. El plan construye la infraestructura CDK primero, luego los componentes Lambda individuales (shared utilities, gateway, auth, action groups), y finalmente la capa de observabilidad y wiring completo.

## Tasks

- [ ] 1. Set up project structure, shared utilities, and CDK foundation
  - [ ] 1.1 Initialize project structure with TypeScript configuration
    - Create directory structure: `infra/bin/`, `infra/lib/stacks/`, `infra/lib/constructs/`, `infra/lib/config/`, `src/lambdas/`, `src/shared/`, `src/login-page/`, `src/tests/unit/`, `src/tests/property/`
    - Initialize `package.json` with dependencies: aws-cdk-lib, constructs, @aws-sdk/client-dynamodb, @aws-sdk/client-s3, @aws-sdk/client-bedrock-agent-runtime, @aws-sdk/client-social-messaging, @aws-sdk/client-transcribe, @aws-lambda-powertools/logger, @aws-lambda-powertools/metrics, uuid, pdfkit
    - Configure `tsconfig.json` for Node.js 20.x with strict mode
    - Configure `cdk.json` with app entry point
    - Add vitest as test framework with `vitest.config.ts`
    - Add fast-check for property-based testing
    - _Requirements: 15.5_

  - [ ] 1.2 Implement shared utilities (logger, masking, constants, types)
    - Create `src/shared/logger.ts` — Lambda Powertools logger configuration with service name and structured JSON output
    - Create `src/shared/masking.ts` — Data masking functions for phone numbers (retain last 4), account numbers (retain last 4), document IDs (retain last 4)
    - Create `src/shared/constants.ts` — Shared constants (MAX_WHATSAPP_LENGTH=4096, AUTH_SESSION_TTL=1800, DEDUP_TTL=600, TC_VERSION="1.0")
    - Create `src/shared/types.ts` — Shared TypeScript interfaces (EUMSIncomingPayload, ConsentRecord, AuthSession, BedrockAgentActionGroupEvent, BedrockAgentActionGroupResponse, MockClient, MockProduct, MockTransaction)
    - Create `src/shared/formatting.ts` — COP currency formatting function ($X.XXX.XXX,YY pattern)
    - _Requirements: 13.1, 14.4, 10.5_

  - [ ]* 1.3 Write property tests for shared utilities
    - **Property 4: Data Masking Correctness** — For any string ≥ 4 chars, masking retains only last 4 characters visible
    - **Property 15: COP Currency Formatting** — For any non-negative number, produces $X.XXX.XXX,YY pattern
    - **Validates: Requirements 14.4, 10.5**

  - [ ]* 1.4 Write unit tests for shared utilities
    - Test masking edge cases (strings < 4 chars, empty strings, phone with prefix)
    - Test COP formatting with 0, integers, large numbers, decimals
    - Test logger configuration outputs correct structure
    - _Requirements: 14.4, 10.5_

- [ ] 2. Implement CDK infrastructure stack (DynamoDB, S3, SNS)
  - [ ] 2.1 Create DynamoDB tables construct
    - Create `infra/lib/constructs/dynamodb-tables.ts`
    - Define Dedup table: pk (String, partition key), TTL on `ttl` attribute, PAY_PER_REQUEST, AWS managed encryption
    - Define Consent_Store table: pk (String, partition key), PAY_PER_REQUEST, no TTL, AWS managed encryption
    - Define Auth_Session table: pk (String, partition key), TTL on `ttl` attribute, PAY_PER_REQUEST, AWS managed encryption
    - _Requirements: 1.5, 3.4, 5.3, 6.1, 14.1, 15.1_

  - [ ] 2.2 Create S3 buckets construct
    - Create `infra/lib/constructs/audio-processing.ts` — Audio_Temp_Bucket with 1-day lifecycle rule, AWS managed encryption, block public access
    - Create Statement_Bucket in `infra/lib/constructs/statement-generator.ts` — 1-day lifecycle rule, AWS managed encryption, block public access (all 4 settings)
    - _Requirements: 9.3, 14.1, 14.7_

  - [ ] 2.3 Create SNS topics and security constructs
    - Create `infra/lib/constructs/security.ts` — Secrets Manager secret for config (whatsappPhoneNumberId, bedrockAgentId, bedrockAgentAliasId, loginPageUrl, authServiceUrl)
    - Create SNS topic for incoming WhatsApp messages
    - Create SNS topic for CloudWatch alarms
    - _Requirements: 14.5, 3.1, 13.4_

- [ ] 3. Checkpoint - Ensure infrastructure constructs compile
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement WhatsApp_Gateway Lambda
  - [ ] 4.1 Implement deduplication module
    - Create `src/lambdas/whatsapp-gateway/dedup.ts`
    - Implement `checkAndStoreDeduplicate(messageId)` using DynamoDB conditional PutItem with `attribute_not_exists(pk)` and TTL of 10 minutes
    - Return false (not duplicate) on successful write, true (duplicate) on ConditionalCheckFailedException
    - _Requirements: 3.4_

  - [ ]* 4.2 Write property test for deduplication
    - **Property 2: Deduplication Idempotency** — For any valid message ID, first call returns false, second call returns true
    - **Validates: Requirements 3.4**

  - [ ] 4.3 Implement consent flow module
    - Create `src/lambdas/whatsapp-gateway/consent.ts`
    - Implement `getConsent(phoneNumber)` — GetItem from Consent_Store
    - Implement `storeConsent(phoneNumber, status)` — PutItem with timestamp and tcVersion
    - Implement `handleConsentFlow(payload, consent)` — Logic for first message (send T&C buttons), accept button (store + welcome), reject button (store + inform)
    - Implement `sendTermsAndConditionsMessage(phoneNumber)` — Interactive message with accept/reject buttons via EUMS
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 4.4 Write property test for consent gate
    - **Property 5: Consent Gate — Existing Consent Skips T&C** — For any phone with accepted consent, check returns true
    - **Validates: Requirements 1.4**

  - [ ] 4.5 Implement auth session check module
    - Create `src/lambdas/whatsapp-gateway/auth.ts`
    - Implement `getAuthSession(phoneNumber)` — GetItem from Auth_Session table
    - Implement `isExpired(session)` — Check if TTL has passed
    - Implement `storePendingRequest(phoneNumber, inputText)` — Store original request for post-auth processing
    - Implement `sendLoginButton(phoneNumber)` — Generate callback token, build login URL, send interactive button via EUMS
    - Implement `deriveSessionId(phoneNumber)` — Deterministic session ID derivation from phone number for Bedrock Agent
    - _Requirements: 5.1, 5.6, 5.8, 6.1, 6.2, 11.1_

  - [ ]* 4.6 Write property tests for auth and session
    - **Property 3: Session ID Determinism** — Same phone always produces same session ID, different phones produce different IDs
    - **Property 6: Auth Gate — No Session Triggers Login** — No active session triggers login flow
    - **Property 7: Auth Gate — Active Session Allows Actions** — Active session with future TTL allows actions
    - **Validates: Requirements 11.1, 5.1, 5.8, 5.6, 6.1, 6.2**

  - [ ] 4.7 Implement audio transcription module
    - Create `src/lambdas/whatsapp-gateway/transcription.ts`
    - Implement `transcribeAudio(audio)` — Download media from WhatsApp, upload to S3 temp, start Transcribe job (es-CO, OGG format), poll for result (max 10s), cleanup temp files
    - Handle errors gracefully returning null on failure
    - _Requirements: 2.2, 2.3, 2.6_

  - [ ] 4.8 Implement messaging module (send responses, split messages, send documents)
    - Create `src/lambdas/whatsapp-gateway/messaging.ts`
    - Implement `sendWhatsAppResponse(phoneNumber, text)` — Split if > 4096 chars, send sequentially via EUMS SendWhatsAppMessageCommand
    - Implement `splitMessage(text, maxLength)` — Split at newlines or spaces, never exceed maxLength per chunk
    - Implement `sendReply(phoneNumber, text)` — Simple text reply
    - Implement `sendWelcomeMessage(phoneNumber)` — Welcome message listing available services
    - Implement `sendWhatsAppDocument(phoneNumber, s3Bucket, s3Key, fileName, caption)` — Download PDF from S3, upload via PostWhatsAppMessageMedia to get media_id, send as WhatsApp document message via SendWhatsAppMessage (type "document")
    - _Requirements: 3.2, 3.6, 4.1, 4.2, 9.4_

  - [ ]* 4.9 Write property test for message splitting
    - **Property 1: Message Splitting Round-Trip** — Splitting and concatenating produces original string, every chunk ≤ 4096 chars
    - **Validates: Requirements 3.6**

  - [ ] 4.10 Implement main handler (WhatsApp_Gateway index.ts)
    - Create `src/lambdas/whatsapp-gateway/index.ts`
    - Parse SNS event to extract EUMSIncomingPayload
    - Orchestrate: dedup check → consent check → message type routing (text/audio/interactive/unsupported) → auth check → Bedrock Agent invocation → response sending
    - Generate correlation_id (UUID v4) and propagate via logger
    - Handle timeout (15s) for Bedrock Agent response
    - Create `src/lambdas/whatsapp-gateway/types.ts` for local interfaces
    - _Requirements: 2.1, 2.4, 2.5, 3.1, 3.3, 3.5, 5.1, 13.1, 13.2_

  - [ ]* 4.11 Write property test for unsupported message format
    - **Property 16: Unsupported Message Format Rejection** — Messages with type image/video/sticker/document/location are classified as unsupported
    - **Validates: Requirements 2.5**

  - [ ]* 4.12 Write unit tests for WhatsApp_Gateway modules
    - Test consent flow: first message sends T&C, accept stores consent, reject stores rejection
    - Test dedup: duplicate detection, TTL calculation
    - Test auth check: expired session, valid session, no session
    - Test message routing: text, audio, interactive, unsupported types
    - Test error handling: Consent_Store unavailable, Bedrock timeout
    - _Requirements: 1.1, 1.2, 1.3, 1.6, 2.5, 3.4, 3.5, 5.1, 6.5_

- [ ] 5. Checkpoint - Ensure WhatsApp_Gateway tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement Auth_Service Lambda and Login_Page
  - [ ] 6.1 Implement Auth_Service Lambda
    - Create `src/lambdas/auth-service/index.ts` — Handler for POST /authenticate
    - Create `src/lambdas/auth-service/users.ts` — Hardcoded test users array (carlos.rodriguez, maria.lopez, juan.garcia) with credentials, phone numbers, names, document IDs
    - Create `src/lambdas/auth-service/types.ts` — AuthenticateRequest, AuthenticateResponse interfaces
    - Implement authentication logic: validate callback token → find user by username+password → verify phone matches → create Auth_Session in DynamoDB (TTL 30min) → return success/failure
    - Add CORS headers for Login_Page access
    - _Requirements: 5.2, 5.3, 5.5, 5.7, 6.1_

  - [ ]* 6.2 Write property tests for Auth_Service
    - **Property 8: Invalid Credentials Rejection** — Any username/password not matching test users returns success:false and no session created
    - **Validates: Requirements 5.5**

  - [ ]* 6.3 Write unit tests for Auth_Service
    - Test valid credentials create session with correct TTL
    - Test invalid username returns error
    - Test valid credentials but wrong phone returns error
    - Test invalid callback token returns error
    - _Requirements: 5.3, 5.5, 5.7_

  - [ ] 6.4 Implement Login_Page (S3 static site)
    - Create `src/login-page/index.html` — Login form with username/password fields, BTG Pactual branding, responsive design
    - Create `src/login-page/styles.css` — BTG Pactual color scheme, mobile-first responsive layout
    - Create `src/login-page/app.js` — Form submission logic: extract phone/token from URL params, POST to Auth_Service, show success/error messages
    - _Requirements: 5.2_

- [ ] 7. Implement Action Group Lambdas
  - [ ] 7.1 Implement balance-query Action Group Lambda
    - Create `src/lambdas/balance-query/index.ts` — Handler parsing BedrockAgentActionGroupEvent, routing to getBalance
    - Create `src/lambdas/balance-query/mock-data.ts` — Mock_Core data for 3 test clients with products (fondos de inversión + cuentas corrientes)
    - Create `src/lambdas/balance-query/types.ts` — BalanceResponse, ProductBalance interfaces
    - Implement: find client by phoneNumber → filter by productType if specified → return all products if no filter → return 404 if client not found
    - Format response as BedrockAgentActionGroupResponse
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 7.2 Write property tests for balance-query
    - **Property 9: Balance Query Correctness** — Existing client returns all products with correct fields matching Mock_Core
    - **Property 10: Unknown Client Error** — Non-existent phone returns 404 error
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4**

  - [ ] 7.3 Implement transfer-breb Action Group Lambda
    - Create `src/lambdas/transfer-breb/index.ts` — Handler parsing BedrockAgentActionGroupEvent, routing to validateTransfer and executeTransfer
    - Create `src/lambdas/transfer-breb/mock-data.ts` — Shared mock data (import from balance-query or duplicate for isolation)
    - Create `src/lambdas/transfer-breb/types.ts` — TransferRequest, TransferResult, TransferReceipt interfaces
    - Implement validateTransfer: check source account exists + belongs to client + sufficient funds + destination exists
    - Implement executeTransfer: validate → update mock balances → generate receipt with transactionId, masked accounts, amount, timestamp, status
    - _Requirements: 8.1, 8.2, 8.3, 8.5, 8.6_

  - [ ]* 7.4 Write property tests for transfer-breb
    - **Property 11: Transfer Execution Produces Valid Receipt** — Valid transfer produces receipt with all required fields
    - **Property 12: Insufficient Funds Rejection** — Amount > availableBalance rejects with error, balance unchanged
    - **Validates: Requirements 8.3, 8.5**

  - [ ] 7.5 Implement statement-generator Action Group Lambda
    - Create `src/lambdas/statement-generator/index.ts` — Handler parsing BedrockAgentActionGroupEvent, routing to generateStatement
    - Create `src/lambdas/statement-generator/pdf-generator.ts` — PDF generation using pdfkit (client name, masked account, period, transactions, final balance)
    - Create `src/lambdas/statement-generator/mock-data.ts` — Mock transactions data
    - Create `src/lambdas/statement-generator/types.ts` — StatementRequest, StatementResult interfaces
    - Implement: validate cutoff date (must be past) → get client data → generate PDF → upload to S3 → return S3 bucket + key + fileName (no presigned URL needed)
    - Handle empty transactions case (generate empty statement)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [ ]* 7.6 Write property tests for statement-generator
    - **Property 13: Future Date Rejection for Statements** — Today or future date rejects with error
    - **Property 14: Valid Statement Generation with S3 Reference** — Valid past date + existing client produces success with S3 bucket, key, and fileName in response
    - **Validates: Requirements 9.2, 9.3, 14.7**

  - [ ]* 7.7 Write unit tests for Action Group Lambdas
    - Test balance-query: all products, filtered by type, client not found
    - Test transfer-breb: valid transfer, insufficient funds, invalid destination, invalid source
    - Test statement-generator: valid generation, future date rejection, empty transactions
    - _Requirements: 7.1-7.4, 8.3, 8.5, 8.6, 9.2, 9.3, 9.6_

- [ ] 8. Checkpoint - Ensure all Lambda tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Implement CDK Lambda constructs and Bedrock Agent configuration
  - [ ] 9.1 Create WhatsApp_Gateway Lambda construct
    - Create `infra/lib/constructs/whatsapp-gateway.ts`
    - Define Lambda function: Node.js 20.x, 512MB memory, 60s timeout, no VPC
    - Subscribe to SNS incoming messages topic
    - Configure IAM role with least privilege: DynamoDB (dedup, consent, auth read/write), Bedrock InvokeAgent, EUMS SendWhatsAppMessage + GetWhatsAppMessageMedia + PostWhatsAppMessageMedia, Transcribe StartTranscriptionJob + GetTranscriptionJob, S3 audio temp bucket, S3 GetObject on Statement_Bucket, Secrets Manager GetSecretValue, CloudWatch Logs
    - Set environment variables for table names, bucket names, agent IDs
    - _Requirements: 3.1, 14.3, 15.1, 15.2, 15.3_

  - [ ] 9.2 Create Auth_Service Lambda construct
    - Create `infra/lib/constructs/auth-service.ts`
    - Define Lambda function: Node.js 20.x, 128MB memory, 10s timeout, no VPC
    - Configure Function URL with CORS for Login_Page origin
    - Configure IAM role: DynamoDB Auth_Session PutItem, CloudWatch Logs
    - _Requirements: 5.3, 14.3, 15.1_

  - [ ] 9.3 Create Login_Page S3 static site construct
    - Create `infra/lib/constructs/login-page.ts`
    - Define S3 bucket for static website hosting
    - Configure BucketDeployment to upload login-page assets
    - Set up CloudFront distribution (or S3 website endpoint for MVP simplicity)
    - _Requirements: 5.2_

  - [ ] 9.4 Create Action Group Lambda constructs
    - Create `infra/lib/constructs/balance-query.ts` — Lambda: 128MB, 15s timeout, no VPC, IAM: CloudWatch Logs only
    - Create `infra/lib/constructs/transfer-breb.ts` — Lambda: 128MB, 15s timeout, no VPC, IAM: CloudWatch Logs only
    - Create `infra/lib/constructs/statement-generator.ts` — Lambda: 256MB, 30s timeout, no VPC, IAM: S3 Statement_Bucket PutObject only, CloudWatch Logs
    - _Requirements: 14.3, 15.1, 15.2_

  - [ ] 9.5 Create Bedrock Agent and Guardrails construct
    - Create `infra/lib/constructs/bedrock-agent.ts`
    - Define Bedrock Agent with Claude Haiku 3.5 foundation model
    - Configure agent instructions (system prompt in Spanish for Colombian banking assistant)
    - Define 3 Action Groups with OpenAPI schemas: balance-query, transfer-breb, statement-generator
    - Configure Bedrock Guardrails: content filtering (SEXUAL, VIOLENCE, HATE, INSULTS, MISCONDUCT, PROMPT_ATTACK), topic policies (investment-advice DENY, non-banking-topics DENY, competitor-info DENY)
    - Configure blocked input/output messaging in Spanish
    - Configure Bedrock Agent IAM role: InvokeModel, Lambda InvokeFunction for action groups, ApplyGuardrail
    - _Requirements: 10.1, 12.1, 12.2, 12.3, 12.6_

  - [ ] 9.6 Create main CDK stack wiring all constructs
    - Create `infra/lib/stacks/btg-connectai-stack.ts` — Instantiate all constructs, wire dependencies (pass table ARNs, bucket names, Lambda ARNs between constructs)
    - Create `infra/bin/app.ts` — CDK App entry point
    - Create `infra/lib/config/environment.ts` — Environment-specific configuration (region, account)
    - Ensure no Lambda has VpcConfig defined
    - _Requirements: 15.1, 15.4, 15.5_

- [ ] 10. Implement Observability stack
  - [ ] 10.1 Create observability construct (Dashboard, Alarms, SNS)
    - Create `infra/lib/constructs/observability.ts`
    - Define CloudWatch Dashboard with widgets: invocations, errors, duration p50/p90 for each Lambda (WhatsApp_Gateway, Auth_Service, balance-query, transfer-breb, statement-generator)
    - Define error rate alarms (>10% in 5min window) for each Lambda using math expressions (errors/invocations*100)
    - Configure alarm actions to publish to SNS alarm topic
    - Set CloudWatch Logs retention to 7 days for all Lambda log groups
    - _Requirements: 13.1, 13.3, 13.4_

- [ ] 11. Final checkpoint - Ensure CDK synth succeeds and all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run `cdk synth` to validate CloudFormation template generation
  - Verify no Lambda has VpcConfig in synthesized template

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- All Lambdas use Node.js 20.x runtime with TypeScript
- Mock data is inline in Action Group Lambdas (no external database calls)
- CDK infrastructure uses single-stack approach for MVP simplicity
- All encryption uses AWS managed keys (zero cost, zero management)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.1", "2.2", "2.3"] },
    { "id": 3, "tasks": ["4.1", "4.3", "4.5", "4.7", "4.8"] },
    { "id": 4, "tasks": ["4.2", "4.4", "4.6", "4.9", "4.10", "4.11"] },
    { "id": 5, "tasks": ["4.12", "6.1", "6.4"] },
    { "id": 6, "tasks": ["6.2", "6.3", "7.1", "7.3", "7.5"] },
    { "id": 7, "tasks": ["7.2", "7.4", "7.6", "7.7"] },
    { "id": 8, "tasks": ["9.1", "9.2", "9.3", "9.4", "9.5"] },
    { "id": 9, "tasks": ["9.6", "10.1"] }
  ]
}
```
