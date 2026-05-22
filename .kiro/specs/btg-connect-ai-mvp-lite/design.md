# Technical Design Document

## Overview

BTG ConnectAI MVP Lite es un asistente bancario conversacional serverless que conecta WhatsApp con Amazon Bedrock Agent para ejecutar servicios bancarios en español natural. El sistema soporta entrada multimodal (texto y audio), flujo de consentimiento regulatorio, autenticación vía enlace web, y tres servicios bancarios: consulta de saldos, transferencias BRE-B y generación de extractos PDF.

### Decisiones Arquitectónicas Clave

| Decisión | Elección | Razón |
|----------|----------|-------|
| Runtime | TypeScript (Node.js 20.x) | Tipado fuerte, cold start rápido, Powertools nativo |
| IaC | AWS CDK (TypeScript) | Mismo lenguaje que Lambdas, L2 constructs |
| AI Engine | Amazon Bedrock Agent + Claude Haiku 3.5 | Managed agent con memoria de sesión nativa, bajo costo |
| WhatsApp | AWS End User Messaging Social | Servicio managed, integración nativa con SNS, mensajes interactivos |
| Audio | Amazon Transcribe | Soporte nativo OGG/Opus, español colombiano |
| Deduplicación | DynamoDB conditional writes + TTL | Serverless, free tier, sin estado externo |
| Autenticación | Lambda + DynamoDB (mock vía enlace web) | Simula flujo real con mínima infraestructura |
| Extractos | S3 + envío como documento adjunto WhatsApp | Entrega directa al Bank_Client vía EUMS |
| Observabilidad | Lambda Powertools + CloudWatch | Structured logging JSON, métricas nativas |
| Seguridad | IAM roles + AWS managed keys | Zero cost, sufficient para MVP |

### Flujo de Datos Principal (Happy Path Completo)

```mermaid
sequenceDiagram
    participant BC as Bank_Client (WhatsApp)
    participant EUMS as AWS End User Messaging Social
    participant SNS as Amazon SNS
    participant WG as WhatsApp_Gateway Lambda
    participant DDB_D as DynamoDB (Dedup)
    participant DDB_C as DynamoDB (Consent_Store)
    participant DDB_A as DynamoDB (Auth_Session)
    participant TS as Amazon Transcribe
    participant BA as Bedrock Agent (Claude Haiku 3.5)
    participant GR as Bedrock Guardrails
    participant AG_B as Action_Group: balance-query
    participant AG_T as Action_Group: transfer-breb
    participant AG_S as Action_Group: statement-generator
    participant S3 as Statement_Bucket (S3)

    BC->>EUMS: Mensaje WhatsApp (texto o audio)
    EUMS->>SNS: Notificación incoming message
    SNS->>WG: Trigger Lambda
    WG->>DDB_D: ConditionalPut (dedup check)
    alt Mensaje duplicado
        WG-->>WG: Descartar (return early)
    end
    WG->>DDB_C: GetItem (consent check)
    alt Sin consentimiento
        WG->>EUMS: Enviar mensaje interactivo T&C (botones)
        BC->>EUMS: Acepta T&C (button reply)
        EUMS->>SNS: Button callback
        SNS->>WG: Trigger Lambda
        WG->>DDB_C: PutItem (registrar consentimiento)
        WG->>EUMS: Mensaje de bienvenida + servicios
    end
    alt Audio message
        WG->>TS: StartTranscriptionJob (OGG/Opus)
        TS-->>WG: Transcripción en texto
    end
    WG->>DDB_A: GetItem (auth session check)
    alt Sin Auth_Session activa
        WG->>EUMS: Enviar botón "Iniciar sesión"
        Note over BC,WG: Bank_Client completa login en Login_Page
        Note over WG: Auth_Service crea Auth_Session en DDB_A
        WG->>EUMS: Confirmación de autenticación exitosa
    end
    WG->>BA: InvokeAgent (sessionId, inputText)
    BA->>GR: Evaluar input
    GR-->>BA: Input aprobado
    BA->>AG_B: Invoke balance-query (si aplica)
    BA->>AG_T: Invoke transfer-breb (si aplica)
    BA->>AG_S: Invoke statement-generator (si aplica)
    AG_S->>S3: PutObject (PDF)
    AG_S-->>BA: S3 key del PDF generado
    BA->>GR: Evaluar output
    GR-->>BA: Output aprobado
    BA-->>WG: Respuesta del agente (incluye S3 key del PDF)
    WG->>S3: GetObject (descargar PDF)
    S3-->>WG: PDF binary
    WG->>EUMS: PostWhatsAppMessageMedia (upload PDF)
    EUMS-->>WG: media_id
    WG->>EUMS: SendWhatsAppMessage (document message con media_id)
    EUMS->>BC: Mensaje WhatsApp (documento PDF adjunto)
```

### Flujo de Autenticación (Detalle)

```mermaid
sequenceDiagram
    participant BC as Bank_Client
    participant WA as WhatsApp
    participant WG as WhatsApp_Gateway
    participant LP as Login_Page (S3 Static)
    participant AS as Auth_Service Lambda
    participant DDB as DynamoDB (Auth_Session)

    BC->>WA: "Quiero ver mi saldo"
    WA->>WG: Mensaje entrante
    WG->>DDB: GetItem(phoneNumber)
    DDB-->>WG: No Auth_Session found
    WG->>WA: Mensaje interactivo con botón "Iniciar sesión"
    WA->>BC: Muestra botón
    BC->>WA: Click "Iniciar sesión"
    WA->>BC: Abre URL de Login_Page
    BC->>LP: GET /login?phone=+57300XXXX&callback_token=xyz
    LP-->>BC: Formulario HTML (usuario + contraseña)
    BC->>LP: POST /login (credentials)
    LP->>AS: POST /authenticate (credentials + phone + token)
    AS->>AS: Validar credenciales vs hardcoded users
    AS->>DDB: PutItem(Auth_Session, TTL=30min)
    AS-->>LP: 200 OK (redirect)
    LP-->>BC: "Autenticación exitosa, vuelve a WhatsApp"
    Note over AS,WG: Auth_Service notifica a Gateway (o Gateway polling)
    WG->>WA: "✅ Autenticación exitosa. Procesando tu solicitud..."
    WG->>WG: Procesar solicitud original (consulta de saldo)
```

## Architecture

### Diagrama de Componentes

```mermaid
graph TB
    subgraph "Canal WhatsApp"
        WA[WhatsApp Business Platform]
        EUMS[AWS End User Messaging Social]
    end

    subgraph "Ingestion Layer"
        SNS_IN[SNS Topic - Incoming Messages]
        WG[WhatsApp_Gateway Lambda]
        DDB_DEDUP[DynamoDB - Dedup Table]
        DDB_CONSENT[DynamoDB - Consent_Store]
    end

    subgraph "Authentication Layer"
        LP[Login_Page - S3 Static Site]
        AS[Auth_Service Lambda]
        DDB_AUTH[DynamoDB - Auth_Session]
    end

    subgraph "Audio Processing"
        TRANSCRIBE[Amazon Transcribe]
    end

    subgraph "AI Layer"
        BA[Amazon Bedrock Agent]
        FM[Claude Haiku 3.5 Foundation Model]
        GR[Bedrock Guardrails]
    end

    subgraph "Action Groups"
        AG_BAL[balance-query Lambda]
        AG_TRF[transfer-breb Lambda]
        AG_STM[statement-generator Lambda]
    end

    subgraph "Storage"
        S3_STM[Statement_Bucket - S3]
        MOCK[Mock_Core - Inline Data]
    end

    subgraph "Observability"
        CW_LOGS[CloudWatch Logs]
        CW_DASH[CloudWatch Dashboard]
        CW_ALARM[CloudWatch Alarms]
        SNS_ALARM[SNS Topic - Alarms]
    end

    subgraph "Security"
        SM[Secrets Manager]
        IAM[IAM Roles & Policies]
    end

    WA <--> EUMS
    EUMS --> SNS_IN
    SNS_IN --> WG
    WG --> DDB_DEDUP
    WG --> DDB_CONSENT
    WG --> DDB_AUTH
    WG --> TRANSCRIBE
    WG --> BA
    WG --> EUMS
    LP --> AS
    AS --> DDB_AUTH
    BA --> FM
    BA --> GR
    BA --> AG_BAL
    BA --> AG_TRF
    BA --> AG_STM
    AG_BAL --> MOCK
    AG_TRF --> MOCK
    AG_STM --> MOCK
    AG_STM --> S3_STM
    WG --> CW_LOGS
    AG_BAL --> CW_LOGS
    AG_TRF --> CW_LOGS
    AG_STM --> CW_LOGS
    AS --> CW_LOGS
    CW_LOGS --> CW_DASH
    CW_ALARM --> SNS_ALARM
    WG -.-> SM
    AS -.-> SM
```

### Principios Arquitectónicos

1. **Zero VPC**: Todas las Lambdas acceden a servicios AWS vía endpoints públicos. Elimina costos de VPC Endpoints y reduce cold start.
2. **Stateless Lambdas**: Estado conversacional en Bedrock Agent (memoria nativa). Auth_Session y Consent en DynamoDB con TTL.
3. **Three Action Groups**: `balance-query`, `transfer-breb`, `statement-generator` — cada uno con responsabilidad única.
4. **Mock Data Inline**: Datos bancarios sintéticos hardcodeados en las Lambdas de Action Groups.
5. **Multimodal Input**: Audio transcrito a texto antes de llegar al Bedrock Agent — pipeline transparente.
6. **Consent-First**: Ningún servicio se ejecuta sin consentimiento previo registrado.
7. **Auth-Before-Action**: Operaciones bancarias requieren Auth_Session activa (TTL 30min).
8. **Encryption at Rest by Default**: AWS managed keys — zero cost, zero management.


