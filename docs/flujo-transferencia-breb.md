# Flujo Completo: Transferencia BRE-B con OTP

Documentación detallada del flujo end-to-end de una transferencia BRE-B, incluyendo cada servicio AWS involucrado, la secuencia de comunicación y el detalle interno de la Step Function.

---

## Escenario

El cliente envía por WhatsApp (texto o nota de voz):

> *"Quiero transferir 10 mil pesos a la llave 1021803076"*

---

## Fase 1 — Ingesta del mensaje

El mensaje entra al sistema a través de la cadena Twilio → API Gateway → Webhook Receiver → SQS FIFO.

```mermaid
sequenceDiagram
    participant BC as Bank_Client (WhatsApp)
    participant TW as Twilio
    participant APIGW as API Gateway
    participant WHR as Webhook_Receiver (Lambda)
    participant SM as Secrets Manager
    participant SQS as SQS inbound-messages.fifo

    BC->>TW: Mensaje WhatsApp (texto o audio)
    TW->>APIGW: POST /webhook/twilio (form-urlencoded)
    APIGW->>WHR: Invoke Lambda (sync)
    WHR->>SM: GetSecretValue (Twilio Auth Token)
    WHR->>WHR: Validar X-Twilio-Signature
    WHR->>SQS: SendMessage(MessageGroupId=phone, DeduplicationId=MessageSid)
    WHR-->>APIGW: 200 OK (latencia < 1s)
    APIGW-->>TW: 200 OK
    Note over TW: Twilio satisfecho — no reintenta
```

### Servicios involucrados

| # | Servicio | Red | Acción |
|---|----------|-----|--------|
| 1 | Twilio | Externo | Recibe mensaje WhatsApp, envía webhook POST |
| 2 | API Gateway (HTTP API) | Borde público AWS | Rutea POST /webhook/twilio a Lambda |
| 3 | Webhook_Receiver (Lambda) | Fuera de VPC | Valida firma X-Twilio-Signature, encola mensaje |
| 4 | Secrets Manager | Managed | Provee Twilio Auth Token para validar firma |
| 5 | SQS FIFO (inbound-messages) | Managed | Almacena mensaje, dedup por MessageSid, orden por phone |

---

## Fase 2 — Procesamiento, transcripción y AI Agent

El Message_Processor se activa por SQS, valida consent/auth, transcribe audio si aplica, e invoca al AI Agent.

```mermaid
sequenceDiagram
    participant SQS as SQS inbound-messages.fifo
    participant MP as Message_Processor (Lambda)
    participant DDB_C as DynamoDB Consent_Store
    participant DDB_A as DynamoDB Auth_Session
    participant S3A as S3 Audio_Temp_Bucket
    participant TS as Amazon Transcribe
    participant AI as AI Agent / Strands (Lambda)
    participant GR as Bedrock Guardrails
    participant BR as Amazon Bedrock (Claude Haiku 3.5)

    SQS->>MP: Event Source Mapping (batch=1)
    MP->>DDB_C: GetItem(phone) → consentimiento aceptado ✓
    
    alt Si el mensaje es nota de voz
        MP->>S3A: PutObject (audio .ogg)
        MP->>TS: StartTranscriptionJob (es-CO, OGG/Opus)
        TS-->>MP: Texto transcrito
        MP->>S3A: DeleteObject (cleanup)
    end

    MP->>DDB_A: GetItem(phone) → Auth_Session activa (TTL < 30m) ✓
    MP->>AI: Invoke(sessionId, inputText, phoneNumber)
    AI->>GR: Evaluar input contra guardrails
    GR-->>AI: Input aprobado ✓
    AI->>BR: Converse (system prompt + tools + historial de sesión)
    BR-->>AI: Intención identificada: transferencia BRE-B
    Note over BR: Parámetros extraídos:<br/>destino=1021803076<br/>monto=10000<br/>moneda=COP
```

### Servicios involucrados

