# Requirements Document

## Introduction

BTG ConnectAI MVP Lite es un chatbot bancario conversacional por WhatsApp para BTG Pactual Colombia, impulsado por Amazon Bedrock Agent con Claude Haiku 3.5. A diferencia de un chatbot tradicional basado en menús, el sistema utiliza inteligencia artificial conversacional que entiende lenguaje natural (texto y audio) para ejecutar servicios bancarios.

**Características diferenciadoras:**
- **Entrada multimodal**: Soporta mensajes de texto Y notas de voz (audio), transcribiendo automáticamente el audio a texto
- **IA conversacional**: No es un bot de menús — el usuario solicita servicios en lenguaje natural
- **Flujo de consentimiento**: Términos y condiciones obligatorios antes de usar el servicio
- **Autenticación vía enlace web**: Login vía enlace en WhatsApp con sesión temporal
- **Servicios bancarios**: Transferencias BRE-B (mock), consulta de saldos, generación de extractos PDF

**Arquitectura técnica:**

- Serverless (Lambda, DynamoDB, S3) — estrategia de red híbrida: solo las Lambdas del dominio bancario corren en subnets privadas de la VPC IA-Builder (us-east-1) con VPC Endpoints (sin NAT, cero salida a internet); el resto corre fuera de VPC, seguro por IAM
- **Python 3.13 para todas las Lambdas** (negocio y Conversational_Agent con Strands Agent SDK)
- **CloudFormation puro (YAML)** para IaC — templates anidados siguiendo el patrón del repo `infra` (no se usa CDK ni SAM). Empaquetado de Lambdas vía ZIP a S3 + GitHub Actions con OIDC
- Twilio (WhatsApp Sandbox) como canal de mensajería
- Amazon API Gateway (HTTP API) expuesto públicamente para recibir webhooks de Twilio
- **Async Webhook Pattern**: `Webhook_Receiver` Lambda síncrono responde 200 a Twilio en <1s y publica a SQS FIFO (`inbound-messages-queue.fifo`); `Message_Processor` Lambda async procesa los mensajes triggered por SQS. SQS FIFO da dedup automático por `MessageSid` (elimina tabla Dedup custom) y orden por `phoneNumber`
- Strands Agent SDK + Amazon Bedrock Agent Core (Claude Haiku 3.5) para IA conversacional
- Bedrock Guardrails para filtrado de contenido
- **AWS Step Functions** para orquestar transacciones distribuidas (transfer BRE-B con autorización OTP via patrón `waitForTaskToken`)
- **Amazon SQS** para notificaciones asíncronas event-driven (email-notification-queue, sms-notification-queue) — productores fire-and-forget, consumidores procesan en batch con reintentos automáticos y DLQ
- AWS Pinpoint para envío de OTP transaccional vía SMS
- Amazon SES para envío de notificaciones por email
- CloudWatch + Lambda Powertools para observabilidad
- Secrets Manager para configuración sensible (Twilio credentials, API keys)
- Cifrado con AWS managed keys (sin CMKs custom)
- Mercado colombiano — idioma español

**Enfoque MVP:**
- Datos mock para el demo del hackathon
- Autenticación mock con usuarios de prueba hardcodeados
- Demuestra el flujo completo end-to-end con mínima infraestructura

---

## Glossary

- **Conversational_Agent**: Lambda Python 3.13 que implementa el agente conversacional usando Strands Agent SDK sobre Amazon Bedrock Agent Core (Claude Haiku 3.5). Interpreta mensajes en español, mantiene memoria de sesión, decide qué herramientas invocar y formula respuestas.
- **Webhook_Receiver**: Lambda Python 3.13 síncrona detrás de API Gateway. Su única responsabilidad es responder 200 a Twilio en <1s: valida la firma X-Twilio-Signature, parsea el payload, y publica el mensaje a `InboundMessagesQueue` (SQS FIFO). Sin acceso a DynamoDB, Bedrock, Transcribe ni Twilio REST API.
- **Message_Processor**: Lambda Python 3.13 asíncrona, disparada por SQS Event Source Mapping sobre `InboundMessagesQueue`. Hace todo el trabajo pesado del flujo de un mensaje: consent check, transcripción de audio, auth check, OTP callback, invocación al Conversational_Agent, y envío de respuesta vía Twilio REST API.
- **InboundMessagesQueue**: SQS FIFO (`inbound-messages-queue.fifo`) con `MessageGroupId = phoneNumber` (garantiza orden por cliente) y `MessageDeduplicationId = MessageSid` (dedup automática de reintentos de Twilio en ventana de 5 min). DLQ asociado: `inbound-messages-dlq`. Reemplaza la tabla `Dedup` custom del diseño anterior.
- **Twilio_Webhook_API**: Amazon API Gateway (HTTP API) público expuesto a Twilio. Recibe los webhooks POST de mensajes entrantes de WhatsApp y los entrega al Webhook_Receiver.
- **Action_Group**: Lambdas Python 3.13 que el Conversational_Agent puede invocar para ejecutar acciones bancarias. Incluye: `transfer-breb`, `balance-query`, `statement-generator`.
- **OTP_Service**: Lambda Python 3.13 invocada por Step Functions con el patrón `waitForTaskToken`. Genera un código OTP de 6 dígitos, lo persiste en DynamoDB junto con el token de Step Functions, y lo envía via AWS Pinpoint (SMS). La Lambda retorna inmediatamente; el state machine queda pausado esperando que Message_Processor invoque `SendTaskSuccess` con el código validado.
- **Email_Service**: Lambda Python 3.13 **disparada por SQS** (Event Source Mapping con `email-notification-queue`, batch size 10). Consume eventos `transfer_confirmation` y los envía via Amazon SES. Fallos van automáticamente a DLQ después de 3 reintentos.
- **SMS_Service**: Lambda Python 3.13 disparada por SQS (`sms-notification-queue`). Consume eventos de notificación SMS post-operación (no confundir con OTP_Service, que es síncrono dentro del workflow).
- **TransferBrebStateMachine**: AWS Step Functions Standard Workflow que orquesta el ciclo completo de transferencia BRE-B: ValidateTransfer → GenerateOTP (waitForTaskToken) → ValidateOTP → ExecuteTransfer → PublishNotifications (Parallel: SQS email + SQS SMS) → NotifyUserSuccess. Maneja nativamente timeouts, reintentos y compensación.
- **EmailNotificationQueue**: Cola SQS de notificaciones por email. Productor: Step Functions (evento `transfer_confirmation`); Email_Service procesa en batch. DLQ `email-dlq` después de 3 fallos.
- **SmsNotificationQueue**: Cola SQS análoga para SMS de confirmación post-operación.
- **Bank_Client**: Cliente de BTG Pactual que interactúa con el sistema vía WhatsApp.
- **Consent_Store**: Tabla DynamoDB que almacena el estado de aceptación de Términos y Condiciones por número telefónico.
- **Auth_Session**: Sesión autenticada almacenada en DynamoDB con TTL de 30 minutos, vinculada al número telefónico del Bank_Client.
- **Auth_Service**: Sistema mock de autenticación (Lambda + DynamoDB) con usuarios de prueba hardcodeados que simula el flujo de login vía enlace web.
- **Login_Page**: Página web simple (S3 estático) con formulario de login para autenticación del Bank_Client.
- **Session**: Interacción conversacional gestionada por el Conversational_Agent (Strands) con memoria de sesión, separada de la Auth_Session.
- **Bedrock_Guardrails**: Feature managed de Amazon Bedrock que aplica content filtering y topic restrictions sobre las respuestas del Conversational_Agent.
- **Mock_Core**: Datos sintéticos hardcodeados que simulan respuestas del core bancario para saldos, transferencias y extractos.
- **Transcription_Service**: Amazon Transcribe, utilizado para procesar notas de voz OGG/Opus enviadas por el Bank_Client vía WhatsApp.
- **Statement_Bucket**: Bucket S3 donde se almacenan temporalmente los PDFs de extractos bancarios antes de ser enviados como documento adjunto vía Twilio.
- **BRE_B_Transfer**: Transferencia de dinero entre cuentas mediante el sistema BRE-B (mock en MVP).