## Components and Interfaces

### 1. WhatsApp_Gateway Lambda

**Responsabilidad:** Punto de entrada del sistema. Recibe mensajes de WhatsApp (texto, audio, botones interactivos) vía SNS, gestiona flujo de consentimiento, verifica autenticación, ejecuta deduplicación, transcribe audio, invoca al Bedrock Agent, envía respuestas de texto y documentos PDF adjuntos.

**Runtime:** Node.js 20.x (TypeScript)  
**Memory:** 512 MB  
**Timeout:** 60 seconds  
**Trigger:** SNS Topic (incoming WhatsApp messages)

#### Interface de Entrada (SNS Event)

```typescript
interface WhatsAppIncomingEvent {
  Records: Array<{
    Sns: {
      Message: string; // JSON string del payload de EUMS
    };
  }>;
}

interface EUMSIncomingPayload {
  messageId: string;
  whatsAppMessageId: string;
  originationPhoneNumber: string; // E.164
  destinationPhoneNumber: string; // E.164
  messageBody: {
    type: "text" | "audio" | "image" | "video" | "sticker" | "document" | "interactive";
    text?: { body: string };
    audio?: {
      id: string;        // WhatsApp media ID
      mimeType: string;  // "audio/ogg; codecs=opus"
    };
    interactive?: {
      type: "button_reply" | "list_reply";
      button_reply?: {
        id: string;      // Button payload ID
        title: string;
      };
    };
  };
  timestamp: string; // ISO 8601
}
```

#### Lógica Principal

```typescript
async function handler(event: SNSEvent): Promise<void> {
  const correlationId = uuidv4();
  logger.appendKeys({ correlationId });

  const payload = parseIncomingMessage(event);
  const phoneNumber = payload.originationPhoneNumber;

  // 1. Deduplicación
  const isDuplicate = await checkAndStoreDeduplicate(payload.whatsAppMessageId);
  if (isDuplicate) {
    logger.info("Duplicate message discarded", { whatsAppMessageId: payload.whatsAppMessageId });
    return;
  }

  // 2. Verificar consentimiento
  const consent = await getConsent(phoneNumber);
  if (!consent?.accepted) {
    // Manejar flujo de consentimiento (botones interactivos o respuesta a botón)
    await handleConsentFlow(payload, consent);
    return;
  }

  // 3. Determinar tipo de mensaje y extraer texto
  let inputText: string;
  switch (payload.messageBody.type) {
    case "text":
      inputText = payload.messageBody.text!.body;
      break;
    case "audio":
      inputText = await transcribeAudio(payload.messageBody.audio!);
      if (!inputText) {
        await sendErrorReply(phoneNumber, ERROR_MESSAGES.transcriptionFailed);
        return;
      }
      break;
    case "interactive":
      inputText = handleInteractiveReply(payload.messageBody.interactive!);
      break;
    default:
      await sendErrorReply(phoneNumber, ERROR_MESSAGES.unsupportedFormat);
      return;
  }

  // 4. Verificar Auth_Session para acciones bancarias
  const authSession = await getAuthSession(phoneNumber);
  if (!authSession || isExpired(authSession)) {
    // Guardar solicitud pendiente y enviar botón de login
    await storePendingRequest(phoneNumber, inputText);
    await sendLoginButton(phoneNumber);
    return;
  }

  // 5. Invocar Bedrock Agent
  const sessionId = deriveSessionId(phoneNumber);
  const response = await invokeBedrockAgent(sessionId, inputText);

  // 6. Verificar si la respuesta incluye un documento PDF (extracto)
  const statementInfo = extractStatementInfo(response);
  if (statementInfo) {
    // Enviar PDF como documento adjunto vía WhatsApp
    await sendWhatsAppDocument(
      phoneNumber,
      statementInfo.s3Bucket,
      statementInfo.s3Key,
      statementInfo.fileName,
      statementInfo.caption
    );
  }

  // 7. Enviar respuesta de texto (split si > 4096 chars)
  const textResponse = removeStatementMetadata(response);
  if (textResponse.trim()) {
    await sendWhatsAppResponse(phoneNumber, textResponse);
  }
}
```

#### Flujo de Consentimiento

```typescript
async function handleConsentFlow(
  payload: EUMSIncomingPayload,
  consent: ConsentRecord | null
): Promise<void> {
  const phoneNumber = payload.originationPhoneNumber;

  // Si es respuesta a botón de T&C
  if (payload.messageBody.type === "interactive" && payload.messageBody.interactive?.button_reply) {
    const buttonId = payload.messageBody.interactive.button_reply.id;
    
    if (buttonId === "accept_tc") {
      await storeConsent(phoneNumber, "accepted");
      await sendWelcomeMessage(phoneNumber);
      return;
    }
    
    if (buttonId === "reject_tc") {
      await storeConsent(phoneNumber, "rejected");
      await sendReply(phoneNumber, ERROR_MESSAGES.consentRequired);
      return;
    }
  }

  // Primer mensaje sin consentimiento — enviar T&C con botones
  await sendTermsAndConditionsMessage(phoneNumber);
}

async function sendTermsAndConditionsMessage(phoneNumber: string): Promise<void> {
  const interactiveMessage = {
    messaging_product: "whatsapp",
    to: phoneNumber,
    type: "interactive",
    interactive: {
      type: "button",
      body: {
        text: "👋 ¡Bienvenido a BTG ConnectAI! Para usar nuestros servicios, necesitas aceptar los Términos y Condiciones. Puedes consultarlos en: https://btgpactual.com.co/terminos\n\n¿Aceptas los Términos y Condiciones?"
      },
      action: {
        buttons: [
          { type: "reply", reply: { id: "accept_tc", title: "✅ Acepto" } },
          { type: "reply", reply: { id: "reject_tc", title: "❌ No acepto" } }
        ]
      }
    }
  };

  await socialMessagingClient.send(new SendWhatsAppMessageCommand({
    originationPhoneNumberId: ORIGINATION_PHONE_ID,
    message: Buffer.from(JSON.stringify(interactiveMessage)),
    metaApiVersion: "v21.0",
  }));
}
```

#### Transcripción de Audio

```typescript
async function transcribeAudio(audio: { id: string; mimeType: string }): Promise<string | null> {
  try {
    // 1. Descargar audio desde WhatsApp Media API
    const audioBuffer = await downloadWhatsAppMedia(audio.id);

    // 2. Subir a S3 temporal para Transcribe
    const s3Key = `audio-temp/${uuidv4()}.ogg`;
    await s3Client.send(new PutObjectCommand({
      Bucket: AUDIO_TEMP_BUCKET,
      Key: s3Key,
      Body: audioBuffer,
      ContentType: "audio/ogg",
    }));

    // 3. Iniciar transcripción
    const jobName = `btg-connectai-${uuidv4()}`;
    await transcribeClient.send(new StartTranscriptionJobCommand({
      TranscriptionJobName: jobName,
      LanguageCode: "es-CO",
      MediaFormat: "ogg",
      Media: { MediaFileUri: `s3://${AUDIO_TEMP_BUCKET}/${s3Key}` },
      OutputBucketName: AUDIO_TEMP_BUCKET,
      OutputKey: `transcriptions/${jobName}.json`,
    }));

    // 4. Polling hasta completar (max 10s)
    const transcript = await waitForTranscription(jobName, 10_000);

    // 5. Limpiar archivos temporales
    await cleanupTempFiles(s3Key, `transcriptions/${jobName}.json`);

    return transcript;
  } catch (error) {
    logger.error("Audio transcription failed", { error });
    return null;
  }
}
```

#### Envío de Mensajes Interactivos (Botón de Login)

```typescript
async function sendLoginButton(phoneNumber: string): Promise<void> {
  const callbackToken = generateCallbackToken(phoneNumber);
  const loginUrl = `${LOGIN_PAGE_URL}?phone=${encodeURIComponent(phoneNumber)}&token=${callbackToken}`;

  const interactiveMessage = {
    messaging_product: "whatsapp",
    to: phoneNumber,
    type: "interactive",
    interactive: {
      type: "button",
      body: {
        text: "🔐 Para ejecutar operaciones bancarias necesitas autenticarte. Haz clic en el botón para iniciar sesión."
      },
      action: {
        buttons: [
          { type: "reply", reply: { id: "login_redirect", title: "🔑 Iniciar sesión" } }
        ]
      }
    }
  };

  await socialMessagingClient.send(new SendWhatsAppMessageCommand({
    originationPhoneNumberId: ORIGINATION_PHONE_ID,
    message: Buffer.from(JSON.stringify(interactiveMessage)),
    metaApiVersion: "v21.0",
  }));
}
```

#### Deduplicación

```typescript
async function checkAndStoreDeduplicate(messageId: string): Promise<boolean> {
  try {
    await dynamoClient.send(new PutItemCommand({
      TableName: DEDUP_TABLE,
      Item: {
        pk: { S: messageId },
        ttl: { N: String(Math.floor(Date.now() / 1000) + 600) }, // 10 min TTL
        createdAt: { S: new Date().toISOString() },
      },
      ConditionExpression: "attribute_not_exists(pk)",
    }));
    return false;
  } catch (error) {
    if (error instanceof ConditionalCheckFailedException) {
      return true;
    }
    throw error;
  }
}
```

#### Invocación del Bedrock Agent

```typescript
async function invokeBedrockAgent(sessionId: string, inputText: string): Promise<string> {
  const command = new InvokeAgentCommand({
    agentId: BEDROCK_AGENT_ID,
    agentAliasId: BEDROCK_AGENT_ALIAS_ID,
    sessionId,
    inputText,
  });

  const response = await bedrockAgentRuntimeClient.send(command);
  
  let fullResponse = "";
  for await (const event of response.completion ?? []) {
    if (event.chunk?.bytes) {
      fullResponse += new TextDecoder().decode(event.chunk.bytes);
    }
  }
  
  return fullResponse;
}
```

#### Envío de Respuesta (con split)

```typescript
const MAX_WHATSAPP_LENGTH = 4096;

