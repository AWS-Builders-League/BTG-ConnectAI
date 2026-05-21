# Implementation Plan: BTG ConnectAI

## Overview

This plan implements the BTG ConnectAI serverless Agentic AI system on AWS. The implementation follows a layered approach: starting with shared infrastructure and interfaces, then building core components (Gateway, NLU, Context Manager), followed by service-layer components (Financial Query, Operations, Secure Links), workflow orchestration, traceability, and finally integration wiring. Each component is implemented as an AWS Lambda function with Python or TypeScript as specified in the design.

## Tasks

- [ ] 1. Set up project structure and core interfaces
  - [ ] 1.1 Create project directory structure and configuration files
    - Create monorepo structure with directories for each Lambda function
    - Set up `pyproject.toml` for Python Lambdas and `package.json` for TypeScript Lambdas
    - Configure AWS CDK or SAM template skeleton for infrastructure-as-code
    - Set up shared Python package for common types and utilities
    - Configure Hypothesis testing framework with project-level settings (min 100 iterations, deadline 5000ms)
    - _Requirements: 12.5, 13.1_

  - [ ] 1.2 Define core TypeScript interfaces for the WhatsApp Gateway
    - Implement `InboundMessage`, `OutboundMessage`, and `AgentRequest` interfaces as defined in the design
    - Implement message serialization/deserialization utilities
    - _Requirements: 1.1, 1.2, 1.3_

  - [ ] 1.3 Define core Python data models for shared components
    - Implement Pydantic models for `NLUResult`, `Intent`, `Entity`, `Session`, `ConversationTurn`, `ContextWindow`
    - Implement models for `WorkflowDefinition`, `WorkflowStep`, `WorkflowExecution`
    - Implement models for `AuditRecord`, `AuditQuery`
    - _Requirements: 2.1, 3.1, 9.1, 10.1_

  - [ ] 1.4 Define DynamoDB table schemas and CDK/SAM infrastructure definitions
    - Define Sessions Table with GSI-1 (phoneNumber)
    - Define Context Store Table
    - Define Audit Trail Table with GSI-1 (clientId, timestamp) and GSI-2 (eventType, timestamp)
    - Define Secure Links Table
    - Configure TTL settings, encryption (KMS AES-256), and multi-AZ deployment
    - _Requirements: 3.3, 3.4, 10.4, 14.1, 12.5_

- [ ] 2. Implement WhatsApp Gateway Lambda
  - [ ] 2.1 Implement inbound message handler (SNS trigger)
    - Parse SNS notification from AWS End User Messaging Social
    - Validate and normalize inbound message payload
    - Generate `AgentRequest` with correlation ID and timestamp
    - Enqueue to SQS message queue
    - Handle unregistered phone numbers with welcome message (Requirement 1.4)
    - _Requirements: 1.1, 1.4, 1.6_

  - [ ] 2.2 Implement outbound message delivery
    - Serialize `OutboundMessage` to WhatsApp Business API payload format
    - Support text, interactive buttons, and interactive list message types
    - Implement exponential backoff retry for delivery failures (max 5 minutes)
    - Queue messages when connectivity is lost
    - _Requirements: 1.2, 1.3, 1.5_

  - [ ]* 2.3 Write property test for outbound message serialization (Property 1)
    - **Property 1: Outbound Message Serialization Validity**
    - Generate arbitrary valid `OutboundMessage` instances and verify serialized payload conforms to WhatsApp schema
    - **Validates: Requirements 1.3**

  - [ ]* 2.4 Write property test for retry backoff timing (Property 2)
    - **Property 2: Retry Backoff Timing Correctness**
    - Generate sequences of delivery failures and verify exponential backoff intervals and total duration ≤ 5 minutes
    - **Validates: Requirements 1.5**

  - [ ]* 2.5 Write unit tests for WhatsApp Gateway
    - Test SNS notification parsing with various message types
    - Test unregistered phone number handling
    - Test message queuing during connectivity loss
    - _Requirements: 1.1, 1.4, 1.5_

- [ ] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement NLU Engine integration
  - [ ] 4.1 Implement Bedrock Claude invocation for intent classification
    - Create prompt template for Spanish NLU with structured output
    - Invoke Amazon Bedrock Claude 3.5 Sonnet with conversation context
    - Parse structured response into `NLUResult` (intents, entities, confidence)
    - Handle colloquial Spanish expressions and Colombian banking terminology
    - _Requirements: 2.1, 2.5, 2.6_

  - [ ] 4.2 Implement confidence-based routing logic
    - Route to direct execution when confidence > 0.85
    - Route to clarification flow when confidence ∈ [0.5, 0.85)
    - Route to not-understood response when confidence < 0.5
    - Handle multi-intent messages by sequencing intents
    - _Requirements: 2.2, 2.3, 2.4, 2.7_

  - [ ] 4.3 Implement AI governance guardrails
    - Restrict responses to banking/financial topics
    - Apply content filtering for financial advice and investment recommendations
    - Detect and flag potential fraud/social engineering patterns
    - Terminate interaction and generate security alert on fraud detection
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

  - [ ]* 4.4 Write property test for confidence-based routing (Property 3)
    - **Property 3: Confidence-Based Intent Routing**
    - Generate confidence scores in [0, 1] and verify routing matches threshold bands
    - **Validates: Requirements 2.2, 2.3, 2.4**

  - [ ]* 4.5 Write property test for NLU output structure (Property 4)
    - **Property 4: NLU Output Structural Validity**
    - Generate valid text inputs and verify NLU returns at least one intent with confidence in [0, 1]
    - **Validates: Requirements 2.1**

  - [ ]* 4.6 Write unit tests for NLU Engine
    - Test specific Spanish colloquial expressions and abbreviations
    - Test fraud detection patterns
    - Test off-topic rejection examples
    - Test multi-intent message handling
    - _Requirements: 2.6, 2.7, 11.4, 11.5_