| # | Servicio | Red | Acción |
|---|----------|-----|--------|
| 6 | Message_Processor (Lambda) | Fuera de VPC | Orquesta procesamiento completo del mensaje |
| 7 | DynamoDB Consent_Store | Managed | Verifica T&C aceptados |
| 8 | S3 Audio_Temp_Bucket | Managed | Almacena audio temporal (solo si es voz) |
| 9 | Amazon Transcribe | Managed | Transcribe audio a texto es-CO (solo si es voz) |
| 10 | DynamoDB Auth_Session | Managed | Verifica sesión autenticada activa |
| 11 | AI Agent (Lambda, Strands SDK) | Fuera de VPC | Razonamiento conversacional con tools |
| 12 | Bedrock Guardrails | Managed | Filtrado de contenido (input/output) |
| 13 | Amazon Bedrock (Claude Haiku 3.5) | Managed | Modelo fundacional — identifica intención y extrae parámetros |

---

## Fase 3 — Confirmación explícita del usuario

El AI Agent solicita confirmación antes de ejecutar la transferencia (regla del system prompt).

```mermaid
sequenceDiagram
    participant AI as AI Agent (Strands)
    participant BR as Amazon Bedrock (Claude Haiku 3.5)
    participant MP as Message_Processor
    participant TW as Twilio
    participant BC as Bank_Client

    AI-->>MP: Respuesta: resumen + solicitud de confirmación
    MP->>TW: REST API (enviar mensaje WhatsApp)
    TW->>BC: "Voy a transferir $10.000 COP a la llave 1021803076.<br/>¿Confirmas esta transferencia?"
    
    BC->>TW: "Sí, confirmo"
    Note over TW,MP: Repite Fase 1 completa:<br/>Twilio → API GW → Webhook → SQS → Message_Processor
    
    MP->>AI: Invoke(sessionId, "Sí, confirmo", phone)
    AI->>BR: Converse (historial de sesión + "Sí, confirmo")
    BR-->>AI: Decisión: invocar tool initiate_transfer_breb
    Note over AI,BR: Bedrock interpreta la confirmación<br/>en contexto de la transferencia pendiente
```

### Servicios involucrados

| # | Servicio | Acción |
|---|----------|--------|
| — | Misma cadena de Fase 1 | Ingesta del mensaje de confirmación |
| — | AI Agent + Bedrock | Interpreta confirmación, decide invocar tool |

---

## Fase 4 — Inicio del workflow (Step Functions)

El AI Agent invoca la tool `initiate_transfer_breb` que dispara la Step Function.

### Diagrama de arquitectura — AI Agent → Initiator → Step Function (desglose)