## Requirements

### Requisito 1: Flujo de Consentimiento (Términos y Condiciones)

**Historia de Usuario:** Como Bank_Client, quiero aceptar los Términos y Condiciones antes de usar el servicio, para que el banco cumpla con los requisitos regulatorios de consentimiento informado.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje por primera vez y no tiene consentimiento registrado en el Consent_Store, THE Message_Processor SHALL enviar un mensaje interactivo de WhatsApp con un botón para aceptar o rechazar los Términos y Condiciones antes de procesar cualquier otra solicitud
2. WHEN un Bank_Client presiona el botón de aceptar Términos y Condiciones, THE Message_Processor SHALL registrar el consentimiento en el Consent_Store asociado al número telefónico del Bank_Client con un timestamp de aceptación
3. WHEN un Bank_Client presiona el botón de rechazar Términos y Condiciones, THE Message_Processor SHALL responder con un mensaje informando que la aceptación es obligatoria para utilizar el servicio y que no se procesarán solicitudes hasta que acepte
4. WHEN un Bank_Client que ya tiene consentimiento registrado en el Consent_Store envía un mensaje, THE Message_Processor SHALL omitir el flujo de Términos y Condiciones y procesar el mensaje directamente
5. THE Consent_Store SHALL almacenar para cada registro: número telefónico del Bank_Client (partition key), estado del consentimiento (aceptado/rechazado), timestamp de la decisión y versión de los Términos y Condiciones aceptados
6. IF el Consent_Store no está disponible para verificar el estado de consentimiento, THEN THE Message_Processor SHALL responder al Bank_Client con un mensaje de indisponibilidad temporal del servicio

### Requisito 2: Entrada Multimodal (Texto y Audio)

**Historia de Usuario:** Como Bank_Client, quiero enviar mensajes de texto o notas de voz al chatbot, para que pueda interactuar con el banco de la forma que me resulte más cómoda.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje de texto vía WhatsApp, THE Message_Processor SHALL procesarlo directamente como entrada para el Conversational_Agent
2. WHEN un Bank_Client envía una nota de voz (mensaje de audio) vía WhatsApp, THE Message_Processor SHALL enviar el audio al Transcription_Service para obtener la transcripción en texto y luego procesar el texto resultante como entrada para el Conversational_Agent
3. THE Transcription_Service SHALL transcribir el audio a texto en español con una latencia máxima de 10 segundos para notas de voz de hasta 60 segundos de duración
4. IF la transcripción del audio falla o produce un resultado vacío, THEN THE Message_Processor SHALL responder al Bank_Client indicando que no se pudo procesar la nota de voz y solicitando que reenvíe el mensaje como texto o intente de nuevo
5. WHEN un Bank_Client envía un mensaje en formato no soportado (imagen, video, sticker, documento, ubicación), THEN THE Message_Processor SHALL responder con un mensaje indicando que solo se aceptan mensajes de texto y notas de voz
6. THE Message_Processor SHALL soportar notas de voz en los formatos de audio que WhatsApp envía nativamente (OGG/Opus) sin requerir conversión previa por parte del Bank_Client

### Requisito 3: Integración del Canal WhatsApp