- [ ] 5. Implement Context Manager
  - [ ] 5.1 Implement session lifecycle management
    - Create new sessions with unique IDs and TTL (30 min inactivity)
    - Load existing sessions by phone number (GSI-1 lookup)
    - Expire sessions after 30 minutes of inactivity
    - Preserve client identity across session boundaries while clearing context
    - _Requirements: 3.1, 3.4, 3.5_

  - [ ] 5.2 Implement conversation turn storage and context windowing
    - Store conversation turns with composite key (SESSION#, TURN#)
    - Enforce maximum 50 turns per session (evict oldest when exceeded)
    - Build context window for Bedrock prompts from stored turns
    - Resolve pronoun references and implicit entities from history
    - _Requirements: 3.2, 3.3, 3.6_

  - [ ]* 5.3 Write property test for context window bounded storage (Property 5)
    - **Property 5: Context Window Bounded Storage**
    - Generate sessions with N turns (N > 50) and verify at most 50 are stored, retaining most recent
    - **Validates: Requirements 3.3**

  - [ ]* 5.4 Write property test for session lifecycle (Property 6)
    - **Property 6: Session Lifecycle Correctness**
    - Generate sessions with varying inactivity periods and verify expiration at 30 min, context clearing on new session
    - **Validates: Requirements 3.4, 3.5**

  - [ ]* 5.5 Write unit tests for Context Manager
    - Test session creation and lookup
    - Test TTL calculation
    - Test pronoun resolution with specific conversation examples
    - _Requirements: 3.1, 3.2, 3.6_

- [ ] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement Financial Query Service
  - [ ] 7.1 Implement account movement queries
    - Query bank core system API for transactions by account and date range
    - Default to last 30 days when no date range specified
    - Format transactions with date, description, amount, currency, and running balance
    - Implement pagination for results exceeding 10 transactions
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [ ] 7.2 Implement spending analysis and categorization
    - Categorize transactions by spending category
    - Calculate spending distribution with amounts and percentages
    - Identify top 5 spending categories
    - Default to current calendar month when no period specified
    - Compare with previous period and flag changes > 20%
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ] 7.3 Implement product status queries
    - Retrieve product status from bank core system
    - Support savings, checking, investment, credit, and CD products
    - Present product list when no specific product specified
    - Include current value, status, interest rate, and maturity date
    - Detect and indicate data staleness (> 5 minutes)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 7.4 Write property test for transaction formatting (Property 7)
    - **Property 7: Transaction Formatting Completeness**
    - Generate valid `Transaction` objects and verify formatted output contains date, description, amount, currency, and balance
    - **Validates: Requirements 4.3**

  - [ ]* 7.5 Write property test for pagination threshold (Property 8)
    - **Property 8: Pagination Threshold Enforcement**
    - Generate result sets of varying sizes and verify pagination at > 10 transactions
    - **Validates: Requirements 4.4**

  - [ ]* 7.6 Write property test for spending distribution math (Property 9)
    - **Property 9: Spending Distribution Mathematical Correctness**
    - Generate categorized transactions and verify category amounts sum to total and percentages sum to 100%
    - **Validates: Requirements 5.1**

  - [ ]* 7.7 Write property test for top-N category selection (Property 10)
    - **Property 10: Top-N Category Selection Correctness**
    - Generate distributions with K ≥ 5 categories and verify top 5 are highest by amount in descending order
    - **Validates: Requirements 5.3**

  - [ ]* 7.8 Write property test for trend comparison threshold (Property 11)
    - **Property 11: Trend Comparison Threshold Detection**
    - Generate two-period spending data and verify only changes > 20% are flagged as significant
    - **Validates: Requirements 5.4**

  - [ ]* 7.9 Write property test for product status formatting (Property 12)
    - **Property 12: Product Status Formatting Completeness**
    - Generate valid `ProductStatus` objects and verify formatted output includes value, status, rate, and maturity date when applicable
    - **Validates: Requirements 6.4**

  - [ ]* 7.10 Write property test for data staleness detection (Property 13)
    - **Property 13: Data Staleness Detection**
    - Generate product responses with varying `lastUpdated` timestamps and verify staleness indicator at > 5 minutes
    - **Validates: Requirements 6.5**

  - [ ]* 7.11 Write unit tests for Financial Query Service
    - Test default date range application (last 30 days)
    - Test default period application (current calendar month)
    - Test error handling when bank core system is unavailable
    - Test balance query formatting
    - _Requirements: 4.2, 4.5, 5.2, 6.5_

- [ ] 8. Implement Operation Request Service
  - [ ] 8.1 Implement operational request parameter collection and validation
    - Collect required parameters through conversational interaction (source, destination, amount, currency, description)
    - Validate balance sufficiency before generating request
    - Generate operation summary for user confirmation
    - _Requirements: 7.1, 7.2, 7.4_

  - [ ] 8.2 Implement operational request submission
    - Submit confirmed requests to bank transactional system API
    - Return confirmation number and expected processing time
    - Handle submission failures with informative error messages
    - _Requirements: 7.3, 7.5, 7.6_

  - [ ]* 8.3 Write property test for operation summary completeness (Property 14)
    - **Property 14: Operation Summary Completeness**
    - Generate complete operation parameters and verify summary contains all values
    - **Validates: Requirements 7.2**

  - [ ]* 8.4 Write property test for balance validation (Property 15)
    - **Property 15: Balance Validation Correctness**
    - Generate (amount, balance) pairs and verify validation passes iff amount ≤ balance
    - **Validates: Requirements 7.4**

  - [ ]* 8.5 Write unit tests for Operation Request Service
    - Test parameter collection flow
    - Test insufficient balance rejection
    - Test submission failure handling
    - _Requirements: 7.1, 7.4, 7.6_