```
┌───────────────┐         ┌──────────────────────┐         ┌──────────────────────────────────────────────────────────────────┐
│               │ invoke  │                      │ Start   │                                                                  │
│   AI Agent    │────────►│  transfer_breb       │────────►│              TransferBrebStateMachine                            │
│   (Strands)   │         │  initiator (Lambda)  │Execution│              (Step Functions STANDARD)                            │
│               │◄────────│                      │◄────────│                                                                  │
│               │ response│                      │ {arn,   │  ┌─────────────────────────────────────────────────────────────┐  │
└───────────────┘ {arn,   └──────────────────────┘ RUNNING}│  │                                                             │  │
                  "OTP                                      │  │  ┌──────────────────┐                                       │  │
                  enviado"}                                 │  │  │ ValidateTransfer  │ ← Task (Lambda, VPC)                  │  │
                                                           │  │  │                  │    Valida fondos + destino             │  │
                                                           │  │  └────────┬─────────┘                                       │  │
                                                           │  │           │ OK                                               │  │
                                                           │  │           ▼                                                  │  │
                                                           │  │  ┌──────────────────┐                                       │  │
                                                           │  │  │ GenerateOTP      │ ← Task (Lambda, waitForTaskToken)      │  │
                                                           │  │  │                  │    Genera código, guarda en DynamoDB,  │  │
                                                           │  │  │                  │    envía SMS vía Pinpoint              │  │
                                                           │  │  └────────┬─────────┘                                       │  │
                                                           │  │           │                                                  │  │
                                                           │  │           ▼                                                  │  │
                                                           │  │  ┌──────────────────┐                                       │  │
                                                           │  │  │ ⏸️ PAUSADO       │    Esperando SendTaskSuccess           │  │
                                                           │  │  │ (hasta 5 min)    │    del Message_Processor               │  │
                                                           │  │  └────────┬─────────┘                                       │  │
                                                           │  │           │ OTP validado                                     │  │
                                                           │  │           ▼                                                  │  │
                                                           │  │  ┌──────────────────┐                                       │  │
                                                           │  │  │ ValidateOTP      │ ← Choice                              │  │
                                                           │  │  │                  │    ¿valid == true?                     │  │
                                                           │  │  └────────┬─────────┘                                       │  │
                                                           │  │           │ Sí                                               │  │
                                                           │  │           ▼                                                  │  │
                                                           │  │  ┌──────────────────┐                                       │  │
                                                           │  │  │ ExecuteTransfer  │ ← Task (Lambda, VPC)                  │  │
                                                           │  │  │                  │    Debita origen, acredita destino     │  │
                                                           │  │  └────────┬─────────┘                                       │  │
                                                           │  │           │ OK                                               │  │
                                                           │  │           ▼                                                  │  │
                                                           │  │  ┌──────────────────┐                                       │  │
                                                           │  │  │ Publish          │ ← Parallel                            │  │
                                                           │  │  │ Notifications    │    ├─► SQS email → Email_Service → SES│  │
                                                           │  │  │                  │    └─► SQS sms → SMS_Service → Pinpoint│ │
                                                           │  │  └────────┬─────────┘                                       │  │
                                                           │  │           │                                                  │  │
                                                           │  │           ▼                                                  │  │
                                                           │  │  ┌──────────────────┐                                       │  │
                                                           │  │  │ NotifyUser       │ ← Task (Lambda)                       │  │
                                                           │  │  │ Success          │    Envía comprobante por WhatsApp      │  │
                                                           │  │  └──────────────────┘                                       │  │
                                                           │  │                                                             │  │
                                                           │  └─────────────────────────────────────────────────────────────┘  │
                                                           │                                                                  │
                                                           │  Estados de error (cualquiera termina en notify al cliente):      │
                                                           │  • NotifyValidationFailed (fondos/destino inválido)               │
                                                           │  • NotifyOTPExpired (timeout 5 min)                               │
                                                           │  • NotifyOTPBlocked (3 intentos fallidos)                         │
                                                           │  • NotifyTransferFailed (error en ejecución)                      │
                                                           └──────────────────────────────────────────────────────────────────┘
```

### Flujo de invocación paso a paso

```
AI Agent (Strands)
    │
    │  1. El modelo decide invocar tool: initiate_transfer_breb(source, dest, amount, concept, phone)
    │
    ▼
transfer_breb_initiator (Lambda)
    │
    │  2. stepfunctions.start_execution(
    │         stateMachineArn = TransferBrebStateMachine,
    │         name = correlationId,          ← idempotencia
    │         input = {phone, source, dest, amount, concept, sessionId}
    │     )
    │
    │  3. Retorna INMEDIATAMENTE: {executionArn, status: "RUNNING"}
    │     (NO espera a que termine el workflow)
    │
    ▼
AI Agent recibe respuesta
    │
    │  4. Genera texto: "Te envié un código de verificación por SMS. Escríbelo aquí."
    │     (El agente sabe que NO debe esperar el OTP — instrucción en system prompt)
    │
    ▼
Message_Processor envía respuesta a Twilio → Bank_Client
    │
    │  5. El Message_Processor TERMINA su ejecución.
    │     La Lambda se libera. El workflow sigue corriendo independientemente.
    │
    ▼
Step Functions ejecuta los estados secuencialmente:
    │
    ├─► ValidateTransfer ──► Lambda VPC (valida fondos + destino)
    │
    ├─► GenerateOTP ──► Lambda (genera código, guarda taskToken en DynamoDB, envía SMS)
    │       │
    │       └─► ⏸️ WORKFLOW PAUSADO (waitForTaskToken, max 5 min, costo: $0)
    │
    │   ... el cliente responde el OTP por WhatsApp ...
    │   ... Message_Processor valida y llama SendTaskSuccess ...
    │
    ├─► ValidateOTP ──► Choice (¿válido?)
    │
    ├─► ExecuteTransfer ──► Lambda VPC (debita/acredita, genera receipt)
    │
    ├─► PublishNotifications ──► Parallel:
    │       ├─► SQS email-notification ──► Email_Service ──► SES
    │       └─► SQS sms-notification ──► SMS_Service ──► Pinpoint
    │
    └─► NotifyUserSuccess ──► Lambda (envía comprobante por WhatsApp vía Twilio)
```