**Historia de Usuario:** Como Bank_Client, quiero interactuar con mi banco a través de WhatsApp, para que pueda acceder a servicios bancarios mediante una plataforma de mensajería familiar.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje vía WhatsApp, Twilio SHALL enviar un webhook POST a la Twilio_Webhook_API (API Gateway), que disparará al Webhook_Receiver Lambda síncrono
2. THE Webhook_Receiver SHALL validar la cabecera `X-Twilio-Signature` usando el `TWILIO_AUTH_TOKEN` (Secrets Manager). Si la firma es inválida, SHALL responder `403 Forbidden` sin encolar el mensaje
3. THE Webhook_Receiver SHALL publicar el payload a `InboundMessagesQueue` (SQS FIFO) con `MessageGroupId = phoneNumber` y `MessageDeduplicationId = MessageSid`, y responder `200 OK` a Twilio dentro de **1 segundo** desde la recepción del webhook
4. WHEN Twilio reintenta un webhook por timeout o error de red, SQS FIFO SHALL descartar automáticamente el mensaje duplicado por coincidencia de `MessageDeduplicationId` en ventana de 5 minutos. NO se requiere tabla Dedup custom
5. WHEN un mensaje entra a `InboundMessagesQueue`, THE Message_Processor SHALL ser invocado vía SQS Event Source Mapping con `batchSize=1` y `reportBatchItemFailures=true`, y procesar el mensaje completo (consent + transcripción + auth + agent + envío de respuesta) dentro de su timeout de 120 segundos
6. THE Message_Processor SHALL invocar el Conversational_Agent (Strands_Agent) sin presión de tiempo del lado de Twilio, ya que la respuesta a Twilio fue entregada por el Webhook_Receiver previamente
7. THE Message_Processor SHALL entregar la respuesta del Conversational_Agent al Bank_Client mediante llamada a Twilio REST API (`twilio.messages.create`), no como respuesta HTTP al webhook original
8. THE Message_Processor SHALL soportar mensajes interactivos de WhatsApp (botones de respuesta rápida de Twilio) para el flujo de consentimiento y el flujo de autenticación
9. IF el Message_Processor falla al procesar un mensaje, SQS SHALL reintentar hasta `maxReceiveCount=3` con visibility timeout de 130 segundos, y luego mover el mensaje al DLQ `inbound-messages-dlq`. THE system SHALL emitir alarma CloudWatch cuando `ApproximateNumberOfMessagesVisible > 0` en el DLQ
10. IF la respuesta del Conversational_Agent excede 1600 caracteres (límite de mensaje único de Twilio WhatsApp), THEN THE Message_Processor SHALL dividirla en múltiples mensajes secuenciales y enviarlos en orden vía Twilio REST API

### Requisito 4: Mensaje Inicial y Servicios Disponibles

**Historia de Usuario:** Como Bank_Client, quiero recibir un mensaje de bienvenida con los servicios disponibles después de aceptar los Términos y Condiciones, para que sepa qué puedo hacer con el chatbot.

#### Criterios de Aceptación

1. WHEN un Bank_Client acepta los Términos y Condiciones por primera vez, THE Conversational_Agent SHALL enviar un mensaje de bienvenida que liste los servicios disponibles: transferencias BRE-B, consulta de saldos (Fondos de Inversión y Cuenta Corriente), y generación de extractos
2. THE Conversational_Agent SHALL presentar la lista de servicios de forma informativa, indicando al Bank_Client que puede solicitar cualquier servicio en lenguaje natural (texto o audio) sin necesidad de seleccionar opciones de un menú
3. WHEN un Bank_Client solicita un servicio que no está en la lista de servicios disponibles, THE Conversational_Agent SHALL declinar la solicitud indicando que no puede ayudar con ese tema y presentar nuevamente la lista de servicios disponibles
4. WHEN un Bank_Client envía un mensaje ambiguo que no permite determinar una acción ejecutable, THE Conversational_Agent SHALL hacer una pregunta de aclaración con un máximo de 2 intentos consecutivos antes de presentar la lista de servicios disponibles

### Requisito 5: Flujo de Autenticación (Vía Enlace Web)

**Historia de Usuario:** Como Bank_Client, quiero autenticarme de forma segura mediante un enlace en WhatsApp antes de ejecutar operaciones bancarias, para que mis datos financieros estén protegidos.

#### Criterios de Aceptación

1. WHEN un Bank_Client solicita ejecutar una acción bancaria (transferencia, consulta de saldo, generación de extracto) y no tiene una Auth_Session activa, THE Message_Processor SHALL enviar un mensaje interactivo de WhatsApp con un botón o enlace para "Iniciar sesión" antes de procesar la solicitud
2. WHEN un Bank_Client accede a la Login_Page mediante el enlace proporcionado, THE Login_Page SHALL presentar un formulario de autenticación que solicite credenciales (usuario y contraseña de los usuarios de prueba hardcodeados)
3. WHEN un Bank_Client envía credenciales válidas en la Login_Page, THE Auth_Service SHALL crear una Auth_Session en DynamoDB asociada al número telefónico del Bank_Client con un TTL de 30 minutos
4. WHEN la Auth_Session se crea exitosamente, THE Message_Processor SHALL enviar un mensaje al Bank_Client confirmando que la autenticación fue exitosa y proceder a ejecutar la acción solicitada originalmente
5. IF un Bank_Client envía credenciales inválidas en la Login_Page, THEN THE Auth_Service SHALL rechazar la autenticación y la Login_Page SHALL mostrar un mensaje de error indicando credenciales incorrectas
6. WHEN un Bank_Client tiene una Auth_Session activa (TTL no expirado), THE Message_Processor SHALL permitir la ejecución de acciones bancarias sin solicitar re-autenticación
7. THE Auth_Service SHALL mantener al menos 3 usuarios de prueba hardcodeados con credenciales predefinidas para el demo del hackathon
8. IF la Auth_Session ha expirado (TTL superado), THEN THE Message_Processor SHALL solicitar re-autenticación al Bank_Client antes de ejecutar cualquier acción bancaria

### Requisito 6: Gestión de Sesiones

**Historia de Usuario:** Como Bank_Client, quiero que mi sesión autenticada se mantenga activa por un tiempo razonable, para que no tenga que autenticarme en cada interacción.

#### Criterios de Aceptación