- [ ] 9. Implement Secure Link Generator
  - [ ] 9.1 Implement HMAC-SHA256 signed URL generation
    - Generate unique link identifiers
    - Encode operation context (type, parameters, session reference) into URL
    - Compute HMAC-SHA256 signature over link parameters using KMS-managed keys
    - Set expiration timestamp to 10 minutes from creation
    - Store link metadata in DynamoDB Secure Links Table
    - _Requirements: 8.1, 8.2, 8.3, 8.5_

  - [ ] 9.2 Implement secure link validation
    - Validate HMAC-SHA256 signature integrity
    - Check expiration timestamp
    - Detect parameter tampering
    - Mark links as used after successful validation
    - Handle expired link notification flow
    - _Requirements: 8.4, 8.5, 8.6_

  - [ ]* 9.3 Write property test for secure link generation (Property 16)
    - **Property 16: Secure Link Generation Structural Correctness**
    - Generate link requests and verify output contains unique ID, valid HMAC-SHA256 signature, and expiration at exactly 10 minutes
    - **Validates: Requirements 8.1, 8.3**

  - [ ]* 9.4 Write property test for secure link context round-trip (Property 17)
    - **Property 17: Secure Link Context Round-Trip**
    - Generate operation contexts, encode into link, decode from link, and verify equivalence
    - **Validates: Requirements 8.2**

  - [ ]* 9.5 Write property test for secure link validation (Property 18)
    - **Property 18: Secure Link Validation Rejects Invalid Links**
    - Generate links with expired timestamps or tampered parameters and verify rejection; verify valid links pass
    - **Validates: Requirements 8.4, 8.5**

  - [ ]* 9.6 Write unit tests for Secure Link Generator
    - Test key rotation handling
    - Test expired link notification flow
    - Test link status transitions (active → used, active → expired)
    - _Requirements: 8.3, 8.4, 8.5_

- [ ] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Implement Tool Orchestrator (Step Functions)
  - [ ] 11.1 Implement workflow decomposition and execution engine
    - Define Step Functions state machine for multi-step workflows
    - Implement sequential tool invocation with output-to-input mapping
    - Enforce maximum 10 tool invocations per request
    - Implement retry policies with exponential backoff per step
    - Handle workflow pausing for authentication (secure link trigger)
    - _Requirements: 9.1, 9.2, 9.5, 9.6_

  - [ ] 11.2 Implement workflow failure handling and status reporting
    - Implement compensating transactions for failed steps (saga pattern)
    - Report progress after each completed step
    - Offer retry/abort options on failure
    - _Requirements: 9.3, 9.4_

  - [ ]* 11.3 Write property test for workflow output chaining (Property 19)
    - **Property 19: Workflow Sequential Output Chaining**
    - Generate multi-step workflows and verify output of step i is correctly passed as input to step i+1
    - **Validates: Requirements 9.2**

  - [ ]* 11.4 Write property test for workflow status reporting (Property 20)
    - **Property 20: Workflow Execution Status Reporting**
    - Generate workflow executions with varying success/failure patterns and verify status reports
    - **Validates: Requirements 9.3, 9.4**

  - [ ]* 11.5 Write property test for workflow invocation limit (Property 21)
    - **Property 21: Workflow Maximum Invocation Limit**
    - Generate workflows with > 10 steps and verify execution terminates at 10 invocations
    - **Validates: Requirements 9.5**

  - [ ]* 11.6 Write unit tests for Tool Orchestrator
    - Test compensating transaction execution on failure
    - Test authentication pause and resume flow
    - Test maximum invocation enforcement
    - _Requirements: 9.3, 9.5, 9.6_

- [ ] 12. Implement Traceability Service
  - [ ] 12.1 Implement audit record creation and storage
    - Record interaction events (timestamp, content, intent, confidence)
    - Record tool invocation events (tool name, input, output, duration, status)
    - Record operational request events (parameters, identity, approval, completion)
    - Generate unique correlation ID per session linking all records
    - Store in DynamoDB Audit Trail Table with 90-day TTL
    - _Requirements: 10.1, 10.2, 10.3, 10.6_

  - [ ] 12.2 Implement audit archival and query capabilities
    - Set up DynamoDB Streams → Lambda → S3 archival pipeline
    - Implement query filtering by client ID, date range, operation type, session ID
    - Configure S3 lifecycle for 7-year retention
    - Set up Amazon Athena for querying archived records
    - _Requirements: 10.4, 10.5_

  - [ ]* 12.3 Write property test for audit record completeness (Property 22)
    - **Property 22: Audit Record Completeness**
    - Generate events of each type and verify required fields are present per type
    - **Validates: Requirements 10.1, 10.2, 10.3**

  - [ ]* 12.4 Write property test for audit query filter correctness (Property 23)
    - **Property 23: Audit Query Filter Correctness**
    - Generate audit record sets and filter combinations, verify results match ALL criteria
    - **Validates: Requirements 10.5**

  - [ ]* 12.5 Write property test for session correlation ID uniqueness (Property 24)
    - **Property 24: Session Correlation ID Uniqueness**
    - Generate multiple sessions and verify distinct correlation IDs; verify all records in a session share the same ID
    - **Validates: Requirements 10.6**

  - [ ]* 12.6 Write unit tests for Traceability Service
    - Test DynamoDB Streams archival trigger
    - Test query with multiple filter combinations
    - Test TTL configuration correctness
    - _Requirements: 10.4, 10.5_