### Punto clave: desacople temporal

```
    TIEMPO ──────────────────────────────────────────────────────────────────────►

    │◄── Message_Processor (ejecución 1) ──►│         │◄── Message_Processor (ejecución 2) ──►│
    │  Recibe msg → AI Agent → Initiator    │         │  Recibe OTP → valida → SendTaskSuccess │
    │  → responde "te envié OTP"            │         │                                        │
    │  → Lambda TERMINA                     │         │  → Lambda TERMINA                      │
    │                                       │         │                                        │
    │◄──────────── Step Functions (ejecución continua, puede durar minutos) ──────────────────►│
    │  Validate → OTP → ⏸️ PAUSA ──────────────────── ▶️ RESUME → Execute → Notify            │
    │                                       │         │                                        │
                                     Cliente recibe SMS,
                                     piensa, escribe OTP
```

El `transfer_breb_initiator` es **fire-and-forget**: dispara el workflow y retorna. No hay Lambda bloqueada esperando al usuario. Step Functions absorbe la espera a costo cero.

### Diagrama de secuencia

```mermaid
sequenceDiagram
    participant AI as AI Agent (Strands)
    participant INIT as transfer_breb_initiator (Lambda)
    participant SFN as Step Functions
    participant MP as Message_Processor
    participant TW as Twilio
    participant BC as Bank_Client

    AI->>INIT: Lambda invoke {source, dest, amount, concept, phone}
    INIT->>SFN: StartExecution(name=correlationId, input={...})
    SFN-->>INIT: {executionArn, status: RUNNING}
    INIT-->>AI: {executionArn, message: "OTP enviado por SMS"}
    AI-->>MP: "Te envié un código de verificación por SMS. Escríbelo aquí."
    MP->>TW: REST API (enviar mensaje)
    TW->>BC: "📱 Te envié un código de verificación por SMS. Escríbelo aquí cuando lo recibas."
```

### Servicios involucrados

| # | Servicio | Red | Acción |
|---|----------|-----|--------|
| 14 | transfer_breb_initiator (Lambda) | Fuera de VPC | Dispara el workflow con StartExecution |
| 15 | AWS Step Functions | Managed | Inicia TransferBrebStateMachine (tipo STANDARD) |

---

## Fase 5 — Step Function: Detalle de estados

### Diagrama completo de la máquina de estados

```mermaid
stateDiagram-v2
    [*] --> ValidateTransfer
    
    ValidateTransfer --> GenerateOTP: Validación OK
    ValidateTransfer --> NotifyValidationFailed: INSUFFICIENT_FUNDS / INVALID_DEST
    
    GenerateOTP --> WaitForOTP: OTP enviado por SMS (Pinpoint)
    WaitForOTP --> ValidateOTP: SendTaskSuccess(otp)
    WaitForOTP --> NotifyOTPExpired: Timeout 5 minutos
    
    ValidateOTP --> ExecuteTransfer: OTP válido
    ValidateOTP --> WaitForOTP: OTP inválido (reintentos < 3)
    ValidateOTP --> NotifyOTPBlocked: 3 intentos fallidos
    
    ExecuteTransfer --> PublishNotifications: Transferencia exitosa
    ExecuteTransfer --> NotifyTransferFailed: Error en ejecución
    
    PublishNotifications --> NotifyUserSuccess: Eventos SQS publicados
    
    NotifyUserSuccess --> [*]
    NotifyValidationFailed --> [*]
    NotifyOTPExpired --> [*]
    NotifyOTPBlocked --> [*]
    NotifyTransferFailed --> [*]
```

---

### Estado 1: `ValidateTransfer` (Task — Lambda en VPC)