async function sendWhatsAppResponse(phoneNumber: string, text: string): Promise<void> {
  const chunks = splitMessage(text, MAX_WHATSAPP_LENGTH);
  
  for (const chunk of chunks) {
    const messagePayload = JSON.stringify({
      messaging_product: "whatsapp",
      to: phoneNumber,
      type: "text",
      text: { body: chunk },
    });

    await socialMessagingClient.send(new SendWhatsAppMessageCommand({
      originationPhoneNumberId: ORIGINATION_PHONE_ID,
      message: Buffer.from(messagePayload),
      metaApiVersion: "v21.0",
    }));
  }
}

function splitMessage(text: string, maxLength: number): string[] {
  if (text.length <= maxLength) return [text];
  
  const chunks: string[] = [];
  let remaining = text;
  
  while (remaining.length > 0) {
    if (remaining.length <= maxLength) {
      chunks.push(remaining);
      break;
    }
    // Buscar último salto de línea o espacio antes del límite
    let splitIndex = remaining.lastIndexOf("\n", maxLength);
    if (splitIndex === -1 || splitIndex < maxLength * 0.5) {
      splitIndex = remaining.lastIndexOf(" ", maxLength);
    }
    if (splitIndex === -1) {
      splitIndex = maxLength;
    }
    chunks.push(remaining.substring(0, splitIndex));
    remaining = remaining.substring(splitIndex).trimStart();
  }
  
  return chunks;
}
```

#### Envío de Documento PDF (Extracto Bancario)

```typescript
async function sendWhatsAppDocument(
  phoneNumber: string,
  s3Bucket: string,
  s3Key: string,
  fileName: string,
  caption?: string
): Promise<void> {
  // 1. Descargar PDF desde S3
  const getObjectResponse = await s3Client.send(new GetObjectCommand({
    Bucket: s3Bucket,
    Key: s3Key,
  }));
  const pdfBuffer = await streamToBuffer(getObjectResponse.Body);

  // 2. Subir media (PDF) a EUMS para obtener media_id
  const mediaResponse = await socialMessagingClient.send(new PostWhatsAppMessageMediaCommand({
    originationPhoneNumberId: ORIGINATION_PHONE_ID,
    mediaContentType: "application/pdf",
    sourceS3File: {
      bucketName: s3Bucket,
      key: s3Key,
    },
  }));
  const mediaId = mediaResponse.mediaId;

  // 3. Enviar mensaje de tipo document con el media_id
  const documentMessage = JSON.stringify({
    messaging_product: "whatsapp",
    to: phoneNumber,
    type: "document",
    document: {
      id: mediaId,
      filename: fileName,
      caption: caption ?? "📄 Aquí tienes tu extracto bancario.",
    },
  });

  await socialMessagingClient.send(new SendWhatsAppMessageCommand({
    originationPhoneNumberId: ORIGINATION_PHONE_ID,
    message: Buffer.from(documentMessage),
    metaApiVersion: "v21.0",
  }));
}
```


### 2. Auth_Service Lambda

**Responsabilidad:** Backend de autenticación mock. Valida credenciales contra usuarios de prueba hardcodeados y crea Auth_Session en DynamoDB. Simula un flujo de autenticación vía enlace web.

**Runtime:** Node.js 20.x (TypeScript)  
**Memory:** 128 MB  
**Timeout:** 10 seconds  
**Trigger:** API Gateway (HTTP API) o Function URL

#### Interface

```typescript
// POST /authenticate
interface AuthenticateRequest {
  username: string;
  password: string;
  phoneNumber: string;    // E.164 — vincula sesión al teléfono
  callbackToken: string;  // Token para validar origen legítimo
}

interface AuthenticateResponse {
  success: boolean;
  message: string;
  sessionId?: string;     // Solo si success=true
  expiresAt?: string;     // ISO 8601 — TTL de la sesión
}
```

#### Usuarios de Prueba Hardcodeados

```typescript
const TEST_USERS: TestUser[] = [
  {
    username: "carlos.rodriguez",
    password: "Btg2024*Test",
    phoneNumber: "+573001234567",
    name: "Carlos Rodríguez",
    documentId: "1234567890",
  },
  {
    username: "maria.lopez",
    password: "Btg2024*Demo",
    phoneNumber: "+573009876543",
    name: "María López",
    documentId: "0987654321",
  },
  {
    username: "juan.garcia",
    password: "Btg2024*Hack",
    phoneNumber: "+573005551234",
    name: "Juan García",
    documentId: "1122334455",
  },
];
```

#### Lógica de Autenticación

```typescript
async function authenticate(request: AuthenticateRequest): Promise<AuthenticateResponse> {
  // 1. Validar callback token
  if (!isValidCallbackToken(request.callbackToken, request.phoneNumber)) {
    return { success: false, message: "Token inválido" };
  }

  // 2. Buscar usuario
  const user = TEST_USERS.find(
    u => u.username === request.username && u.password === request.password
  );

  if (!user) {
    return { success: false, message: "Credenciales incorrectas" };
  }

  // 3. Validar que el teléfono coincide con el usuario
  if (user.phoneNumber !== request.phoneNumber) {
    return { success: false, message: "Credenciales incorrectas" };
  }

  // 4. Crear Auth_Session en DynamoDB
  const sessionId = uuidv4();
  const expiresAt = new Date(Date.now() + 30 * 60 * 1000); // 30 min TTL
  const ttl = Math.floor(expiresAt.getTime() / 1000);

  await dynamoClient.send(new PutItemCommand({
    TableName: AUTH_SESSION_TABLE,
    Item: {
      pk: { S: request.phoneNumber },
      sessionId: { S: sessionId },
      username: { S: user.username },
      name: { S: user.name },
      documentId: { S: user.documentId },
      createdAt: { S: new Date().toISOString() },
      expiresAt: { S: expiresAt.toISOString() },
      ttl: { N: String(ttl) },
    },
  }));

  // 5. Notificar al Gateway (via DynamoDB stream o polling)
  return {
    success: true,
    message: "Autenticación exitosa",
    sessionId,
    expiresAt: expiresAt.toISOString(),
  };
}
```

### 3. Login_Page (S3 Static Site)

**Responsabilidad:** Página web simple con formulario de login. Hosted en S3 como sitio estático con CloudFront (o directamente S3 website hosting para MVP).

**Tecnología:** HTML + CSS + JavaScript vanilla (sin framework)

#### Estructura

```
login-page/
├── index.html      # Formulario de login
├── styles.css      # Estilos BTG Pactual branding
├── app.js          # Lógica de submit + llamada a Auth_Service
└── assets/
    └── logo.png    # Logo BTG Pactual
```

#### Flujo de la Login_Page

```typescript
// app.js (client-side)
async function handleLogin(event: Event): Promise<void> {
  event.preventDefault();
  
  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;
  const params = new URLSearchParams(window.location.search);
  const phoneNumber = params.get("phone");
  const callbackToken = params.get("token");

  const response = await fetch(AUTH_SERVICE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, phoneNumber, callbackToken }),
  });

  const result = await response.json();
  
  if (result.success) {
    showSuccess("✅ Autenticación exitosa. Puedes volver a WhatsApp.");
  } else {
    showError(result.message);
  }
}
```

### 4. Action_Group Lambda: balance-query

**Responsabilidad:** Consultar saldos de Fondos de Inversión y Cuenta Corriente del Mock_Core.

**Runtime:** Node.js 20.x (TypeScript)  
**Memory:** 128 MB  
**Timeout:** 15 seconds  
**Trigger:** Bedrock Agent Action Group invocation

#### Interface de Entrada/Salida (Bedrock Agent)

```typescript
interface BedrockAgentActionGroupEvent {
  messageVersion: "1.0";
  agent: { name: string; id: string; alias: string; version: string };
  inputText: string;
  sessionId: string;
  actionGroup: string;
  apiPath: string;
  httpMethod: string;
  parameters: Array<{ name: string; type: string; value: string }>;
  sessionAttributes: Record<string, string>;
  promptSessionAttributes: Record<string, string>;
}