- [ ] 13. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 14. Implement Observability, Security, and Cross-Cutting Concerns
  - [ ] 14.1 Implement structured logging and distributed tracing
    - Configure structured log format with request ID, latency, status, component name
    - Integrate AWS X-Ray for distributed tracing across all Lambdas
    - Implement sensitive data masking (account numbers show last 4 digits only, balances omitted)
    - _Requirements: 13.1, 13.3, 14.4_

  - [ ] 14.2 Implement CloudWatch metrics, alarms, and dashboard
    - Publish custom metrics: message throughput, latency percentiles, error rates, active sessions
    - Configure alarm for error rate > 5% over 5-minute window
    - Configure alarm for average latency > 5 seconds over 5-minute window
    - Create operational dashboard with key health metrics
    - _Requirements: 13.2, 13.4, 13.5, 13.6_

  - [ ] 14.3 Implement security controls
    - Configure KMS encryption for all DynamoDB tables and S3 buckets (AES-256)
    - Enforce TLS 1.2+ for all inter-component communication
    - Define IAM roles with least-privilege policies for each Lambda
    - Implement session token issuance with 30-minute maximum validity
    - _Requirements: 14.1, 14.2, 14.3, 14.5, 14.6_

  - [ ] 14.4 Implement financial response disclaimer injection
    - Add disclaimer to all responses containing financial data
    - Disclaimer states information is for reference and official records available through bank portals
    - _Requirements: 11.6_

  - [ ]* 14.5 Write property test for financial disclaimer inclusion (Property 25)
    - **Property 25: Financial Response Disclaimer Inclusion**
    - Generate responses containing financial data and verify disclaimer is present
    - **Validates: Requirements 11.6**

  - [ ]* 14.6 Write property test for structured log completeness (Property 26)
    - **Property 26: Structured Log Completeness**
    - Generate request processing events and verify log entries contain request ID, latency, status, component
    - **Validates: Requirements 13.1**

  - [ ]* 14.7 Write property test for alarm threshold correctness (Property 27)
    - **Property 27: Alarm Threshold Correctness**
    - Generate metric time series and verify alarms fire at error rate > 5% or latency > 5s, and not otherwise
    - **Validates: Requirements 13.4, 13.5**

  - [ ]* 14.8 Write property test for sensitive data masking (Property 28)
    - **Property 28: Sensitive Data Masking**
    - Generate account numbers and verify masked output shows only last 4 digits; verify balances are omitted from logs
    - **Validates: Requirements 14.4**

  - [ ]* 14.9 Write property test for session token expiration (Property 29)
    - **Property 29: Session Token Expiration Correctness**
    - Generate session tokens and verify expiration is at most 30 minutes from issuance
    - **Validates: Requirements 14.5**

  - [ ]* 14.10 Write unit tests for observability and security
    - Test data masking with various account number formats
    - Test alarm threshold edge cases
    - Test IAM policy validation
    - _Requirements: 14.4, 13.4, 14.3_

- [ ] 15. Integration wiring and Conversational Agent orchestration
  - [ ] 15.1 Implement Conversational Agent Lambda (core orchestration loop)
    - Implement ReAct (Reason + Act) agentic loop with Bedrock Claude
    - Wire NLU Engine, Context Manager, and Tool Orchestrator together
    - Implement tool selection logic based on identified intents
    - Format final responses using Bedrock with tool results
    - Implement graceful degradation when components are unavailable
    - _Requirements: 2.2, 2.3, 2.4, 9.1, 12.4_

  - [ ] 15.2 Wire SQS trigger and end-to-end message flow
    - Configure SQS trigger on Conversational Agent Lambda
    - Wire Gateway → SQS → Agent → Gateway response path
    - Implement Dead Letter Queue routing for failed messages
    - Configure circuit breaker for external service calls
    - _Requirements: 1.1, 1.2, 12.1, 12.2, 12.3_

  - [ ] 15.3 Implement auto-scaling and availability configuration
    - Configure Lambda concurrency for 10,000 concurrent clients
    - Set up multi-AZ deployment for DynamoDB and Lambda
    - Configure auto-scaling triggers at 80% capacity
    - Validate 99.9% availability architecture
    - _Requirements: 1.6, 12.1, 12.2, 12.3, 12.5, 12.6_

  - [ ]* 15.4 Write integration tests for end-to-end message flow
    - Test full conversation flow: greeting → query → response
    - Test multi-step workflow: transfer → parameters → confirmation → secure link
    - Test session expiration and re-engagement
    - Test graceful degradation scenarios
    - _Requirements: 1.1, 1.2, 9.1, 12.4_