Valida que la transferencia sea posible: cuenta origen existe, fondos suficientes, destino válido.

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant VAL as transfer_breb_validate (Lambda, VPC)
    participant MOCK as Mock_Core (datos inline)

    SFN->>VAL: Invoke {phone, sourceAccount, destinationAccount, amount}
    VAL->>MOCK: ¿Cuenta origen existe y pertenece al cliente?
    VAL->>MOCK: ¿Saldo disponible >= $10.000?
    VAL->>MOCK: ¿Destino 1021803076 existe?
    MOCK-->>VAL: Todo OK
    VAL-->>SFN: {valid: true, sourceName: "Carlos Rodríguez", destName: "Juan García"}
    Note over SFN: Transición → GenerateOTP
```

| Servicio | Red | Acción |
|----------|-----|--------|
| transfer_breb_validate (Lambda) | **VPC privada** — BankingLambdaSG | Valida contra Mock Core |
| Mock_Core | Inline (datos hardcodeados) | Simula core bancario |

**Si falla:** Transición a `NotifyValidationFailed` → notifica al cliente el error.

---

### Estado 2: `GenerateOTP` (Task — waitForTaskToken)

Genera un OTP, lo persiste con el taskToken, envía SMS. El workflow **SE PAUSA**.

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant OTP as OTP_Service (Lambda, fuera VPC)
    participant DDB as DynamoDB OTP_Store
    participant PIN as AWS Pinpoint
    participant BC as Bank_Client

    SFN->>OTP: Invoke con $$.Task.Token incluido en el payload
    OTP->>OTP: Generar código aleatorio 6 dígitos (ej: 847291)
    OTP->>DDB: PutItem {phone, code=847291, taskToken, attempts=0, ttl=5min}
    OTP->>PIN: SendMessages (SMS: "Tu código BTG: 847291. Válido 5 min.")
    PIN-->>BC: 📱 SMS recibido con código
    OTP-->>SFN: Lambda retorna exitosamente
    
    Note over SFN: ⏸️ WORKFLOW PAUSADO<br/>Esperando SendTaskSuccess o SendTaskFailure<br/>HeartbeatSeconds: 300 (5 min timeout)<br/>Costo mientras espera: $0
```

| Servicio | Red | Acción |
|----------|-----|--------|
| OTP_Service (Lambda) | Fuera de VPC | Genera OTP, persiste, envía SMS |
| DynamoDB OTP_Store | Managed | Almacena: code + taskToken + attempts + TTL 5m |
| AWS Pinpoint | Managed | Envía SMS con el código OTP |

**Estructura del registro en OTP_Store:**

| Campo | Valor |
|-------|-------|
| pk | +573001234567 |
| code | 847291 |
| taskToken | (token largo de Step Functions) |
| executionArn | arn:aws:states:... |
| attempts | 0 |
| transferContext | {amount: 10000, dest: "1021803076"} |
| createdAt | 2026-05-27T15:30:00Z |
| ttl | 1748358900 (epoch + 300s) |

---

### Interrupción: El cliente responde con el OTP

El cliente escribe el código por WhatsApp. El mensaje recorre la cadena de ingesta normal y el Message_Processor detecta que hay un OTP pendiente (prioridad sobre el AI Agent).

```mermaid
sequenceDiagram
    participant BC as Bank_Client
    participant TW as Twilio
    participant APIGW as API Gateway
    participant WHR as Webhook_Receiver
    participant SQS as SQS FIFO
    participant MP as Message_Processor
    participant DDB as DynamoDB OTP_Store
    participant SFN as Step Functions

    BC->>TW: "847291" (mensaje WhatsApp)
    TW->>APIGW: POST /webhook/twilio
    APIGW->>WHR: Invoke
    WHR->>SQS: SendMessage
    WHR-->>APIGW: 200 OK
    SQS->>MP: Event Source Mapping (batch=1)

    MP->>DDB: GetItem(phone) → OTP pendiente encontrado
    Note over MP: PRIORIDAD: OTP callback > AI Agent<br/>No se invoca Strands para este mensaje
    MP->>MP: Comparar "847291" == stored code (847291)
    
    alt ✅ Código correcto
        MP->>SFN: SendTaskSuccess(taskToken, {valid: true})
        MP->>DDB: DeleteItem(phone) — limpiar OTP
        Note over SFN: ▶️ WORKFLOW REANUDADO → ValidateOTP
    else ❌ Código incorrecto (attempts < 3)
        MP->>DDB: UpdateItem(attempts = attempts + 1)
        MP->>TW: "Código incorrecto. Te quedan N intentos."
        TW->>BC: Mensaje de error
        Note over SFN: Sigue pausado esperando
    else 🚫 3 intentos fallidos
        MP->>SFN: SendTaskFailure(taskToken, "OTPBlockedError")
        MP->>DDB: DeleteItem(phone)
        Note over SFN: Workflow → NotifyOTPBlocked
    end
```