1. THE Auth_Session SHALL almacenarse en DynamoDB con un TTL de 30 minutos desde el momento de la autenticación exitosa, vinculada al número telefónico del Bank_Client
2. WHEN un Bank_Client ejecuta una acción bancaria con Auth_Session activa, THE system SHALL verificar que el TTL de la Auth_Session no ha expirado antes de procesar la solicitud
3. THE system SHALL mantener separada la Auth_Session (autenticación con TTL en DynamoDB) del contexto conversacional del Conversational_Agent (memoria de sesión nativa de Bedrock Agents), de modo que la expiración de una no afecte a la otra
4. WHEN la Auth_Session de un Bank_Client expira, THE Conversational_Agent SHALL mantener el contexto conversacional previo pero requerir re-autenticación antes de ejecutar la siguiente acción bancaria
5. IF DynamoDB no está disponible para verificar la Auth_Session, THEN THE system SHALL responder al Bank_Client con un mensaje de indisponibilidad temporal y no ejecutar la acción solicitada

### Requisito 7: Consulta de Saldos

**Historia de Usuario:** Como Bank_Client, quiero consultar mis saldos por WhatsApp, para que pueda verificar mi disponibilidad financiera en Fondos de Inversión y Cuenta Corriente sin abrir la app del banco.

#### Criterios de Aceptación

1. WHEN un Bank_Client autenticado solicita su saldo, THE Action_Group `balance-query` SHALL consultar el Mock_Core y retornar los saldos de los productos del Bank_Client (Fondos de Inversión y Cuenta Corriente) en moneda COP
2. IF el Bank_Client no especifica un producto en su solicitud, THEN THE Action_Group `balance-query` SHALL retornar el resumen de saldos de todos los productos registrados en el Mock_Core para ese Bank_Client
3. THE Action_Group `balance-query` SHALL retornar para cada producto: tipo de producto (Fondo de Inversión o Cuenta Corriente), nombre del producto, moneda (COP), saldo disponible, saldo total y fecha de corte
4. IF el Action_Group `balance-query` no encuentra datos en el Mock_Core para el Bank_Client solicitante, THEN THE Action_Group `balance-query` SHALL retornar un mensaje de error indicando que no se encontró información de productos para el cliente
5. WHEN el Conversational_Agent presenta datos financieros al Bank_Client, THE Conversational_Agent SHALL incluir un disclaimer indicando que la información es referencial y los registros oficiales están en los portales del banco

### Requisito 8: Transacciones BRE-B (Transferencias)

**Historia de Usuario:** Como Bank_Client, quiero transferir dinero entre cuentas por WhatsApp, para que pueda realizar operaciones bancarias de forma conversacional.

#### Criterios de Aceptación

1. WHEN un Bank_Client autenticado solicita una transferencia BRE-B, THE Conversational_Agent SHALL solicitar los datos necesarios: cuenta origen, cuenta destino, monto y concepto de la transferencia
2. WHEN el Conversational_Agent tiene todos los datos de la transferencia, THE Conversational_Agent SHALL presentar un resumen de la operación al Bank_Client y solicitar confirmación explícita antes de iniciar el workflow
3. WHEN el Bank_Client confirma la transferencia, THE Strands_Agent SHALL invocar la tool `initiate-transfer-breb`, la cual ejecutará `StartExecution` sobre el state machine `TransferBrebStateMachine` con los datos validados de la transferencia y el `correlationId` de la sesión
4. THE TransferBrebStateMachine SHALL ejecutar la secuencia de estados: `ValidateTransfer` (verifica fondos y cuenta destino contra Mock_Core) → `GenerateOTP` (genera código y pausa el workflow con `waitForTaskToken`, HeartbeatSeconds=300) → `ValidateOTP` (Choice state) → `ExecuteTransfer` (actualiza Mock_Core) → `PublishNotifications` (Parallel: publica a `EmailNotificationQueue` y `SmsNotificationQueue`) → `NotifyUserSuccess` (envía comprobante via Twilio)
5. WHEN el state machine entra al estado `GenerateOTP`, THE OTP_Service SHALL generar un código de 6 dígitos, persistir `{phoneNumber, code, taskToken, executionArn, attempts: 0, ttl: 5min}` en `OTP_Store`, enviar el SMS via Pinpoint y retornar inmediatamente. El state machine queda suspendido sin consumir compute hours
6. WHEN el Bank_Client responde con el código OTP en WhatsApp, THE Message_Processor SHALL leer `OTP_Store`, validar el código y, si es válido, invocar `SendTaskSuccess(taskToken, {valid: true})` para resumir el workflow al estado `ValidateOTP`
7. IF el Bank_Client ingresa un OTP incorrecto y `attempts < 3`, THEN THE Message_Processor SHALL incrementar el contador en DynamoDB y enviar mensaje de reintento via Twilio sin resumir el workflow
8. IF se alcanzan 3 intentos fallidos, THEN THE Message_Processor SHALL invocar `SendTaskFailure(taskToken, OTPBlockedError)` y el state machine transicionará al estado `NotifyOTPBlocked`
9. IF el OTP expira (HeartbeatSeconds=300 vencido sin callback), THEN THE state machine SHALL transicionar al estado `NotifyOTPExpired` y notificar al cliente via Twilio
10. IF `ValidateTransfer` rechaza la operación (fondos insuficientes o cuenta destino inválida), THEN el state machine SHALL transicionar al estado `NotifyValidationFailed` y notificar al cliente, sin generar OTP ni consumir el workflow restante
11. IF `ExecuteTransfer` falla por error inesperado, THEN el state machine SHALL transicionar al estado `NotifyTransferFailed`. AT this stage NO compensación de saldo es necesaria (el Mock_Core MVP solo se actualiza si la ejecución es exitosa); en producción el estado de compensación deberá revertir la operación
12. THE Action_Group `transfer-breb` SHALL generar un comprobante con: `transactionId`, `sourceAccount` (enmascarado), `destinationAccount` (enmascarado), `amount`, `currency: "COP"`, `concept`, `executedAt`, `status: "COMPLETED"`