interface BedrockAgentActionGroupResponse {
  messageVersion: "1.0";
  response: {
    actionGroup: string;
    apiPath: string;
    httpMethod: string;
    httpStatusCode: number;
    responseBody: {
      "application/json": { body: string };
    };
  };
}
```

#### API Paths

| Path | Method | Parámetros | Descripción |
|------|--------|------------|-------------|
| `/balance` | GET | `phoneNumber` (required), `productType` (optional: "fondo_inversion" \| "cuenta_corriente") | Consulta saldos |

### 5. Action_Group Lambda: transfer-breb

**Responsabilidad:** Ejecutar transferencias BRE-B entre cuentas contra el Mock_Core.

**Runtime:** Node.js 20.x (TypeScript)  
**Memory:** 128 MB  
**Timeout:** 15 seconds  
**Trigger:** Bedrock Agent Action Group invocation

#### API Paths

| Path | Method | Parámetros | Descripción |
|------|--------|------------|-------------|
| `/transfer` | POST | `sourceAccount`, `destinationAccount`, `amount`, `concept`, `phoneNumber` | Ejecutar transferencia |
| `/transfer/validate` | POST | `sourceAccount`, `destinationAccount`, `amount`, `phoneNumber` | Validar antes de confirmar |

#### Lógica de Transferencia

```typescript
async function executeTransfer(params: TransferParams): Promise<TransferResult> {
  const { sourceAccount, destinationAccount, amount, concept, phoneNumber } = params;

  // 1. Validar cuenta origen existe y pertenece al cliente
  const sourceAcct = findAccountByNumber(phoneNumber, sourceAccount);
  if (!sourceAcct) {
    return { success: false, error: "ACCOUNT_NOT_FOUND", message: "Cuenta origen no encontrada" };
  }

  // 2. Validar saldo suficiente
  if (sourceAcct.availableBalance < amount) {
    return { success: false, error: "INSUFFICIENT_FUNDS", message: "Fondos insuficientes" };
  }

  // 3. Validar cuenta destino existe
  const destAcct = findAccountByNumber(null, destinationAccount);
  if (!destAcct) {
    return { success: false, error: "DEST_NOT_FOUND", message: "Cuenta destino no encontrada" };
  }

  // 4. Ejecutar transferencia (mock — actualizar saldos en memoria)
  sourceAcct.availableBalance -= amount;
  sourceAcct.totalBalance -= amount;
  destAcct.availableBalance += amount;
  destAcct.totalBalance += amount;

  // 5. Generar comprobante
  const receipt: TransferReceipt = {
    transactionId: `TRX-${Date.now()}-${Math.random().toString(36).substr(2, 6)}`,
    sourceAccount: maskAccountNumber(sourceAccount),
    destinationAccount: maskAccountNumber(destinationAccount),
    amount,
    currency: "COP",
    concept,
    executedAt: new Date().toISOString(),
    status: "COMPLETED",
  };

  return { success: true, receipt };
}
```

### 6. Action_Group Lambda: statement-generator

**Responsabilidad:** Generar extractos bancarios en PDF, almacenarlos en S3 y retornar la referencia (S3 key) para que el WhatsApp_Gateway descargue y envíe el PDF como documento adjunto vía WhatsApp.

**Runtime:** Node.js 20.x (TypeScript)  
**Memory:** 256 MB  
**Timeout:** 30 seconds  
**Trigger:** Bedrock Agent Action Group invocation

#### API Paths

| Path | Method | Parámetros | Descripción |
|------|--------|------------|-------------|
| `/statement` | POST | `phoneNumber`, `accountId`, `cutoffDate` | Generar extracto PDF |

#### Lógica de Generación

```typescript
async function generateStatement(params: StatementParams): Promise<StatementResult> {
  const { phoneNumber, accountId, cutoffDate } = params;

  // 1. Validar fecha de corte (debe ser pasada)
  const cutoff = new Date(cutoffDate);
  if (cutoff >= new Date()) {
    return { success: false, error: "INVALID_DATE", message: "La fecha de corte debe ser una fecha pasada" };
  }

  // 2. Obtener datos del cliente y transacciones
  const client = findClientByPhone(phoneNumber);
  const transactions = getTransactionsUntilDate(accountId, cutoffDate);

  // 3. Generar PDF (usando pdfkit o similar)
  const pdfBuffer = await generatePDF({
    clientName: client.name,
    accountNumber: maskAccountNumber(accountId),
    period: { start: getStartOfMonth(cutoffDate), end: cutoffDate },
    transactions,
    finalBalance: calculateBalance(transactions),
  });

  // 4. Subir a S3
  const s3Key = `statements/${phoneNumber}/${accountId}/${cutoffDate}-${uuidv4()}.pdf`;
  await s3Client.send(new PutObjectCommand({
    Bucket: STATEMENT_BUCKET,
    Key: s3Key,
    Body: pdfBuffer,
    ContentType: "application/pdf",
  }));

  // 5. Retornar referencia S3 para que el Gateway descargue y envíe como documento adjunto
  return {
    success: true,
    s3Bucket: STATEMENT_BUCKET,
    s3Key,
    fileName: `extracto_${accountId}_${cutoffDate}.pdf`,
  };
}
```


### 7. Amazon Bedrock Agent (Conversational_Agent)

**Responsabilidad:** Interpretar intenciones en español (texto o audio transcrito), mantener contexto conversacional, decidir cuándo invocar Action Groups, solicitar confirmación para transferencias, y formular respuestas naturales.

**Foundation Model:** Claude 3.5 Haiku (anthropic.claude-3-5-haiku-20241022-v1:0)  
**Session Timeout:** 30 minutos de inactividad  
**Session ID Strategy:** Derivado del número telefónico del Bank_Client

#### Instrucciones del Agente (System Prompt)

```text
Eres el asistente virtual de BTG Pactual Colombia. Tu nombre es ConnectAI.

SERVICIOS DISPONIBLES:
1. Consulta de saldos (Fondos de Inversión y Cuenta Corriente)
2. Transferencias BRE-B (entre cuentas)
3. Generación de extractos bancarios (PDF)

REGLAS:
1. Responde SIEMPRE en español colombiano natural y amigable.
2. Solo puedes ayudar con los 3 servicios listados arriba e información general de productos BTG Pactual.
3. Si el cliente pregunta algo fuera del dominio bancario, declina amablemente y lista los servicios disponibles.
4. Cuando presentes datos financieros (saldos, montos), SIEMPRE incluye el disclaimer: "📋 Esta información es referencial. Para registros oficiales, consulta los portales del banco."
5. Si no entiendes la solicitud, haz UNA pregunta de aclaración. Si después de 2 intentos no logras entender, ofrece el menú de servicios.
6. Interpreta expresiones coloquiales colombianas: "plata"=dinero, "luca"=mil pesos, "extracto"=estado de cuenta, "pásame plata"=transferencia, "cuánto tengo"=consulta de saldo.
7. Formatea montos en COP con separador de miles (punto) y decimales (coma): $1.234.567,89
8. Para TRANSFERENCIAS: SIEMPRE presenta un resumen con cuenta origen, cuenta destino, monto y concepto, y solicita confirmación explícita ("¿Confirmas esta transferencia?") ANTES de ejecutar.
9. Para EXTRACTOS: Solicita la fecha de corte. Si el cliente da una fecha futura, informa que debe ser una fecha pasada.
10. Cuando presentes transacciones o movimientos, muestra máximo 5 y ofrece ver más si hay adicionales.
11. Si el cliente acaba de autenticarse, salúdalo por su nombre.

FORMATO DE RESPUESTA:
- Usa emojis moderadamente para hacer la conversación amigable
- Usa listas con viñetas para presentar múltiples productos o transacciones
- Mantén las respuestas concisas (máximo 3 párrafos)
```

#### OpenAPI Schema — Action Group: balance-query

```yaml
openapi: "3.0.0"
info:
  title: "BTG ConnectAI Balance Query API"
  version: "1.0.0"
  description: "API para consulta de saldos de Fondos de Inversión y Cuenta Corriente"
paths:
  /balance:
    get:
      summary: "Consultar saldos del cliente"
      description: "Retorna saldos de Fondos de Inversión y/o Cuenta Corriente del cliente"
      operationId: "getBalance"
      parameters:
        - name: phoneNumber
          in: query
          required: true
          schema:
            type: string
          description: "Número de teléfono del cliente en formato E.164"
        - name: productType
          in: query
          required: false
          schema:
            type: string
            enum: ["fondo_inversion", "cuenta_corriente", "all"]
          description: "Tipo de producto. Si no se especifica, retorna todos los productos"
      responses:
        "200":
          description: "Saldos consultados exitosamente"
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/BalanceResponse"
        "404":
          description: "Cliente no encontrado"
components:
  schemas:
    BalanceResponse:
      type: object
      properties:
        products:
          type: array
          items:
            $ref: "#/components/schemas/ProductBalance"
    ProductBalance:
      type: object
      properties:
        productType:
          type: string
          enum: ["fondo_inversion", "cuenta_corriente"]
        productName:
          type: string
          example: "Fondo BTG Pactual Liquidez"
        accountNumber:
          type: string
        currency:
          type: string
          example: "COP"
        availableBalance:
          type: number
        totalBalance:
          type: number
        cutoffDate:
          type: string
          format: date
```

#### OpenAPI Schema — Action Group: transfer-breb

```yaml
openapi: "3.0.0"
info:
  title: "BTG ConnectAI BRE-B Transfer API"
  version: "1.0.0"
  description: "API para transferencias BRE-B entre cuentas"
paths:
  /transfer/validate:
    post:
      summary: "Validar transferencia antes de ejecutar"
      description: "Valida que la transferencia sea posible (saldo, cuentas válidas)"
      operationId: "validateTransfer"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/TransferRequest"
      responses:
        "200":
          description: "Transferencia válida"
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/ValidationResult"
        "400":
          description: "Transferencia inválida"
  /transfer:
    post:
      summary: "Ejecutar transferencia BRE-B"
      description: "Ejecuta la transferencia después de confirmación del cliente"
      operationId: "executeTransfer"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/TransferRequest"
      responses:
        "200":
          description: "Transferencia ejecutada"
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/TransferReceipt"
        "400":
          description: "Error en transferencia"