| Servicio | Acción |
|----------|--------|
| Cadena de ingesta completa | Twilio → API GW → Webhook → SQS → Message_Processor |
| DynamoDB OTP_Store | Lee OTP pendiente, valida código, actualiza attempts |
| Step Functions | Recibe SendTaskSuccess/Failure → reanuda workflow |

---

### Estado 3: `ValidateOTP` (Choice)

Evalúa el resultado del callback.

```mermaid
flowchart TD
    A["ValidateOTP (Choice State)"] --> B{"$.otpResult.valid == true?"}
    B -->|Sí| C["→ ExecuteTransfer"]
    B -->|No / Default| D["→ NotifyOTPExpired"]
```

---

### Estado 4: `ExecuteTransfer` (Task — Lambda en VPC)

Ejecuta la transferencia contra el Mock Core.

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant EXEC as transfer_breb_execute (Lambda, VPC)
    participant MOCK as Mock_Core

    SFN->>EXEC: Invoke {phone, source, dest, amount, concept, validation}
    EXEC->>MOCK: Debitar $10.000 COP de cuenta origen (Carlos Rodríguez)
    EXEC->>MOCK: Acreditar $10.000 COP a destino 1021803076 (Juan García)
    EXEC->>EXEC: Generar receipt con transactionId único
    EXEC-->>SFN: $.receipt = {transactionId: "TRX-20260527-a1b2c3", amount: 10000, status: "COMPLETED", executedAt: "..."}
    Note over SFN: Transición → PublishNotifications
```

| Servicio | Red | Acción |
|----------|-----|--------|
| transfer_breb_execute (Lambda) | **VPC privada** — BankingLambdaSG | Ejecuta transferencia en Mock Core |
| Mock_Core | Inline | Actualiza saldos simulados |

**Si falla:** Transición a `NotifyTransferFailed`.

---

### Estado 5: `PublishNotifications` (Parallel)

Publica eventos de confirmación a las colas de notificaciones. Ambas ramas se ejecutan en paralelo.

```mermaid
flowchart LR
    SFN["Step Functions<br/>(Parallel State)"] -->|Rama 1| SQS_E["SQS<br/>email-notification<br/>(+DLQ)"]
    SFN -->|Rama 2| SQS_S["SQS<br/>sms-notification<br/>(+DLQ)"]
    
    SQS_E -->|"Event Source Mapping<br/>batch=10"| LEM["Email_Service<br/>(Lambda)"]
    SQS_S -->|"Event Source Mapping<br/>batch=10"| LSM["SMS_Service<br/>(Lambda)"]
    
    LEM -->|SendEmail| SES["Amazon SES"]
    LSM -->|SendMessages| PIN["AWS Pinpoint"]
```

| Servicio | Acción |
|----------|--------|
| Step Functions | PUBLICA eventos a ambas colas (SQS SDK integration nativa) |
| SQS email-notification | Almacena evento `transfer_confirmation` |
| SQS sms-notification | Almacena evento `transfer_confirmation` |
| Email_Service (Lambda, fuera VPC) | CONSUME cola → envía email vía SES |
| SMS_Service (Lambda, fuera VPC) | CONSUME cola → envía SMS vía Pinpoint |
| Amazon SES | Entrega email de confirmación al cliente |
| AWS Pinpoint | Entrega SMS de confirmación al cliente |

---

### Estado 6: `NotifyUserSuccess` (Task — Lambda fuera de VPC)

Envía el comprobante final al cliente por WhatsApp.

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant NOT as message_handler_notify (Lambda)
    participant SM as Secrets Manager
    participant TW as Twilio
    participant BC as Bank_Client

    SFN->>NOT: Invoke {phone, messageType: "transfer_success", receipt}
    NOT->>SM: GetSecretValue (credenciales Twilio)
    NOT->>NOT: Formatear comprobante en español colombiano
    NOT->>TW: REST API (enviar mensaje WhatsApp)
    TW->>BC: "✅ Transferencia exitosa!<br/>$10.000 COP a la llave 1021803076 (Juan García)<br/>ID: TRX-20260527-a1b2c3"
    NOT-->>SFN: Fin del workflow ✓
```