### Requisito 9: Generación de Extractos

**Historia de Usuario:** Como Bank_Client, quiero generar extractos bancarios en PDF por WhatsApp, para que pueda obtener mis estados de cuenta sin ir a una sucursal.

#### Criterios de Aceptación

1. WHEN un Bank_Client autenticado solicita un extracto bancario, THE Conversational_Agent SHALL solicitar la fecha de corte deseada para el extracto
2. IF la fecha de corte solicitada es una fecha futura, THEN THE Conversational_Agent SHALL informar al Bank_Client que la fecha de corte debe ser una fecha pasada y solicitar una nueva fecha
3. WHEN la fecha de corte es válida (fecha pasada), THE Action_Group `statement-generator` SHALL generar el extracto en formato PDF y almacenarlo en el Statement_Bucket (S3)
4. WHEN el extracto está generado y almacenado en el Statement_Bucket, THE Message_Processor SHALL descargar el PDF desde S3 y enviarlo al Bank_Client como mensaje de documento adjunto vía WhatsApp (tipo de mensaje document de EUMS)
5. THE Action_Group `statement-generator` SHALL generar el PDF del extracto incluyendo: nombre del Bank_Client, número de cuenta (enmascarado), período del extracto, listado de movimientos con fecha, descripción y monto, y saldo final
6. IF no existen movimientos para la fecha de corte solicitada, THEN THE Action_Group `statement-generator` SHALL generar un extracto vacío indicando que no se encontraron movimientos para el período

### Requisito 10: Comprensión de Lenguaje Natural

**Historia de Usuario:** Como Bank_Client, quiero comunicarme con el asistente usando español natural (texto o voz), para que pueda expresar mis necesidades bancarias de forma conversacional sin navegar menús.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje (texto o audio transcrito) en español, THE Conversational_Agent SHALL interpretar la intención del mensaje clasificándola en una de las acciones soportadas (transferencia BRE-B, consulta de saldo, generación de extracto, saludo, despedida, consulta fuera de dominio) usando el foundation model Claude Haiku 3.5
2. WHEN un Bank_Client usa expresiones coloquiales o terminología bancaria del mercado colombiano (por ejemplo: "plata", "luca", "pagar el extracto", "cuánto tengo en la cuenta", "pásame plata a otra cuenta"), THE Conversational_Agent SHALL interpretarlas correctamente como equivalentes a la terminología financiera estándar
3. WHEN un Bank_Client envía un mensaje fuera del dominio bancario, THE Conversational_Agent SHALL declinar la solicitud indicando que no puede ayudar con ese tema y presentar la lista de servicios disponibles
4. WHEN un Bank_Client envía un mensaje vacío o compuesto únicamente por caracteres no interpretables, THE Conversational_Agent SHALL responder solicitando al Bank_Client que reformule su consulta en español
5. THE Conversational_Agent SHALL responder siempre en español colombiano natural y amigable, formateando montos en COP con separador de miles (punto) y decimales (coma): $1.234.567,89

### Requisito 11: Gestión del Contexto Conversacional

**Historia de Usuario:** Como Bank_Client, quiero que el asistente recuerde lo que conversamos dentro de la sesión, para que no tenga que repetir información en cada mensaje.

#### Criterios de Aceptación

1. THE Conversational_Agent SHALL mantener memoria de sesión nativa de Bedrock Agents, utilizando un session ID derivado del número telefónico del Bank_Client, de modo que cada mensaje subsiguiente dentro de la misma sesión tenga acceso al historial de turnos previos
2. WHEN un Bank_Client referencia un tema mencionado en turnos anteriores dentro de la misma sesión, THE Conversational_Agent SHALL producir una respuesta contextualmente coherente que utilice la información previa sin solicitar al Bank_Client que la repita
3. WHEN una sesión conversacional ha estado inactiva por 30 minutos, THE Conversational_Agent SHALL expirar la sesión y la siguiente interacción del Bank_Client SHALL iniciar una nueva sesión sin contexto previo
4. WHEN un Bank_Client envía un mensaje después de que su sesión conversacional ha expirado, THE Conversational_Agent SHALL iniciar una nueva sesión indicando al Bank_Client que se ha iniciado una nueva conversación

### Requisito 12: Gobierno y Seguridad de la IA

**Historia de Usuario:** Como risk manager, quiero que el sistema opere dentro de guardrails definidos, para que el banco mantenga control sobre las respuestas generadas por la IA.

#### Criterios de Aceptación

1. THE Conversational_Agent SHALL usar Bedrock_Guardrails con políticas que restrinjan respuestas al dominio bancario y financiero de BTG Pactual, incluyendo transferencias, consultas de saldo, extractos e información general de servicios
2. WHEN un Bank_Client pregunta por temas fuera del dominio bancario, THE Conversational_Agent SHALL declinar indicando que solo puede asistir con servicios bancarios de BTG Pactual y listar los servicios disponibles
3. THE Bedrock_Guardrails SHALL aplicar content filtering para bloquear asesoría financiera personalizada, recomendaciones de inversión, y cualquier sugerencia que implique una acción financiera específica sobre el portafolio del Bank_Client
4. THE Conversational_Agent SHALL incluir un disclaimer en toda respuesta que contenga saldos, montos de transacciones, o cualquier dato numérico asociado a las cuentas del Bank_Client, indicando que la información es referencial
5. IF Bedrock_Guardrails no puede evaluar una respuesta por indisponibilidad del servicio, THEN THE Conversational_Agent SHALL bloquear la entrega de la respuesta al Bank_Client y responder con un mensaje de indisponibilidad temporal
6. IF un Bank_Client intenta mediante reformulaciones sucesivas obtener respuestas fuera del dominio permitido, THEN THE Bedrock_Guardrails SHALL mantener el bloqueo independientemente de la formulación del mensaje