- [ ] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 17. Implement Message Deduplication Service
  - [ ] 17.1 Implement deduplication key computation
    - Compute deduplication key from WhatsApp message ID combined with SHA-256 content hash
    - Implement `DeduplicationCheck` interface with key composition logic
    - Handle edge cases: empty messages, binary content, special characters
    - _Requirements: 21.1, 21.4_

  - [ ] 17.2 Implement DynamoDB deduplication record storage with 5-minute TTL
    - Create DynamoDB operations for `DeduplicationRecord` CRUD
    - Configure TTL attribute set to 5 minutes from receipt (covers SQS FIFO dedup window)
    - Store original response for returning on duplicate detection
    - Track duplicate count per message
    - _Requirements: 21.2, 21.5_

  - [ ] 17.3 Implement SQS FIFO queue configuration with content-based deduplication
    - Configure SQS FIFO queue with `ContentBasedDeduplication` enabled
    - Set `MessageGroupId` to client phone number for per-client ordering
    - Configure deduplication scope at message group level
    - Update CDK/SAM infrastructure definitions for FIFO queue
    - _Requirements: 21.3_

  - [ ] 17.4 Wire deduplication into Gateway → SQS flow (replace standard SQS with FIFO)
    - Integrate deduplication check into WhatsApp Gateway Lambda before enqueue
    - Replace standard SQS queue with FIFO queue in message flow
    - Return cached response immediately for detected duplicates
    - Record duplicate detection events in Traceability Service
    - _Requirements: 21.2, 21.3, 21.6_

  - [ ]* 17.5 Write property test for message duplicate detection (Property 36)
    - **Property 36: Message Duplicate Detection**
    - Generate inbound messages and verify deduplication key computation from WhatsApp message ID + content hash
    - Verify duplicate messages within 5-minute window are classified as duplicates and original response is returned
    - Verify messages with different content hashes are NOT classified as duplicates
    - **Validates: Requirements 21.1, 21.2, 21.4**

  - [ ]* 17.6 Write unit tests for deduplication service
    - Test deduplication key computation with various message types
    - Test TTL configuration correctness (5-minute window)
    - Test SQS FIFO queue deduplication configuration
    - Test duplicate count tracking
    - Test cached response return on duplicate detection
    - _Requirements: 21.1, 21.2, 21.3, 21.5_

- [ ] 18. Implement Fast Path Router
  - [ ] 18.1 Implement lightweight pattern matching engine with configurable regex/keyword patterns
    - Implement `FastPathPattern` matching logic with regex and keyword support
    - Implement confidence scoring for pattern matches
    - Support parameter extraction from matched messages (account numbers, transaction counts)
    - Load patterns from SSM Parameter Store
    - _Requirements: 16.1, 16.2, 16.5_

  - [ ] 18.2 Implement routing decision logic (confidence >= 0.9 → fast path, else standard)
    - Implement `FastPathRoutingDecision` generation based on confidence threshold
    - Route messages with confidence ≥ 0.9 to fast path (Financial Query Service directly)
    - Route messages with confidence < 0.9 to standard orchestration chain
    - Track routing time in milliseconds
    - _Requirements: 16.1, 16.4_

  - [ ] 18.3 Wire fast path directly to Financial Query Service (bypass NLU + orchestration)
    - Connect Fast Path Router output to Financial Query Service Lambda
    - Bypass NLU Engine and Tool Orchestrator for fast path requests
    - Ensure response delivery within 2 seconds end-to-end target
    - Record fast path invocations in Traceability Service with full audit detail
    - _Requirements: 16.1, 16.3, 16.6_

  - [ ] 18.4 Implement SSM Parameter Store integration for pattern configuration without code deployment
    - Store `FastPathConfig` (patterns, confidence threshold) in SSM Parameter Store
    - Implement hot-reload of patterns without Lambda redeployment
    - Support adding/modifying/disabling patterns through parameter updates
    - _Requirements: 16.5_

  - [ ]* 18.5 Write property test for fast path routing correctness (Property 31)
    - **Property 31: Fast Path Routing Correctness**
    - Generate inbound messages with varying pattern match confidence scores
    - Verify messages with confidence ≥ 0.9 are routed to Financial Query Service without NLU invocation
    - Verify messages with confidence < 0.9 are routed through standard orchestration chain
    - **Validates: Requirements 16.1, 16.2, 16.4**

  - [ ]* 18.6 Write unit tests for fast path router
    - Test pattern matching with Spanish balance check phrases ("mi saldo", "cuánto tengo")
    - Test pattern matching with transaction queries ("últimos movimientos", "últimas 5 transacciones")
    - Test confidence threshold boundary cases (0.89 vs 0.90 vs 0.91)
    - Test SSM Parameter Store pattern loading and hot-reload
    - Test parameter extraction from matched messages
    - _Requirements: 16.1, 16.2, 16.4, 16.5_

- [ ] 19. Implement Fallback Response Engine
  - [ ] 19.1 Implement Bedrock health check mechanism (10-second interval)
    - Implement periodic health check invocation against Bedrock endpoint
    - Track consecutive failures and response times
    - Emit `HealthCheckResult` with status (healthy/unhealthy/degraded)
    - Configure CloudWatch scheduled events for 10-second health check interval
    - _Requirements: 15.6_

  - [ ] 19.2 Implement fallback activation/deactivation logic (30-second thresholds)
    - Implement `FallbackState` management with activation/deactivation timing
    - Activate fallback within 30 seconds of failure detection
    - Deactivate fallback and resume full processing within 30 seconds of recovery detection
    - Emit CloudWatch metrics for fallback state transitions
    - _Requirements: 15.1, 15.4, 15.6_

  - [ ] 19.3 Implement template library with 20+ response templates for common queries
    - Create response templates for balance inquiries, last transactions, product status
    - Create templates for greetings, general information, and error responses
    - Ensure minimum 20 templates covering most frequent simple query types
    - Store templates in DynamoDB with priority-based pattern matching
    - _Requirements: 15.2, 15.5_

  - [ ] 19.4 Implement template rendering with data source integration (balance, transactions, product status)
    - Implement `FallbackResponse` generation with template variable substitution
    - Integrate with Financial Query Service for real-time data (balance, transactions, product status)
    - Always include limited mode disclaimer in fallback responses
    - Calculate match confidence for template selection
    - _Requirements: 15.2, 15.3_

  - [ ] 19.5 Wire fallback engine into Conversational Agent (activate when Bedrock unhealthy)
    - Integrate fallback activation check into Conversational Agent Lambda
    - Route messages to Fallback Response Engine when `FallbackState.isActive` is true
    - Resume standard NLU processing when fallback deactivates
    - Record fallback mode transitions in Traceability Service
    - _Requirements: 15.1, 15.4_

  - [ ]* 19.6 Write property test for fallback activation and recovery timing (Property 30)
    - **Property 30: Fallback Activation and Recovery Timing**
    - Generate health check failure sequences and verify fallback activates within 30 seconds
    - Generate recovery sequences and verify fallback deactivates within 30 seconds
    - Verify health checks occur every 10 seconds
    - **Validates: Requirements 15.1, 15.4, 15.6**

  - [ ]* 19.7 Write unit tests for fallback response engine
    - Test template library completeness (verify ≥ 20 templates)
    - Test template rendering with variable substitution
    - Test limited mode disclaimer inclusion in all fallback responses
    - Test health check interval configuration (10 seconds)
    - Test activation/deactivation timing boundaries
    - _Requirements: 15.1, 15.2, 15.3, 15.5, 15.6_