components:
  schemas:
    TransferRequest:
      type: object
      required: [phoneNumber, sourceAccount, destinationAccount, amount]
      properties:
        phoneNumber:
          type: string
          description: "Teléfono del cliente en E.164"
        sourceAccount:
          type: string
          description: "Número de cuenta origen"
        destinationAccount:
          type: string
          description: "Número de cuenta destino"
        amount:
          type: number
          minimum: 1
          description: "Monto en COP"
        concept:
          type: string
          maxLength: 100
          description: "Concepto de la transferencia"
    ValidationResult:
      type: object
      properties:
        valid:
          type: boolean
        sourceAccountName:
          type: string
        destinationAccountName:
          type: string
        availableBalance:
          type: number
        error:
          type: string
    TransferReceipt:
      type: object
      properties:
        transactionId:
          type: string
        sourceAccount:
          type: string
        destinationAccount:
          type: string
        amount:
          type: number
        currency:
          type: string
        concept:
          type: string
        executedAt:
          type: string
          format: date-time
        status:
          type: string
          enum: ["COMPLETED", "FAILED"]
```

#### OpenAPI Schema — Action Group: statement-generator

```yaml
openapi: "3.0.0"
info:
  title: "BTG ConnectAI Statement Generator API"
  version: "1.0.0"
  description: "API para generación de extractos bancarios en PDF"
paths:
  /statement:
    post:
      summary: "Generar extracto bancario PDF"
      description: "Genera un extracto en PDF, lo almacena en S3 y retorna la referencia para envío como documento adjunto vía WhatsApp"
      operationId: "generateStatement"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/StatementRequest"
      responses:
        "200":
          description: "Extracto generado exitosamente"
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/StatementResult"
        "400":
          description: "Fecha de corte inválida"
        "404":
          description: "Cliente o cuenta no encontrada"
components:
  schemas:
    StatementRequest:
      type: object
      required: [phoneNumber, accountId, cutoffDate]
      properties:
        phoneNumber:
          type: string
          description: "Teléfono del cliente en E.164"
        accountId:
          type: string
          description: "ID de la cuenta"
        cutoffDate:
          type: string
          format: date
          description: "Fecha de corte del extracto (debe ser fecha pasada)"
    StatementResult:
      type: object
      properties:
        success:
          type: boolean
        s3Bucket:
          type: string
          description: "Nombre del bucket S3 donde se almacenó el PDF"
        s3Key:
          type: string
          description: "Key del objeto PDF en S3"
        fileName:
          type: string
          description: "Nombre del archivo PDF para envío como documento adjunto"
        error:
          type: string
```

### 8. Bedrock Guardrails

**Responsabilidad:** Filtrar contenido inapropiado, restringir respuestas al dominio bancario, bloquear asesoría financiera personalizada y prevenir prompt injection.

#### Configuración

```typescript
const guardrailConfig = {
  name: "btg-connectai-guardrail",
  description: "Guardrail para asistente bancario BTG Pactual Colombia",
  
  contentPolicyConfig: {
    filtersConfig: [
      { type: "SEXUAL", inputStrength: "HIGH", outputStrength: "HIGH" },
      { type: "VIOLENCE", inputStrength: "HIGH", outputStrength: "HIGH" },
      { type: "HATE", inputStrength: "HIGH", outputStrength: "HIGH" },
      { type: "INSULTS", inputStrength: "MEDIUM", outputStrength: "HIGH" },
      { type: "MISCONDUCT", inputStrength: "HIGH", outputStrength: "HIGH" },
      { type: "PROMPT_ATTACK", inputStrength: "HIGH", outputStrength: "NONE" },
    ],
  },

  topicPolicyConfig: {
    topicsConfig: [
      {
        name: "investment-advice",
        definition: "Recomendaciones específicas de inversión, compra o venta de activos financieros, sugerencias sobre portafolio",
        examples: [
          "¿Debería invertir en acciones de X?",
          "¿Es buen momento para comprar dólares?",
          "Recomiéndame un CDT",
          "¿Qué fondo me conviene más?",
        ],
        type: "DENY",
      },
      {
        name: "non-banking-topics",
        definition: "Temas no relacionados con servicios bancarios de BTG Pactual como política, deportes, entretenimiento, salud, cocina",
        examples: [
          "¿Quién ganó el partido ayer?",
          "¿Qué opinas del presidente?",
          "Dame una receta de cocina",
          "¿Cómo está el clima?",
        ],
        type: "DENY",
      },
      {
        name: "competitor-info",
        definition: "Información sobre productos o servicios de otros bancos o entidades financieras competidoras",
        examples: [
          "¿Qué tasas ofrece Bancolombia?",
          "Compara BTG con Davivienda",
          "¿Es mejor un CDT en Nequi?",
        ],
        type: "DENY",
      },
    ],
  },

  blockedInputMessaging: "Lo siento, no puedo procesar esa solicitud. Solo puedo ayudarte con: consulta de saldos, transferencias BRE-B y generación de extractos bancarios de BTG Pactual.",
  blockedOutputsMessaging: "Lo siento, no puedo proporcionar esa información. ¿Puedo ayudarte con consulta de saldos, transferencias o extractos bancarios?",
};
```


### 9. Observability Stack

#### CloudWatch Dashboard

```typescript
const dashboardWidgets = [
  // WhatsApp_Gateway
  { title: "Gateway - Invocations", metric: "Invocations", functionName: "WhatsApp_Gateway" },
  { title: "Gateway - Errors", metric: "Errors", functionName: "WhatsApp_Gateway" },
  { title: "Gateway - Duration p50/p90", metric: "Duration", functionName: "WhatsApp_Gateway", stats: ["p50", "p90"] },
  // Auth_Service
  { title: "AuthService - Invocations", metric: "Invocations", functionName: "Auth_Service" },
  { title: "AuthService - Errors", metric: "Errors", functionName: "Auth_Service" },
  // balance-query
  { title: "BalanceQuery - Invocations", metric: "Invocations", functionName: "balance-query" },
  { title: "BalanceQuery - Errors", metric: "Errors", functionName: "balance-query" },
  { title: "BalanceQuery - Duration p50/p90", metric: "Duration", functionName: "balance-query", stats: ["p50", "p90"] },
  // transfer-breb
  { title: "TransferBREB - Invocations", metric: "Invocations", functionName: "transfer-breb" },
  { title: "TransferBREB - Errors", metric: "Errors", functionName: "transfer-breb" },
  // statement-generator
  { title: "StatementGen - Invocations", metric: "Invocations", functionName: "statement-generator" },
  { title: "StatementGen - Errors", metric: "Errors", functionName: "statement-generator" },
  { title: "StatementGen - Duration p50/p90", metric: "Duration", functionName: "statement-generator", stats: ["p50", "p90"] },
];
```

#### CloudWatch Alarms

```typescript
// Error rate alarm per Lambda (>10% en 5 min)
const createErrorRateAlarm = (functionName: string) => ({
  alarmName: `btg-connectai-${functionName}-error-rate`,
  metric: mathExpression("errors / invocations * 100"),
  threshold: 10,
  evaluationPeriods: 1,
  period: 300, // 5 minutes
  comparisonOperator: "GreaterThanThreshold",
  alarmActions: [snsAlarmTopic.topicArn],
});

// Alarmas para cada Lambda
const alarms = [
  createErrorRateAlarm("WhatsApp_Gateway"),
  createErrorRateAlarm("Auth_Service"),
  createErrorRateAlarm("balance-query"),
  createErrorRateAlarm("transfer-breb"),
  createErrorRateAlarm("statement-generator"),
];
```

### 10. Infrastructure as Code (CDK Stack Structure)

```
infra/
├── bin/
│   └── app.ts                        # CDK App entry point
├── lib/
│   ├── stacks/
│   │   └── btg-connectai-stack.ts    # Main stack (all resources)
│   ├── constructs/
│   │   ├── whatsapp-gateway.ts       # Gateway Lambda + SNS subscription
│   │   ├── auth-service.ts           # Auth_Service Lambda + Function URL
│   │   ├── login-page.ts             # S3 static site + deployment
│   │   ├── balance-query.ts          # Action Group Lambda
│   │   ├── transfer-breb.ts          # Action Group Lambda
│   │   ├── statement-generator.ts    # Action Group Lambda + S3 bucket
│   │   ├── bedrock-agent.ts          # Bedrock Agent + Guardrails
│   │   ├── dynamodb-tables.ts        # Dedup + Consent_Store + Auth_Session
│   │   ├── audio-processing.ts       # S3 temp bucket for Transcribe
│   │   ├── observability.ts          # Dashboard + Alarms + SNS
│   │   └── security.ts              # IAM roles + Secrets Manager
│   └── config/
│       └── environment.ts            # Environment-specific config
├── cdk.json
└── tsconfig.json