### Requisito 13: Observabilidad

**Historia de Usuario:** Como platform engineer, quiero observabilidad del sistema, para que pueda detectar problemas y verificar que funciona correctamente.

#### Criterios de Aceptación

1. THE system SHALL emitir logs estructurados en formato JSON usando AWS Lambda Powertools, incluyendo `correlation_id`, `request_id`, latencia en milisegundos y status code, hacia Amazon CloudWatch Logs con un período de retención de 7 días
2. WHEN un mensaje entrante es recibido por el Webhook_Receiver, THE system SHALL generar un `correlation_id` único en formato UUID v4 y propagarlo en el payload de SQS para que el Message_Processor y todas las Lambdas downstream lo usen en sus logs estructurados
3. THE system SHALL proveer un CloudWatch Dashboard con métricas de invocaciones, errores y latencia p50/p90 para cada Lambda del sistema en períodos de 5 minutos
4. WHEN la proporción de invocaciones con error de cualquier Lambda respecto al total de invocaciones supera el 10% en una ventana de 5 minutos, THE system SHALL disparar una alarma de CloudWatch que publique una notificación a un tópico SNS configurado

### Requisito 14: Seguridad y Protección de Datos

**Historia de Usuario:** Como security architect, quiero que la data esté protegida con controles de acceso adecuados, para que la información del Bank_Client permanezca confidencial.

#### Criterios de Aceptación

1. THE system SHALL cifrar toda la data en reposo usando AWS managed keys (aws/dynamodb para DynamoDB, aws/s3 para S3) en vez de CMKs custom
2. THE system SHALL cifrar toda la data en tránsito usando TLS 1.2 o superior
3. THE system SHALL autenticar todas las llamadas API entre componentes internos usando IAM roles donde cada Lambda role tenga permisos limitados exclusivamente a los recursos y acciones específicos que requiere (principio de mínimo privilegio)
4. THE system SHALL enmascarar en logs toda data clasificada como sensible — incluyendo números de cuenta (retener solo últimos 4 dígitos), números de teléfono del Bank_Client (retener solo últimos 4 dígitos) y números de documento de identidad (retener solo últimos 4 dígitos)
5. THE system SHALL almacenar cualquier secreto de configuración (API keys, tokens) en AWS Secrets Manager
6. IF una llamada entre componentes internos es rechazada por IAM (AccessDeniedException), THEN THE system SHALL registrar el evento en CloudWatch Logs incluyendo el `correlation_id`, el recurso denegado y el timestamp, sin exponer detalles del secreto o la política
7. THE Statement_Bucket SHALL configurar una lifecycle policy de 1 día para eliminar automáticamente los PDFs de extractos después de su entrega, dado que el documento se envía directamente como adjunto al Bank_Client

### Requisito 15: Infraestructura Serverless con Estrategia de Red Híbrida

**Historia de Usuario:** Como solution architect, quiero aplicar VPC solo donde aporta seguridad real (las Lambdas del dominio bancario que mañana se conectan al core privado), y dejar fuera de VPC las Lambdas de canal/orquestación que solo consumen APIs públicas (Twilio, Bedrock, Pinpoint, SES), para no sobre-arquitectar, evitar el NAT Gateway, y mantener una postura de seguridad fuerte sin fricción innecesaria.

**Contexto de red de la cuenta:** La cuenta AWS sandbox tiene desplegado el stack `IA-Builder-sandbox-networking` (región us-east-1, CIDR 10.0.0.0/16) con dos subnets privadas disponibles (10.0.11.0/24 en us-east-1a y 10.0.12.0/24 en us-east-1b). **El NAT Gateway NO se usa en este proyecto** (`EnableNatGateway=false`): las Lambdas del dominio bancario no tienen salida a internet — alcanzan servicios AWS exclusivamente vía VPC Endpoints, lo que garantiza cero exfiltración posible.

**Estrategia de ubicación de Lambdas:**

- **Fuera de VPC** (red managed de Lambda, acceso a internet + APIs AWS públicas, control de acceso por IAM): `Webhook_Receiver`, `Message_Processor`, `message_handler_notify`, `ai_agent`, `auth_service`, `otp_service`, `email_service`, `sms_service`, `transfer_breb_initiator`. Estas necesitan internet (Twilio) o solo APIs AWS públicas.
- **Dentro de VPC** (subnets privadas, sin NAT, sin salida a internet, solo VPC Endpoints): `balance_query`, `transfer_breb_validate`, `transfer_breb_execute`, `statement_generator`. Son el dominio bancario — las que en EXT-1 se conectarán al core real vía PrivateLink.

#### Criterios de Aceptación