- [ ] 20. Implement Idempotency for Financial Operations
  - [ ] 20.1 Implement idempotency key generation (unique per confirmed operation)
    - Generate unique idempotency keys upon operation confirmation
    - Include idempotency key in operation confirmation provided to Bank_Client
    - Implement key format: UUID v4 combined with client ID and operation hash
    - _Requirements: 17.1, 17.5_

  - [ ] 20.2 Implement DynamoDB idempotency key storage with 24-hour TTL
    - Create DynamoDB operations for `IdempotencyRecord` storage
    - Configure TTL attribute set to 24 hours from creation
    - Store operation result alongside idempotency key for duplicate response
    - Implement GSI for client-based lookup of recent operations
    - _Requirements: 17.3_

  - [ ] 20.3 Implement duplicate detection logic in Operation Request Service
    - Check idempotency key existence before operation execution
    - Return original operation result for duplicate submissions without re-processing
    - Record duplicate submission attempts in Traceability Service
    - Handle concurrent duplicate submissions with DynamoDB conditional writes
    - _Requirements: 17.2, 17.4, 17.6_

  - [ ] 20.4 Wire idempotency check into operation submission flow
    - Integrate idempotency check as first step in Operation Request Service submission
    - Generate idempotency key after user confirmation, before bank API call
    - Store result after successful operation execution
    - Ensure at-least-once delivery semantics are handled correctly
    - _Requirements: 17.1, 17.2, 17.4_

  - [ ]* 20.5 Write property test for idempotency exactly-once processing (Property 32)
    - **Property 32: Idempotency Exactly-Once Processing**
    - Generate confirmed operations and submit N times (N ≥ 1) with same idempotency key
    - Verify operation executes exactly once and all subsequent submissions return original result
    - Verify different idempotency keys result in independent executions
    - **Validates: Requirements 17.1, 17.2, 17.4**

  - [ ]* 20.6 Write unit tests for idempotency service
    - Test idempotency key generation uniqueness
    - Test duplicate detection with existing key
    - Test TTL configuration (24-hour expiration)
    - Test concurrent duplicate submission handling
    - Test idempotency key inclusion in confirmation response
    - _Requirements: 17.1, 17.2, 17.3, 17.5_

- [ ] 21. Implement Bank Core API Integration Resilience
  - [ ] 21.1 Implement mTLS/OAuth2 authentication with Secrets Manager integration
    - Implement mTLS certificate loading from AWS Secrets Manager
    - Implement OAuth2 client credentials flow with token rotation
    - Configure automatic token refresh before expiration
    - Support both authentication methods based on endpoint configuration
    - _Requirements: 20.1_

  - [ ] 21.2 Implement exponential backoff with jitter for rate limit (HTTP 429) handling
    - Implement `RateLimitBackoff` logic with exponential intervals
    - Add random jitter to prevent thundering herd effect
    - Configure max 3 retries with base backoff 1000ms and max backoff 30000ms
    - Report unavailability to user after all retries exhausted
    - _Requirements: 20.2_

  - [ ] 21.3 Implement circuit breaker (5 failures → open, 30s recovery → half-open)
    - Implement `CircuitBreakerState` management (closed → open → half-open → closed)
    - Open circuit after 5 consecutive failures
    - Transition to half-open after 30 seconds, allow single test request
    - Close circuit after successful test request in half-open state
    - Emit CloudWatch metrics for circuit state transitions
    - _Requirements: 20.5_

  - [ ] 21.4 Implement API version negotiation and concurrent version support (30-day transition)
    - Include API version headers in all Bank Core API requests
    - Handle version deprecation responses gracefully
    - Support concurrent communication with current and previous API versions
    - Maintain 30-day minimum transition period for version changes
    - _Requirements: 20.3, 20.4_

  - [ ] 21.5 Implement DynamoDB response cache for non-transactional endpoints (5-minute TTL)
    - Create DynamoDB operations for `API Response Cache Table`
    - Cache non-sensitive, non-transactional responses (product catalogs, exchange rates)
    - Configure 5-minute TTL for cached responses
    - Include `fromCache` flag and `cachedAt` timestamp in responses
    - _Requirements: 20.7_

  - [ ]* 21.6 Write property test for rate limit backoff and circuit breaker correctness (Property 35)
    - **Property 35: Bank Core API Rate Limit Backoff and Circuit Breaker Correctness**
    - Generate HTTP 429 response sequences and verify exponential backoff with jitter (each interval ≥ previous × multiplier)
    - Verify max 3 retries before reporting unavailability
    - Generate 5 consecutive failure sequences and verify circuit opens
    - Verify circuit transitions to half-open after 30 seconds
    - **Validates: Requirements 20.2, 20.5**

  - [ ]* 21.7 Write unit tests for bank core API integration
    - Test mTLS certificate loading from Secrets Manager
    - Test OAuth2 token rotation and refresh
    - Test exponential backoff calculation with jitter
    - Test circuit breaker state transitions
    - Test API version header inclusion
    - Test response cache TTL and invalidation
    - Test unavailability notification after 60 seconds
    - _Requirements: 20.1, 20.2, 20.3, 20.5, 20.6, 20.7_