src/
├── lambdas/
│   ├── whatsapp-gateway/
│   │   ├── index.ts                  # Handler
│   │   ├── consent.ts                # Consent flow logic
│   │   ├── auth.ts                   # Auth session check
│   │   ├── transcription.ts          # Audio transcription
│   │   ├── dedup.ts                  # Deduplication logic
│   │   ├── messaging.ts             # WhatsApp message sending (text + documents)
│   │   └── types.ts                  # TypeScript interfaces
│   ├── auth-service/
│   │   ├── index.ts                  # Handler
│   │   ├── users.ts                  # Hardcoded test users
│   │   └── types.ts
│   ├── balance-query/
│   │   ├── index.ts                  # Handler
│   │   ├── mock-data.ts             # Mock_Core data
│   │   └── types.ts
│   ├── transfer-breb/
│   │   ├── index.ts                  # Handler
│   │   ├── mock-data.ts             # Mock_Core data
│   │   └── types.ts
│   └── statement-generator/
│       ├── index.ts                  # Handler
│       ├── pdf-generator.ts          # PDF creation logic
│       ├── mock-data.ts             # Mock_Core data
│       └── types.ts
├── shared/
│   ├── logger.ts                     # Powertools logger config
│   ├── masking.ts                    # Data masking utilities
│   ├── types.ts                      # Shared types
│   └── constants.ts                  # Shared constants
├── login-page/
│   ├── index.html
│   ├── styles.css
│   └── app.js
└── tests/
    ├── unit/
    │   ├── dedup.test.ts
    │   ├── split-message.test.ts
    │   ├── masking.test.ts
    │   ├── consent.test.ts
    │   ├── auth.test.ts
    │   ├── balance-query.test.ts
    │   ├── transfer-breb.test.ts
    │   └── statement-generator.test.ts
    └── property/
        ├── split-message.property.test.ts
        ├── masking.property.test.ts
        ├── dedup.property.test.ts
        ├── session-id.property.test.ts
        ├── balance-query.property.test.ts
        ├── transfer-breb.property.test.ts
        └── statement-date.property.test.ts
```

#### IAM Roles (Least Privilege)

```typescript
// WhatsApp_Gateway Lambda Role
const gatewayRole = {
  policies: [
    // DynamoDB: dedup, consent, auth_session (read + write)
    { effect: "Allow", actions: ["dynamodb:PutItem", "dynamodb:GetItem"], resources: [dedupTable.tableArn] },
    { effect: "Allow", actions: ["dynamodb:PutItem", "dynamodb:GetItem"], resources: [consentTable.tableArn] },
    { effect: "Allow", actions: ["dynamodb:GetItem"], resources: [authSessionTable.tableArn] },
    // Bedrock Agent: invoke
    { effect: "Allow", actions: ["bedrock:InvokeAgent"], resources: [agentArn] },
    // EUMS: enviar mensajes y subir media
    { effect: "Allow", actions: ["social-messaging:SendWhatsAppMessage", "social-messaging:GetWhatsAppMessageMedia", "social-messaging:PostWhatsAppMessageMedia"], resources: ["*"] },
    // Transcribe: start + get job
    { effect: "Allow", actions: ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"], resources: ["*"] },
    // S3: audio temp bucket (read/write)
    { effect: "Allow", actions: ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"], resources: [`${audioTempBucket.bucketArn}/*`] },
    // S3: statement bucket (read — para descargar PDF y enviar como documento adjunto)
    { effect: "Allow", actions: ["s3:GetObject"], resources: [`${statementBucket.bucketArn}/*`] },
    // Secrets Manager
    { effect: "Allow", actions: ["secretsmanager:GetSecretValue"], resources: [secretArn] },
    // CloudWatch Logs
    { effect: "Allow", actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], resources: ["*"] },
  ],
};

// Auth_Service Lambda Role
const authServiceRole = {
  policies: [
    // DynamoDB: auth_session (write)
    { effect: "Allow", actions: ["dynamodb:PutItem"], resources: [authSessionTable.tableArn] },
    // CloudWatch Logs
    { effect: "Allow", actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], resources: ["*"] },
  ],
};

// balance-query Lambda Role
const balanceQueryRole = {
  policies: [
    // Solo CloudWatch Logs (datos mock inline)
    { effect: "Allow", actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], resources: ["*"] },
  ],
};

// transfer-breb Lambda Role
const transferBrebRole = {
  policies: [
    // Solo CloudWatch Logs (datos mock inline)
    { effect: "Allow", actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], resources: ["*"] },
  ],
};

// statement-generator Lambda Role
const statementGeneratorRole = {
  policies: [
    // S3: statement bucket (write only — Gateway handles download)
    { effect: "Allow", actions: ["s3:PutObject"], resources: [`${statementBucket.bucketArn}/*`] },
    // CloudWatch Logs
    { effect: "Allow", actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], resources: ["*"] },
  ],
};

// Bedrock Agent Role
const bedrockAgentRole = {
  policies: [
    // Invoke foundation model
    { effect: "Allow", actions: ["bedrock:InvokeModel"], resources: [modelArn] },
    // Invoke Action Group Lambdas
    { effect: "Allow", actions: ["lambda:InvokeFunction"], resources: [
      balanceQueryLambda.functionArn,
      transferBrebLambda.functionArn,
      statementGeneratorLambda.functionArn,
    ]},
    // Apply Guardrails
    { effect: "Allow", actions: ["bedrock:ApplyGuardrail"], resources: [guardrailArn] },
  ],
};
```

## Data Models

### DynamoDB Table: Dedup (Deduplication)

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String (Partition Key) | `whatsapp_message_id` del mensaje |
| `ttl` | Number | Unix timestamp de expiración (createdAt + 600s = 10 min) |
| `createdAt` | String | ISO 8601 timestamp de recepción |

**Table Settings:**
- Billing Mode: PAY_PER_REQUEST
- TTL Attribute: `ttl`
- Encryption: AWS managed key (`aws/dynamodb`)
- No GSIs

### DynamoDB Table: Consent_Store

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String (Partition Key) | Número telefónico del Bank_Client (E.164) |
| `status` | String | `"accepted"` \| `"rejected"` |
| `acceptedAt` | String | ISO 8601 timestamp de aceptación |
| `tcVersion` | String | Versión de los T&C aceptados (e.g., "1.0") |
| `updatedAt` | String | ISO 8601 timestamp de última actualización |

**Table Settings:**
- Billing Mode: PAY_PER_REQUEST
- TTL: No (consentimiento no expira)
- Encryption: AWS managed key (`aws/dynamodb`)
- No GSIs

### DynamoDB Table: Auth_Session

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String (Partition Key) | Número telefónico del Bank_Client (E.164) |
| `sessionId` | String | UUID v4 de la sesión |
| `username` | String | Username del usuario autenticado |
| `name` | String | Nombre completo del usuario |
| `documentId` | String | Documento de identidad (para vincular con Mock_Core) |
| `createdAt` | String | ISO 8601 timestamp de creación |
| `expiresAt` | String | ISO 8601 timestamp de expiración |
| `ttl` | Number | Unix timestamp de expiración (createdAt + 1800s = 30 min) |

**Table Settings:**
- Billing Mode: PAY_PER_REQUEST
- TTL Attribute: `ttl`
- Encryption: AWS managed key (`aws/dynamodb`)
- No GSIs

### Mock_Core Data (Inline en Action Group Lambdas)

```typescript
// Shared mock data structure across Action Groups

interface MockClient {
  phoneNumber: string;        // E.164
  name: string;
  documentId: string;
  products: MockProduct[];
  transactions: MockTransaction[];
}

interface MockProduct {
  accountId: string;
  accountNumber: string;      // Número de cuenta visible
  productType: "fondo_inversion" | "cuenta_corriente";
  productName: string;
  currency: "COP";
  availableBalance: number;
  totalBalance: number;
  cutoffDate: string;         // ISO 8601 date
}

interface MockTransaction {
  transactionId: string;
  accountId: string;
  date: string;               // ISO 8601 datetime
  description: string;        // Max 100 chars
  amount: number;
  currency: "COP";
  type: "credit" | "debit";
}

interface TransferReceipt {
  transactionId: string;
  sourceAccount: string;
  destinationAccount: string;
  amount: number;
  currency: "COP";
  concept: string;
  executedAt: string;
  status: "COMPLETED" | "FAILED";
}