1. THE system SHALL configurar con `VpcConfig` (subnets privadas + Security Group dedicado) ÚNICAMENTE las Lambdas del dominio bancario: `balance_query`, `transfer_breb_validate`, `transfer_breb_execute`, `statement_generator`, importando los subnet IDs vía `Fn::ImportValue: IA-Builder-sandbox-networking-PrivateSubnetIds`
2. THE system SHALL desplegar las Lambdas de canal/orquestación/notificaciones SIN `VpcConfig` (red managed de Lambda), accediendo a internet (Twilio) y a APIs AWS públicas con control de acceso por IAM. Esto elimina la necesidad del NAT Gateway
3. THE system SHALL proveer acceso a servicios AWS desde las Lambdas en VPC mediante VPC Endpoints: Gateway Endpoints para S3 y DynamoDB (sin costo). El `statement_generator` usa el Gateway Endpoint S3 para `PutObject` del PDF; el extracto se entrega al Bank_Client por WhatsApp (no por email). Las subnets privadas NO SHALL tener ruta `0.0.0.0/0` (cero salida a internet)
4. THE system SHALL crear un Security Group dedicado para las Lambdas en VPC con: cero reglas de ingress de red y egress TCP 443 (HTTPS) hacia los VPC Endpoints. CloudWatch Logs NO requiere endpoint (Lambda envía logs por la plataforma, no por la ENI)
5. THE system SHALL usar IAM roles y policies como mecanismo principal de control de acceso entre las Lambdas y los servicios AWS consumidos
6. THE system SHALL desplegarse usando templates de CloudFormation (YAML) con todas las Lambdas en runtime Python 3.13, en la región us-east-1, siguiendo el patrón de nested stacks del repo `infra` (templates en `cloudformation/templates/`, stack raíz en `cloudformation/stacks/sandbox/`, deploy via GitHub Actions con OIDC y `aws cloudformation deploy`)
7. THE system SHALL exponer un Amazon API Gateway (HTTP API) público con una ruta POST `/webhook/twilio` que reciba los webhooks de Twilio y active el Webhook_Receiver. La URL del endpoint SHALL configurarse como webhook en la cuenta Twilio Sandbox
8. THE system SHALL almacenar las credenciales de Twilio (Account SID, Auth Token, número de origen) en AWS Secrets Manager y no hardcodearlas en el código
9. WHEN se crean los VPC Endpoints y el Security Group, THE system SHALL importar el `VpcId` y el `PrivateRouteTableId` del stack de red vía `Fn::ImportValue` (`IA-Builder-sandbox-networking-VpcId`, `IA-Builder-sandbox-networking-PrivateRouteTableId`)
10. THE system SHALL empaquetar el código de cada Lambda Python (con sus dependencias pip) como ZIP, subirlo a S3, y referenciarlo en el template vía `Code: { S3Bucket, S3Key }`. Las dependencias compartidas (boto3 viene en runtime; aws-lambda-powertools, twilio, strands-agents) SHALL empaquetarse vía Lambda Layers donde sea conveniente
11. THE system SHALL crear una cola SQS FIFO `inbound-messages-queue.fifo` con `ContentBasedDeduplication=false` (la dedup se hace explícitamente por `MessageDeduplicationId`), `VisibilityTimeout=130s`, `MessageRetentionPeriod=1d`, encryption SSE-SQS, DLQ `inbound-messages-dlq.fifo` y `maxReceiveCount=3`
12. THE Webhook_Receiver Lambda SHALL configurarse con timeout máximo de 10 segundos (en la práctica resuelve en <1s) y memory 256MB, SIN VpcConfig. Solo requiere SQS SendMessage y Secrets Manager GetSecretValue
13. THE Message_Processor Lambda SHALL configurarse SIN VpcConfig (necesita llamar a Twilio), con SQS Event Source Mapping sobre `inbound-messages-queue.fifo` con `batchSize=1`, `reportBatchItemFailures=true`, timeout 120s y memory 512MB

### Requisito 16: Autorización OTP con Patrón Task Token

**Historia de Usuario:** Como Bank_Client, quiero confirmar operaciones de alto riesgo (transferencias) con un código de un solo uso enviado a mi celular, para que nadie más pueda ejecutar transacciones en mi nombre aunque acceda a mi sesión de WhatsApp. Como platform engineer, quiero que esta autorización no bloquee Lambdas mientras espera al usuario.

#### Criterios de Aceptación

1. WHEN el state machine `TransferBrebStateMachine` entra al estado `GenerateOTP`, THE Step Functions SHALL invocar el OTP_Service con `arn:aws:states:::lambda:invoke.waitForTaskToken` pasando `$$.Task.Token` en el payload
2. THE OTP_Service SHALL generar un código numérico de 6 dígitos y persistir en `OTP_Store` el registro `{pk: phoneNumber, code, taskToken, executionArn, attempts: 0, transferContext, ttl: now+300s}`
3. THE OTP_Service SHALL enviar el código al número telefónico registrado via AWS Pinpoint (canal SMS) con un mensaje que identifique la operación (monto y cuenta destino enmascarada) y retornará inmediatamente; el state machine queda PAUSADO esperando el callback
4. WHEN el Bank_Client responde con el código OTP en WhatsApp, THE Message_Processor SHALL leer `OTP_Store` por `phoneNumber`, comparar el código, verificar TTL y `attempts < 3`
5. IF el código es válido, THEN THE Message_Processor SHALL invocar `sfnClient.sendTaskSuccess({taskToken, output: {valid: true}})` y eliminar el registro de `OTP_Store`
6. IF el código es incorrecto y `attempts < 3`, THEN THE Message_Processor SHALL incrementar `attempts` en DynamoDB, enviar mensaje de reintento al cliente via Twilio, y NO invocar `sendTaskSuccess`/`sendTaskFailure` (el workflow sigue esperando)
7. IF `attempts >= 3`, THEN THE Message_Processor SHALL invocar `sfnClient.sendTaskFailure({taskToken, error: "OTPBlockedError"})` para que el state machine transicione al estado `NotifyOTPBlocked`
8. IF el `HeartbeatSeconds` (300s) se cumple sin callback, THEN Step Functions SHALL emitir el error `States.Timeout` automáticamente y transicionar a `NotifyOTPExpired` sin requerir intervención manual
9. THE OTP_Service SHALL registrar en CloudWatch Logs cada evento (generación, intento exitoso, intento fallido, expiración) incluyendo `correlationId` y `executionArn` del state machine

### Requisito 17: Notificaciones Asíncronas Event-Driven

**Historia de Usuario:** Como Bank_Client, quiero recibir confirmaciones de operaciones por correo electrónico y SMS post-transferencia, para tener un registro formal fuera del chat de WhatsApp. Como platform engineer, quiero que estos envíos NUNCA bloqueen el flujo transaccional principal.

#### Criterios de Aceptación