- [ ] 22. Implement Lambda Cold Start Mitigation
  - [ ] 22.1 Configure provisioned concurrency for WhatsApp Gateway (min 5 instances)
    - Configure Lambda provisioned concurrency for WhatsApp Gateway at minimum 5 instances
    - Set up always-on (24/7) provisioned concurrency schedule
    - Configure peak provisioned concurrency at 20 instances
    - _Requirements: 18.1_

  - [ ] 22.2 Configure provisioned concurrency for Conversational Agent (min 5 instances)
    - Configure Lambda provisioned concurrency for Conversational Agent at minimum 5 instances
    - Set up always-on (24/7) provisioned concurrency schedule
    - Configure peak provisioned concurrency at 30 instances
    - Maintain minimum instances during low-traffic periods (midnight to 6 AM)
    - _Requirements: 18.2, 18.3_

  - [ ] 22.3 Implement CloudWatch scheduled events for pre-peak scaling (15 min ahead)
    - Create CloudWatch scheduled events for anticipated peak periods
    - Implement pre-scaling logic 15 minutes before peak (month-end closings, payroll dates)
    - Configure historical traffic pattern analysis for peak prediction
    - _Requirements: 18.5_

  - [ ] 22.4 Implement cold start rate monitoring metric and alarm (>5% threshold over 15 min)
    - Publish custom CloudWatch metric `ColdStartRate` (cold starts / total invocations)
    - Configure alarm when cold start rate exceeds 5% over 15-minute window
    - Set up alarm notification to operations team
    - Track cold start occurrences per Lambda function
    - _Requirements: 18.4_

  - [ ]* 22.5 Write property test for cold start rate alarm threshold (Property 33)
    - **Property 33: Cold Start Rate Alarm Threshold**
    - Generate time series of Lambda invocations with varying cold start ratios
    - Verify alarm triggers when cold start rate exceeds 5% over 15-minute window
    - Verify alarm does NOT trigger when rate is at or below 5%
    - **Validates: Requirements 18.4**

  - [ ]* 22.6 Write unit tests for cold start monitoring
    - Test provisioned concurrency configuration validation (min 5 instances)
    - Test pre-peak scaling schedule configuration
    - Test cold start rate metric calculation
    - Test alarm threshold boundary cases (4.9% vs 5.0% vs 5.1%)
    - Test low-traffic period instance maintenance
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

- [ ] 23. Implement Configurable Tool Invocation Limits
  - [ ] 23.1 Implement SSM Parameter Store integration for configurable max invocation limit
    - Store configurable max external invocation limit in SSM Parameter Store (default: 10)
    - Implement hot-reload of limit configuration without code deployment
    - Store warning threshold percentage (default: 80%)
    - _Requirements: 19.1, 19.2_

  - [ ] 23.2 Implement internal vs external step classification in Tool Orchestrator
    - Classify workflow steps as 'internal' (validation, audit logging, context resolution) or 'external'
    - Count only external steps against the configured maximum invocation limit
    - Track both internal and external step counts in `WorkflowExecution`
    - Terminate execution only when external step count reaches configured limit
    - _Requirements: 19.1, 19.3_

  - [ ] 23.3 Implement 80% limit warning notification to user
    - Detect when external step count reaches 80% of configured limit
    - Inform Bank_Client that request is complex and may require simplification
    - Set `limitWarningIssued` flag in workflow execution
    - Continue execution after warning (do not terminate)
    - _Requirements: 19.4_

  - [ ] 23.4 Implement tool invocation analytics logging for limit optimization
    - Log tool invocation counts per workflow type to CloudWatch
    - Identify patterns where configured limit is consistently near capacity (>80%)
    - Generate recommendation alerts for operations team when limit appears insufficient
    - _Requirements: 19.5, 19.6_

  - [ ]* 23.5 Write property test for configurable limit with internal step exclusion (Property 34)
    - **Property 34: Configurable Tool Invocation Limit with Internal Step Exclusion**
    - Generate workflows with mix of internal and external steps
    - Verify only external steps count against configured maximum
    - Verify execution terminates when external step count reaches limit regardless of internal step count
    - Verify different configured limits are respected
    - **Validates: Requirements 19.1, 19.3**

  - [ ]* 23.6 Write unit tests for configurable tool invocation limits
    - Test SSM Parameter Store limit loading and hot-reload
    - Test internal vs external step classification
    - Test 80% warning threshold notification
    - Test limit enforcement with various configurations
    - Test analytics logging for workflow type patterns
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5_