// Datos mock
const MOCK_CLIENTS: MockClient[] = [
  {
    phoneNumber: "+573001234567",
    name: "Carlos Rodríguez",
    documentId: "1234567890",
    products: [
      {
        accountId: "ACC-001",
        accountNumber: "2001234567",
        productType: "fondo_inversion",
        productName: "Fondo BTG Pactual Liquidez",
        currency: "COP",
        availableBalance: 12_500_000.00,
        totalBalance: 12_500_000.00,
        cutoffDate: "2024-12-15",
      },
      {
        accountId: "ACC-002",
        accountNumber: "1001234568",
        productType: "cuenta_corriente",
        productName: "Cuenta Corriente BTG",
        currency: "COP",
        availableBalance: 3_750_000.50,
        totalBalance: 4_200_000.50,
        cutoffDate: "2024-12-15",
      },
    ],
    transactions: [
      { transactionId: "TRX-001", accountId: "ACC-002", date: "2024-12-14T10:30:00Z", description: "Nómina Empresa XYZ", amount: 5_000_000, currency: "COP", type: "credit" },
      { transactionId: "TRX-002", accountId: "ACC-002", date: "2024-12-13T15:45:00Z", description: "Pago servicios públicos", amount: -350_000, currency: "COP", type: "debit" },
      { transactionId: "TRX-003", accountId: "ACC-002", date: "2024-12-12T09:00:00Z", description: "Transferencia a Fondo", amount: -2_000_000, currency: "COP", type: "debit" },
      { transactionId: "TRX-004", accountId: "ACC-001", date: "2024-12-12T09:01:00Z", description: "Aporte desde Cuenta Corriente", amount: 2_000_000, currency: "COP", type: "credit" },
      { transactionId: "TRX-005", accountId: "ACC-002", date: "2024-12-10T14:20:00Z", description: "Compra Rappi", amount: -85_000, currency: "COP", type: "debit" },
    ],
  },
  {
    phoneNumber: "+573009876543",
    name: "María López",
    documentId: "0987654321",
    products: [
      {
        accountId: "ACC-003",
        accountNumber: "2009876543",
        productType: "fondo_inversion",
        productName: "Fondo BTG Pactual Renta Fija",
        currency: "COP",
        availableBalance: 25_000_000.00,
        totalBalance: 25_000_000.00,
        cutoffDate: "2024-12-15",
      },
      {
        accountId: "ACC-004",
        accountNumber: "1009876544",
        productType: "cuenta_corriente",
        productName: "Cuenta Corriente BTG",
        currency: "COP",
        availableBalance: 8_750_000.50,
        totalBalance: 8_750_000.50,
        cutoffDate: "2024-12-15",
      },
    ],
    transactions: [
      { transactionId: "TRX-006", accountId: "ACC-004", date: "2024-12-14T08:00:00Z", description: "Transferencia recibida", amount: 3_000_000, currency: "COP", type: "credit" },
      { transactionId: "TRX-007", accountId: "ACC-004", date: "2024-12-11T16:30:00Z", description: "Pago tarjeta de crédito", amount: -1_500_000, currency: "COP", type: "debit" },
    ],
  },
  {
    phoneNumber: "+573005551234",
    name: "Juan García",
    documentId: "1122334455",
    products: [
      {
        accountId: "ACC-005",
        accountNumber: "1005551234",
        productType: "cuenta_corriente",
        productName: "Cuenta Corriente BTG",
        currency: "COP",
        availableBalance: 1_200_000.00,
        totalBalance: 1_200_000.00,
        cutoffDate: "2024-12-15",
      },
    ],
    transactions: [
      { transactionId: "TRX-008", accountId: "ACC-005", date: "2024-12-13T11:00:00Z", description: "Depósito efectivo", amount: 500_000, currency: "COP", type: "credit" },
    ],
  },
];
```

### S3 Buckets

#### Audio_Temp_Bucket

- **Purpose:** Almacenamiento temporal de archivos de audio para Amazon Transcribe
- **Lifecycle:** Objetos eliminados automáticamente después de 1 día
- **Encryption:** AWS managed key (`aws/s3`)
- **Access:** Solo WhatsApp_Gateway Lambda

#### Statement_Bucket

- **Purpose:** Almacenamiento temporal de PDFs de extractos bancarios antes de envío como documento adjunto
- **Lifecycle:** Objetos eliminados automáticamente después de 1 día (PDF se entrega inmediatamente como adjunto)
- **Encryption:** AWS managed key (`aws/s3`)
- **Access:** statement-generator Lambda (write) + WhatsApp_Gateway Lambda (read/download)
- **Block Public Access:** Enabled (all 4 settings)

### Secrets Manager Structure

```json
{
  "secretName": "btg-connectai/mvp-lite/config",
  "secretValue": {
    "whatsappPhoneNumberId": "phone-number-id-xxxxx",
    "bedrockAgentId": "AGENT_ID",
    "bedrockAgentAliasId": "ALIAS_ID",
    "loginPageUrl": "https://d1234567.cloudfront.net",
    "authServiceUrl": "https://xyz123.lambda-url.us-east-1.on.aws"
  }
}
```

### Log Schema (Structured JSON via Powertools)

```typescript
interface StructuredLog {
  level: "INFO" | "WARN" | "ERROR";
  message: string;
  timestamp: string;
  service: string; // "whatsapp-gateway" | "auth-service" | "balance-query" | "transfer-breb" | "statement-generator"
  correlation_id: string;
  request_id: string;
  lambda_function: {
    name: string;
    memory_allocated: number;
    arn: string;
  };
  // Custom fields
  latency_ms?: number;
  status_code?: number;
  phone_number_masked?: string;  // "****4567"
  whatsapp_message_id?: string;
  action?: string;               // "dedup_check" | "consent_check" | "auth_check" | "transcribe" | "invoke_agent" | "send_response"
  message_type?: string;         // "text" | "audio" | "interactive"
  auth_event?: string;           // "login_success" | "login_failed" | "session_expired"
}
```

### Data Masking Rules

| Field | Masking Rule | Example |
|-------|-------------|---------|
| Phone number | Retain last 4 digits | `+57300***4567` |
| Account number | Retain last 4 digits | `******4567` |
| Document ID | Retain last 4 digits | `******7890` |
| Username | First char + mask + last char | `c*****z` |


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Message Splitting Round-Trip

*For any* string of arbitrary length, splitting it into chunks of maximum 4096 characters and then concatenating those chunks SHALL produce the original string (minus leading whitespace on subsequent chunks), and every individual chunk SHALL have length ≤ 4096 characters.

**Validates: Requirements 3.6**

### Property 2: Deduplication Idempotency

*For any* valid WhatsApp message ID string, calling the deduplication check function twice with the same ID SHALL return `false` (not duplicate) on the first call and `true` (duplicate) on the second call, regardless of the message ID format or content.

**Validates: Requirements 3.4**

### Property 3: Session ID Determinism

*For any* valid E.164 phone number, the `deriveSessionId` function SHALL always produce the same session ID for the same phone number (deterministic), and two different phone numbers SHALL produce different session IDs (injective).

**Validates: Requirements 11.1**

### Property 4: Data Masking Correctness

*For any* string representing a sensitive value (phone number, account number, or document ID) with length ≥ 4, the masking function SHALL produce an output where only the last 4 characters of the original are visible, all preceding characters are replaced with a mask character, and the masked output preserves the logical structure (e.g., phone prefix retained).

**Validates: Requirements 14.4**

### Property 5: Consent Gate — Existing Consent Skips T&C

*For any* phone number that has a consent record with status "accepted" in the Consent_Store, the consent check function SHALL return `true` (consent exists), causing the system to skip the T&C flow and proceed to message processing.

**Validates: Requirements 1.4**

### Property 6: Auth Gate — No Session Triggers Login

*For any* phone number that does NOT have an active Auth_Session in DynamoDB (either no record exists or TTL has expired), attempting to execute a banking action SHALL trigger the login flow (return indication that authentication is required).

**Validates: Requirements 5.1, 5.8**

### Property 7: Auth Gate — Active Session Allows Actions

*For any* phone number that has an Auth_Session with a TTL value in the future (not expired), the session validation function SHALL return the session as valid, allowing banking actions to proceed without re-authentication.

**Validates: Requirements 5.6, 6.1, 6.2**

### Property 8: Invalid Credentials Rejection

*For any* username/password combination that does NOT match any entry in the hardcoded test users array, the authentication function SHALL return `success: false` with an error message, and SHALL NOT create an Auth_Session in DynamoDB.

**Validates: Requirements 5.5**

### Property 9: Balance Query Correctness

*For any* phone number that exists in the Mock_Core data, querying the balance SHALL return a response where: (a) all products belonging to that client are included when no filter is specified, (b) each product contains `productType`, `productName`, `currency` (COP), `availableBalance`, `totalBalance`, and `cutoffDate` fields, and (c) all values exactly match the corresponding Mock_Core entries.

**Validates: Requirements 7.1, 7.2, 7.3**

### Property 10: Unknown Client Error

*For any* phone number that does NOT exist in the Mock_Core data, querying the balance or requesting a transfer SHALL return an error response with HTTP status 404 and a descriptive error message indicating no data was found.

**Validates: Requirements 7.4, 8.6**

### Property 11: Transfer Execution Produces Valid Receipt

*For any* valid transfer request (source account exists, belongs to client, has sufficient funds, destination account exists), executing the transfer SHALL produce a receipt containing: `transactionId` (non-empty string), `sourceAccount` (masked), `destinationAccount` (masked), `amount` (matching request), `currency` (COP), `concept`, `executedAt` (valid ISO 8601), and `status` ("COMPLETED").

**Validates: Requirements 8.3**

### Property 12: Insufficient Funds Rejection

*For any* transfer request where the `amount` exceeds the `availableBalance` of the source account in Mock_Core, the transfer function SHALL reject the operation with an error indicating insufficient funds, and the source account balance SHALL remain unchanged.

**Validates: Requirements 8.5**

### Property 13: Future Date Rejection for Statements

*For any* date that is today or in the future (relative to current system time), the statement generation function SHALL reject the request with an error indicating that the cutoff date must be a past date.

**Validates: Requirements 9.2**

### Property 14: Valid Statement Generation with S3 Reference

*For any* valid past cutoff date and existing client/account combination, the statement generation function SHALL produce a result with `success: true`, a non-empty `s3Bucket` string, a non-empty `s3Key` string matching the pattern `statements/{phoneNumber}/{accountId}/{cutoffDate}-{uuid}.pdf`, and a `fileName` string ending in `.pdf`.

**Validates: Requirements 9.3, 9.4**

### Property 15: COP Currency Formatting

*For any* non-negative number, the COP formatting function SHALL produce a string matching the pattern `$X.XXX.XXX,YY` where dots separate thousands and comma separates decimals, with exactly 2 decimal places.

**Validates: Requirements 10.5**

### Property 16: Unsupported Message Format Rejection

*For any* message with type in {"image", "video", "sticker", "document", "location"} (i.e., not "text", "audio", or "interactive"), the message type validation function SHALL classify it as unsupported and return the appropriate error message indicating only text and voice notes are accepted.

**Validates: Requirements 2.5**



## Error Handling

### Error Categories and Responses

| Error Scenario | Component | User-Facing Response | Log Action |
|---------------|-----------|---------------------|------------|
| Non-text/audio message | WhatsApp_Gateway | "👋 Solo acepto mensajes de texto y notas de voz. Escríbeme o envíame un audio con tu consulta." | INFO log with message type |
| Duplicate message | WhatsApp_Gateway | None (silently discarded) | INFO log with message ID |
| Consent_Store unavailable | WhatsApp_Gateway | "⚠️ Nuestro servicio está temporalmente no disponible. Por favor intenta de nuevo en unos minutos." | ERROR log with DDB error |
| T&C rejected | WhatsApp_Gateway | "Para usar nuestros servicios es necesario aceptar los Términos y Condiciones. Cuando estés listo, envíanos un mensaje." | INFO log |
| Audio transcription failed | WhatsApp_Gateway | "No pude procesar tu nota de voz. Por favor intenta enviarla de nuevo o escríbeme tu consulta como texto." | ERROR log with transcription error |
| Auth_Session expired | WhatsApp_Gateway | "🔐 Tu sesión ha expirado. Necesitas autenticarte de nuevo para continuar." + login button | INFO log |
| Auth_Session not found | WhatsApp_Gateway | "🔐 Para ejecutar operaciones bancarias necesitas autenticarte." + login button | INFO log |
| Invalid credentials | Auth_Service | Login_Page shows: "Credenciales incorrectas. Verifica tu usuario y contraseña." | WARN log with masked username |
| Bedrock Agent timeout (>15s) | WhatsApp_Gateway | "⚠️ Nuestro servicio está temporalmente no disponible. Por favor intenta de nuevo en unos minutos." | ERROR log with latency |
| Bedrock Agent error | WhatsApp_Gateway | "Lo siento, ocurrió un error procesando tu solicitud. Por favor intenta de nuevo." | ERROR log with error details |
| Guardrails block (input) | Bedrock Agent | Guardrail's configured blocked input message | WARN log with block reason |
| Guardrails block (output) | Bedrock Agent | Guardrail's configured blocked output message | WARN log with block reason |
| Client not found in Mock_Core | Action Groups | Agent formats: "No encontré información de cuenta asociada a tu número." | INFO log with masked phone |
| Insufficient funds | transfer-breb | Agent formats: "No tienes fondos suficientes en la cuenta origen para esta transferencia." | INFO log |
| Invalid destination account | transfer-breb | Agent formats: "La cuenta destino no fue encontrada. Verifica el número e intenta de nuevo." | INFO log |
| Future cutoff date | statement-generator | Agent formats: "La fecha de corte debe ser una fecha pasada. Por favor indica una fecha anterior a hoy." | INFO log |
| PDF generation failure | statement-generator | Agent formats: "No pude generar el extracto. Por favor intenta de nuevo." | ERROR log |
| DynamoDB write failure (dedup) | WhatsApp_Gateway | Process message anyway (dedup is best-effort) | ERROR log, continue processing |
| DynamoDB read failure (auth) | WhatsApp_Gateway | "⚠️ Servicio temporalmente no disponible." | ERROR log |
| Secrets Manager failure | All Lambdas | "Servicio temporalmente no disponible." | ERROR log with secret name |
| EUMS SendMessage failure | WhatsApp_Gateway | None (cannot reach user) | ERROR log with EUMS error |

### Retry Strategy

| Operation | Retries | Backoff | Notes |
|-----------|---------|---------|-------|
| DynamoDB PutItem (dedup) | 0 | N/A | Best effort — process anyway on failure |
| DynamoDB GetItem (consent/auth) | 1 | 100ms | Critical path — retry once |
| Bedrock InvokeAgent | 0 | N/A | Timeout at 15s, no retry |
| Amazon Transcribe | 0 | N/A | Polling with 10s max wait |
| EUMS SendWhatsAppMessage | 2 | Exponential (100ms, 200ms) | Important for delivery |
| S3 PutObject (PDF) | 1 | 100ms | Retry once for transient errors |
| Secrets Manager GetSecret | 1 | 100ms | Cached after first call |

### Error Response Templates (Spanish)

```typescript
const ERROR_MESSAGES = {
  unsupportedFormat: "👋 Solo acepto mensajes de texto y notas de voz. Escríbeme o envíame un audio con tu consulta.",
  transcriptionFailed: "🎙️ No pude procesar tu nota de voz. Por favor intenta enviarla de nuevo o escríbeme tu consulta como texto.",
  serviceUnavailable: "⚠️ Nuestro servicio está temporalmente no disponible. Por favor intenta de nuevo en unos minutos.",
  genericError: "Lo siento, ocurrió un error procesando tu solicitud. Por favor intenta de nuevo.",
  consentRequired: "Para usar nuestros servicios es necesario aceptar los Términos y Condiciones. Cuando estés listo, envíanos un mensaje.",
  authRequired: "🔐 Para ejecutar operaciones bancarias necesitas autenticarte.",
  authExpired: "🔐 Tu sesión ha expirado. Necesitas autenticarte de nuevo para continuar.",
  authSuccess: "✅ ¡Autenticación exitosa! Procesando tu solicitud...",
  welcomeMessage: "👋 ¡Bienvenido a BTG ConnectAI! Estos son los servicios disponibles:\n\n" +
    "💰 *Consulta de saldos* — Fondos de Inversión y Cuenta Corriente\n" +
    "💸 *Transferencias BRE-B* — Entre cuentas\n" +
    "📄 *Extractos bancarios* — Generación de PDF\n\n" +
    "Puedes solicitarme cualquier servicio en lenguaje natural. ¡Escríbeme o envíame una nota de voz!",
} as const;
```

### Timeout Configuration

| Component | Timeout | Rationale |
|-----------|---------|-----------|
| WhatsApp_Gateway Lambda | 60s | Accommodates transcription (10s) + agent (15s) + EUMS send |
| Auth_Service Lambda | 10s | Simple credential check + DDB write |
| balance-query Lambda | 15s | Mock data instant, buffer for cold start |
| transfer-breb Lambda | 15s | Mock data instant, buffer for cold start |
| statement-generator Lambda | 30s | PDF generation + S3 upload |
| Transcription polling | 10s | Max wait for Amazon Transcribe |
| Bedrock Agent response | 15s | Max wait before timeout error |

## Testing Strategy

### Property-Based Testing (PBT)

**Library:** [fast-check](https://github.com/dubzzz/fast-check) (TypeScript)  
**Minimum iterations:** 100 per property  
**Tag format:** `Feature: btg-connect-ai-mvp-lite, Property {number}: {title}`

Properties to implement as PBT:
1. Message splitting round-trip
2. Deduplication idempotency
3. Session ID determinism
4. Data masking correctness
5. Consent gate logic
6. Auth gate (no session → login)
7. Auth gate (active session → proceed)
8. Invalid credentials rejection
9. Balance query correctness
10. Unknown client error
11. Transfer receipt validity
12. Insufficient funds rejection
13. Future date rejection
14. Statement generation with S3 reference
15. COP currency formatting
16. Unsupported format rejection

### Unit Tests (Example-Based)

| Test | Component | What it verifies |
|------|-----------|-----------------|
| T&C interactive message format | WhatsApp_Gateway | Correct WhatsApp interactive payload structure |
| Welcome message content | WhatsApp_Gateway | All 3 services listed in welcome |
| Login button message format | WhatsApp_Gateway | Correct interactive button payload |
| Auth_Service with each test user | Auth_Service | Each of 3 users can authenticate |
| Transfer cancellation | transfer-breb | No state change on cancel |
| Empty statement generation | statement-generator | PDF generated with "no transactions" note |
| Correlation ID generation | All Lambdas | UUID v4 format, attached to logger |
| Log structure validation | All Lambdas | JSON format with required fields |

### Integration Tests

| Test | What it verifies |
|------|-----------------|
| Full consent flow | First message → T&C → accept → welcome |
| Full auth flow | Request → login button → authenticate → process |
| Audio transcription pipeline | Audio upload → Transcribe → text extraction |
| Balance query end-to-end | Auth'd request → agent → balance-query → formatted response |
| Transfer end-to-end | Auth'd request → agent → confirm → transfer-breb → receipt |
| Statement end-to-end | Auth'd request → agent → statement-generator → PDF document attachment via WhatsApp |
| Guardrails blocking | Out-of-domain request → blocked response |
| Session memory | Multi-turn conversation with context |

### CDK Snapshot Tests

| Test | What it verifies |
|------|-----------------|
| No VpcConfig on any Lambda | All Lambdas serverless without VPC |
| DynamoDB encryption settings | AWS managed keys configured |
| S3 bucket policies | Block public access enabled |
| IAM policy scoping | Least privilege per Lambda |
| CloudWatch alarm configuration | 10% error threshold, 5min window |
| Lambda runtime | All Lambdas use Node.js 20.x |

### Test Execution

```bash
# Unit + Property tests
npx vitest --run

# CDK snapshot tests
cd infra && npx jest --run

# Integration tests (requires deployed stack)
npx vitest --run --config vitest.integration.config.ts
```