1. THE system SHALL exponer dos colas SQS dedicadas: `email-notification-queue` y `sms-notification-queue`, cada una con DLQ asociado (`email-dlq`, `sms-dlq`), `maxReceiveCount: 3`, `visibilityTimeout: 60s`, `messageRetentionPeriod: 4 días`, encryption SSE-SQS
2. WHEN el state machine `TransferBrebStateMachine` ejecuta el estado `PublishNotifications`, THE Step Functions SHALL publicar en paralelo a `email-notification-queue` (evento `transfer_confirmation` con receipt completo) y a `sms-notification-queue` (evento con monto y destino enmascarado) usando el integration directo `arn:aws:states:::sqs:sendMessage`
3. THE Email_Service Lambda SHALL configurarse con SQS Event Source Mapping sobre `email-notification-queue` con `batchSize: 10`, `maxBatchingWindow: 5s`, `reportBatchItemFailures: true`, de modo que mensajes que fallen sean reintentados individualmente sin bloquear el batch completo
4. THE Email_Service SHALL usar Amazon SES como servicio de envío, con dominio remitente verificado y template HTML con identidad BTG Pactual
5. IF un mensaje de SQS falla en `maxReceiveCount` intentos, THEN SQS SHALL moverlo automáticamente al DLQ correspondiente; THE system SHALL emitir alarma CloudWatch cuando `ApproximateNumberOfMessagesVisible` en cualquier DLQ supere 0
6. THE Email_Service SHALL enmascarar datos sensibles en el cuerpo del email (números de cuenta: solo últimos 4 dígitos) siguiendo la misma política de data masking que aplica en los logs
7. IF el envío de email o SMS falla, THEN bajo NINGUNA circunstancia el fallo SHALL propagarse al flujo principal de WhatsApp; el Bank_Client ya recibió la confirmación por el chat
8. THE extracto bancario en PDF NO SHALL enviarse por email; el único canal de entrega del extracto es WhatsApp como documento adjunto vía Twilio Media (ver Requisito 9)

### Requisito 18: Orquestación de Transacciones Distribuidas con Step Functions

**Historia de Usuario:** Como solution architect, quiero que las transacciones distribuidas con callbacks asíncronos (transferencia BRE-B con OTP) se modelen como state machines explícitas, para que el flujo sea auditable, observable, y maneje nativamente errores y timeouts sin código custom.

#### Criterios de Aceptación

1. THE system SHALL implementar `TransferBrebStateMachine` como AWS Step Functions Standard Workflow (no Express) — Standard soporta `waitForTaskToken` con timeouts largos y retiene historial de ejecución 90 días
2. THE state machine SHALL definirse declarativamente en Amazon States Language (ASL) versionado en el repositorio (no construido programáticamente en runtime)
3. THE state machine SHALL tener Retry policies configuradas en cada estado Task con `BackoffRate: 2.0`, `IntervalSeconds: 2`, `MaxAttempts: 2` para errores transitorios (`Lambda.ServiceException`, `Lambda.AWSLambdaException`, `Lambda.SdkClientException`)
4. THE state machine SHALL tener Catch handlers para errores de dominio (`InsufficientFundsError`, `InvalidDestinationError`, `OTPBlockedError`, `States.Timeout`) que transicionan a estados de notificación específicos
5. WHEN una ejecución del state machine completa exitosamente, THE system SHALL retener el historial de ejecución en CloudWatch Logs por 90 días con todos los estados visitados, inputs y outputs (con campos sensibles enmascarados)
6. THE system SHALL emitir alarma CloudWatch cuando la métrica `ExecutionsFailed` del state machine supere 5 en una ventana de 5 minutos
7. THE Strands_Agent SHALL invocar el state machine via `StartExecution` con un `name` único por ejecución (usando `correlationId`) para garantizar idempotencia
8. THE state machine SHALL recibir `executionArn` propagado en logs estructurados para trazabilidad cross-component

---

## Extensiones Futuras (Path de Escalamiento)

Esta sección documenta el camino claro desde MVP Lite hacia producción.

### EXT-1: Integración con Core Bancario Real

**Trigger:** Cuando se reemplace el Mock_Core con el API real del core bancario de BTG Pactual.

**Path:** Las Lambdas del dominio bancario (`balance_query`, `transfer_breb_validate`, `transfer_breb_execute`, `statement_generator`) ya corren en las subnets privadas de la VPC IA-Builder (10.0.11.0/24, 10.0.12.0/24) sin salida a internet — exactamente donde deben estar para conectarse al core. El path requiere: establecer conectividad privada al core bancario de BTG (PrivateLink o VPN site-to-site), reemplazar el Mock_Core por llamadas reales al API del core, y agregar el VPC Endpoint correspondiente si el core se expone como servicio AWS. No se requiere NAT Gateway — el tráfico al core es privado, no por internet.

### EXT-2: Autenticación con Proveedor de Identidad Real

**Trigger:** Cuando se integre con el sistema de identidad corporativo de BTG Pactual (OAuth2/OIDC).

**Path:** Reemplazar Auth_Service mock con integración a Cognito o proveedor de identidad externo. Implementar MFA.

### EXT-3: Servicios Adicionales

**Trigger:** Cuando BTG Pactual apruebe el MVP y se quiera habilitar más operaciones.

**Path:** Agregar Action Groups adicionales (pagos de servicios, apertura de productos, consulta de TRM, etc.).

### EXT-4: Pipeline de Auditoría Completo

**Trigger:** Cuando se requiera retención de 7 años para compliance regulatorio (producción real).

**Path:** Agregar Kinesis Data Firehose → S3 con lifecycle policies. Configurar Glue + Athena para queries ad-hoc.

### EXT-5: KMS CMK Custom con Rotación

**Trigger:** Cuando el equipo de seguridad del banco requiera control total sobre las llaves de cifrado.

**Path:** Crear CMK con rotación anual, migrar tablas DynamoDB y buckets S3 a la nueva key.

### EXT-6: Observabilidad Avanzada (X-Ray, Métricas Custom)

**Trigger:** Cuando el sistema esté en producción con usuarios reales y se necesite troubleshooting avanzado.

**Path:** Habilitar X-Ray tracing, agregar métricas custom con Powertools, crear alarmas granulares.