| Servicio | Red | Acción |
|----------|-----|--------|
| message_handler_notify (Lambda) | Fuera de VPC | Formatea y envía comprobante |
| Secrets Manager | Managed | Provee credenciales Twilio |
| Twilio | Externo | Entrega mensaje WhatsApp al cliente |

---

## Estados de error

Cada estado de error termina notificando al cliente vía `message_handler_notify`:

| Estado | Causa | Mensaje al cliente |
|--------|-------|-------------------|
| `NotifyValidationFailed` | Fondos insuficientes o destino inválido | "No pudimos procesar tu transferencia: [razón]" |
| `NotifyOTPExpired` | 5 minutos sin respuesta | "El código de verificación expiró. Inicia la transferencia de nuevo." |
| `NotifyOTPBlocked` | 3 intentos fallidos | "Por seguridad, bloqueamos esta operación. Contacta al banco." |
| `NotifyTransferFailed` | Error en ejecución | "Hubo un error al procesar tu transferencia. Intenta más tarde." |

---

## Resumen: Todos los servicios por fase

| Fase | Servicios AWS | Externos |
|------|--------------|----------|
| **1. Ingesta** | API Gateway, Lambda (Webhook), SQS FIFO, Secrets Manager | Twilio |
| **2. Procesamiento** | Lambda (Message_Processor), DynamoDB ×2, S3, Transcribe, Lambda (AI Agent), Bedrock, Guardrails | — |
| **3. Confirmación** | Misma cadena de ingesta | Twilio |
| **4. Inicio workflow** | Lambda (Initiator), Step Functions | — |
| **5. ValidateTransfer** | Lambda (validate, **VPC privada**) | — |
| **6. GenerateOTP** | Lambda (OTP_Service), DynamoDB OTP_Store, Pinpoint | — |
| **7. Callback OTP** | API Gateway, Lambda (Webhook), SQS, Lambda (Message_Processor), DynamoDB, Step Functions | Twilio |
| **8. ExecuteTransfer** | Lambda (execute, **VPC privada**) | — |
| **9. Notificaciones** | SQS ×2, Lambda ×2, SES, Pinpoint | — |
| **10. Comprobante** | Lambda (notify), Secrets Manager | Twilio |

**Total: 16 servicios AWS + Twilio** en un flujo de transferencia completo.

---

## Segregación de red

| Lambda | Ubicación | Justificación |
|--------|-----------|---------------|
| Webhook_Receiver | Fuera de VPC | Solo valida firma y encola — no toca datos bancarios |
| Message_Processor | Fuera de VPC | Orquesta flujo, llama APIs AWS públicas y Twilio |
| AI Agent (Strands) | Fuera de VPC | Solo Bedrock + Lambda invoke — APIs públicas |
| transfer_breb_initiator | Fuera de VPC | Solo StartExecution de Step Functions |
| OTP_Service | Fuera de VPC | DynamoDB + Pinpoint — APIs públicas |
| Email_Service / SMS_Service | Fuera de VPC | SES + Pinpoint — APIs públicas |
| message_handler_notify | Fuera de VPC | Secrets Manager + Twilio REST |
| **transfer_breb_validate** | **VPC privada** | Dominio bancario — en producción conectará al core real |
| **transfer_breb_execute** | **VPC privada** | Dominio bancario — ejecuta operaciones financieras |
| **balance_query** | **VPC privada** | Dominio bancario — consulta saldos |
| **statement_generator** | **VPC privada** | Dominio bancario — genera extractos, escribe a S3 vía VPC Endpoint |

Las Lambdas en VPC privada corren bajo `BankingLambdaSG` (ingress: ninguno, egress: TCP 443 → VPC Endpoints únicamente). **Sin ruta 0.0.0.0/0** — cero salida a internet.