- [ ] 24. Checkpoint - Ensure all new tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 25. Integration wiring for new components
  - [ ] 25.1 Wire Message Deduplication into end-to-end message flow
    - Integrate deduplication check at Gateway Lambda entry point
    - Ensure FIFO queue replaces standard SQS in full message pipeline
    - Validate exactly-once delivery guarantee end-to-end
    - _Requirements: 21.1, 21.2, 21.3_

  - [ ] 25.2 Wire Fast Path Router into SQS → Agent flow
    - Insert Fast Path Router between SQS FIFO trigger and Conversational Agent
    - Route fast path matches directly to Financial Query Service
    - Route non-matches to Conversational Agent for standard processing
    - Validate sub-2-second response time for fast path queries
    - _Requirements: 16.1, 16.3_

  - [ ] 25.3 Wire Fallback Engine into Agent with health check monitoring
    - Integrate health check monitoring into Conversational Agent startup
    - Route messages to Fallback Response Engine when fallback is active
    - Resume standard processing when fallback deactivates
    - Validate activation/deactivation timing (30-second thresholds)
    - _Requirements: 15.1, 15.4, 15.6_

  - [ ] 25.4 Wire Bank Core API Integration into Financial Query Service and Operation Request Service
    - Replace direct bank API calls with Bank Core API Integration layer
    - Integrate circuit breaker, rate limit handling, and response caching
    - Integrate idempotency check into Operation Request Service submission flow
    - Validate resilience patterns work end-to-end
    - _Requirements: 20.1, 20.2, 20.5, 20.7, 17.1, 17.2_

  - [ ] 25.5 Update CDK/SAM infrastructure definitions for new DynamoDB tables (Idempotency, Deduplication, Cache)
    - Add Idempotency Keys Table definition with GSI and TTL configuration
    - Add Deduplication Records Table definition with TTL configuration
    - Add API Response Cache Table definition with TTL configuration
    - Configure KMS encryption for all new tables
    - Update IAM roles with least-privilege access to new tables
    - _Requirements: 17.3, 21.5, 20.7, 14.1, 14.3_

  - [ ]* 25.6 Write integration tests for all new components
    - Test Message Deduplication ↔ SQS FIFO (exactly-once delivery)
    - Test Fast Path Router ↔ Financial Query Service (direct routing latency)
    - Test Fallback Response Engine ↔ Bedrock health check (activation/deactivation timing)
    - Test Bank Core API Integration ↔ Bank Core Systems (mTLS/OAuth2, rate limiting, circuit breaker)
    - Test Idempotency Service ↔ Operation Request Service (duplicate detection)
    - _Requirements: 16.3, 15.6, 20.2, 20.5, 17.2, 21.3_

- [ ] 26. Final checkpoint - Ensure all tests pass including new components
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Python Lambdas: Conversational Agent, NLU Engine, Context Manager, Financial Query Service, Operation Request Service, Secure Link Generator, Traceability Service, Fallback Response Engine, Fast Path Router, Bank Core API Integration, Message Deduplication Service
- TypeScript Lambda: WhatsApp Gateway
- Property-based testing uses Hypothesis (Python) with minimum 100 iterations per property (200 for critical properties: secure link, balance validation, idempotency, deduplication)
- Infrastructure-as-code uses AWS CDK or SAM (to be confirmed during setup)
- New DynamoDB tables added: Idempotency Keys, Deduplication Records, API Response Cache
- Tasks 17-26 implement 7 architectural improvements: Message Deduplication, Fast Path Router, Fallback Response Engine, Idempotency, Bank Core API Resilience, Cold Start Mitigation, Configurable Tool Invocation Limits

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "2.2", "5.1", "5.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "4.1", "5.3", "5.4", "5.5"] },
    { "id": 4, "tasks": ["4.2", "4.3"] },
    { "id": 5, "tasks": ["4.4", "4.5", "4.6", "7.1", "7.2", "7.3"] },
    { "id": 6, "tasks": ["7.4", "7.5", "7.6", "7.7", "7.8", "7.9", "7.10", "7.11", "8.1"] },
    { "id": 7, "tasks": ["8.2", "8.3", "8.4", "8.5", "9.1"] },
    { "id": 8, "tasks": ["9.2", "9.3", "9.4", "9.5", "9.6"] },
    { "id": 9, "tasks": ["11.1", "12.1"] },
    { "id": 10, "tasks": ["11.2", "11.3", "11.4", "11.5", "11.6", "12.2"] },
    { "id": 11, "tasks": ["12.3", "12.4", "12.5", "12.6", "14.1", "14.2", "14.3", "14.4"] },
    { "id": 12, "tasks": ["14.5", "14.6", "14.7", "14.8", "14.9", "14.10"] },
    { "id": 13, "tasks": ["15.1"] },
    { "id": 14, "tasks": ["15.2", "15.3"] },
    { "id": 15, "tasks": ["15.4"] },
    { "id": 16, "tasks": ["17.1", "17.2", "17.3", "18.1", "19.1", "20.1", "21.1", "22.1", "23.1"] },
    { "id": 17, "tasks": ["17.4", "18.2", "18.3", "19.2", "19.3", "20.2", "20.3", "21.2", "21.3", "22.2", "23.2"] },
    { "id": 18, "tasks": ["18.4", "19.4", "19.5", "20.4", "21.4", "21.5", "22.3", "23.3", "23.4"] },
    { "id": 19, "tasks": ["17.5", "17.6", "18.5", "18.6", "19.6", "19.7", "20.5", "20.6", "21.6", "21.7", "22.4", "22.5", "22.6", "23.5", "23.6"] },
    { "id": 20, "tasks": ["25.1", "25.2", "25.3", "25.4", "25.5"] },
    { "id": 21, "tasks": ["25.6"] }
  ]
}
```
