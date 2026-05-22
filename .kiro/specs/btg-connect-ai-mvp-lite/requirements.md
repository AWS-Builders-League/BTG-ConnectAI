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
- Serverless (Lambda, DynamoDB, S3, SNS) — sin VPC
- TypeScript (Node.js 20.x) con AWS CDK para IaC
- Amazon Bedrock Agent con Claude Haiku 3.5
- Bedrock Guardrails para filtrado de contenido
- AWS End User Messaging Social para WhatsApp
- CloudWatch + Lambda Powertools para observabilidad
- Secrets Manager para configuración sensible
- Cifrado con AWS managed keys (sin CMKs custom)
- Optimización de free tier donde sea posible (Bedrock pay-per-use aceptado)
- Mercado colombiano — idioma español

**Enfoque MVP:**
- Datos mock para el demo del hackathon
- Autenticación mock con usuarios de prueba hardcodeados
- Demuestra el flujo completo end-to-end con mínima infraestructura

---

## Glossary

- **Conversational_Agent**: Amazon Bedrock Agent que interpreta mensajes en español (texto o audio transcrito), mantiene memoria de sesión, decide qué acciones invocar y formula respuestas. Núcleo de IA conversacional managed por AWS.
- **WhatsApp_Gateway**: Función Lambda (TypeScript) que recibe mensajes entrantes (texto y audio) vía AWS End User Messaging Social, gestiona el flujo de consentimiento, ejecuta deduplicación e invoca al Conversational_Agent.
- **Action_Group**: Lambdas que el Conversational_Agent puede invocar para ejecutar acciones bancarias. Incluye: `transfer-breb`, `balance-query`, `statement-generator`.
- **Bank_Client**: Cliente de BTG Pactual que interactúa con el sistema vía WhatsApp.
- **Consent_Store**: Tabla DynamoDB que almacena el estado de aceptación de Términos y Condiciones por número telefónico.
- **Auth_Session**: Sesión autenticada almacenada en DynamoDB con TTL de 30 minutos, vinculada al número telefónico del Bank_Client.
- **Auth_Service**: Sistema mock de autenticación (Lambda + DynamoDB) con usuarios de prueba hardcodeados que simula el flujo de login vía enlace web.
- **Login_Page**: Página web simple (S3 estático o Lambda-backed) con formulario de login para autenticación del Bank_Client.
- **Session**: Interacción conversacional gestionada por Bedrock Agents con memoria de sesión nativa, separada de la Auth_Session.
- **Bedrock_Guardrails**: Feature managed de Amazon Bedrock que aplica content filtering y topic restrictions sobre las respuestas del Conversational_Agent.
- **Mock_Core**: Datos sintéticos hardcodeados que simulan respuestas del core bancario para saldos, transferencias y extractos.
- **Transcription_Service**: Servicio de transcripción de audio a texto (Amazon Transcribe o capacidades nativas de Bedrock) utilizado para procesar notas de voz.
- **Statement_Bucket**: Bucket S3 donde se almacenan temporalmente los PDFs de extractos bancarios antes de ser enviados como documento adjunto vía WhatsApp.
- **BRE_B_Transfer**: Transferencia de dinero entre cuentas mediante el sistema BRE-B (mock en MVP).

## Requirements

### Requisito 1: Flujo de Consentimiento (Términos y Condiciones)

**Historia de Usuario:** Como Bank_Client, quiero aceptar los Términos y Condiciones antes de usar el servicio, para que el banco cumpla con los requisitos regulatorios de consentimiento informado.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje por primera vez y no tiene consentimiento registrado en el Consent_Store, THE WhatsApp_Gateway SHALL enviar un mensaje interactivo de WhatsApp con un botón para aceptar o rechazar los Términos y Condiciones antes de procesar cualquier otra solicitud
2. WHEN un Bank_Client presiona el botón de aceptar Términos y Condiciones, THE WhatsApp_Gateway SHALL registrar el consentimiento en el Consent_Store asociado al número telefónico del Bank_Client con un timestamp de aceptación
3. WHEN un Bank_Client presiona el botón de rechazar Términos y Condiciones, THE WhatsApp_Gateway SHALL responder con un mensaje informando que la aceptación es obligatoria para utilizar el servicio y que no se procesarán solicitudes hasta que acepte
4. WHEN un Bank_Client que ya tiene consentimiento registrado en el Consent_Store envía un mensaje, THE WhatsApp_Gateway SHALL omitir el flujo de Términos y Condiciones y procesar el mensaje directamente
5. THE Consent_Store SHALL almacenar para cada registro: número telefónico del Bank_Client (partition key), estado del consentimiento (aceptado/rechazado), timestamp de la decisión y versión de los Términos y Condiciones aceptados
6. IF el Consent_Store no está disponible para verificar el estado de consentimiento, THEN THE WhatsApp_Gateway SHALL responder al Bank_Client con un mensaje de indisponibilidad temporal del servicio

### Requisito 2: Entrada Multimodal (Texto y Audio)

**Historia de Usuario:** Como Bank_Client, quiero enviar mensajes de texto o notas de voz al chatbot, para que pueda interactuar con el banco de la forma que me resulte más cómoda.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje de texto vía WhatsApp, THE WhatsApp_Gateway SHALL procesarlo directamente como entrada para el Conversational_Agent
2. WHEN un Bank_Client envía una nota de voz (mensaje de audio) vía WhatsApp, THE WhatsApp_Gateway SHALL enviar el audio al Transcription_Service para obtener la transcripción en texto y luego procesar el texto resultante como entrada para el Conversational_Agent
3. THE Transcription_Service SHALL transcribir el audio a texto en español con una latencia máxima de 10 segundos para notas de voz de hasta 60 segundos de duración
4. IF la transcripción del audio falla o produce un resultado vacío, THEN THE WhatsApp_Gateway SHALL responder al Bank_Client indicando que no se pudo procesar la nota de voz y solicitando que reenvíe el mensaje como texto o intente de nuevo
5. WHEN un Bank_Client envía un mensaje en formato no soportado (imagen, video, sticker, documento, ubicación), THEN THE WhatsApp_Gateway SHALL responder con un mensaje indicando que solo se aceptan mensajes de texto y notas de voz
6. THE WhatsApp_Gateway SHALL soportar notas de voz en los formatos de audio que WhatsApp envía nativamente (OGG/Opus) sin requerir conversión previa por parte del Bank_Client

### Requisito 3: Integración del Canal WhatsApp

**Historia de Usuario:** Como Bank_Client, quiero interactuar con mi banco a través de WhatsApp, para que pueda acceder a servicios bancarios mediante una plataforma de mensajería familiar.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje vía WhatsApp, THE WhatsApp_Gateway SHALL recibirlo vía AWS End User Messaging Social y SNS, e invocar al Conversational_Agent dentro de 5 segundos (excluyendo tiempo de transcripción para audio)
2. WHEN el Conversational_Agent produce una respuesta, THE WhatsApp_Gateway SHALL entregarla al Bank_Client vía AWS End User Messaging Social dentro de 3 segundos
3. THE WhatsApp_Gateway SHALL soportar mensajes interactivos de WhatsApp (botones) para el flujo de consentimiento y el flujo de autenticación
4. WHEN el WhatsApp_Gateway recibe un mensaje con el mismo identificador de mensaje dentro de una ventana de 5 minutos, THE WhatsApp_Gateway SHALL descartarlo como duplicado usando una escritura condicional en DynamoDB con un TTL de 10 minutos en el registro de deduplicación
5. IF el WhatsApp_Gateway no recibe respuesta del Conversational_Agent dentro de 15 segundos, THEN THE WhatsApp_Gateway SHALL responder al Bank_Client con un mensaje indicando indisponibilidad temporal del servicio
6. IF la respuesta del Conversational_Agent excede 4096 caracteres, THEN THE WhatsApp_Gateway SHALL dividirla en múltiples mensajes secuenciales de máximo 4096 caracteres cada uno y enviarlos en orden

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

1. WHEN un Bank_Client solicita ejecutar una acción bancaria (transferencia, consulta de saldo, generación de extracto) y no tiene una Auth_Session activa, THE WhatsApp_Gateway SHALL enviar un mensaje interactivo de WhatsApp con un botón o enlace para "Iniciar sesión" antes de procesar la solicitud
2. WHEN un Bank_Client accede a la Login_Page mediante el enlace proporcionado, THE Login_Page SHALL presentar un formulario de autenticación que solicite credenciales (usuario y contraseña de los usuarios de prueba hardcodeados)
3. WHEN un Bank_Client envía credenciales válidas en la Login_Page, THE Auth_Service SHALL crear una Auth_Session en DynamoDB asociada al número telefónico del Bank_Client con un TTL de 30 minutos
4. WHEN la Auth_Session se crea exitosamente, THE WhatsApp_Gateway SHALL enviar un mensaje al Bank_Client confirmando que la autenticación fue exitosa y proceder a ejecutar la acción solicitada originalmente
5. IF un Bank_Client envía credenciales inválidas en la Login_Page, THEN THE Auth_Service SHALL rechazar la autenticación y la Login_Page SHALL mostrar un mensaje de error indicando credenciales incorrectas
6. WHEN un Bank_Client tiene una Auth_Session activa (TTL no expirado), THE WhatsApp_Gateway SHALL permitir la ejecución de acciones bancarias sin solicitar re-autenticación
7. THE Auth_Service SHALL mantener al menos 3 usuarios de prueba hardcodeados con credenciales predefinidas para el demo del hackathon
8. IF la Auth_Session ha expirado (TTL superado), THEN THE WhatsApp_Gateway SHALL solicitar re-autenticación al Bank_Client antes de ejecutar cualquier acción bancaria

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
2. WHEN el Conversational_Agent tiene todos los datos de la transferencia, THE Conversational_Agent SHALL presentar un resumen de la operación al Bank_Client y solicitar confirmación explícita antes de ejecutarla
3. WHEN el Bank_Client confirma la transferencia, THE Action_Group `transfer-breb` SHALL ejecutar la transferencia contra el Mock_Core y retornar un comprobante con: número de transacción, cuenta origen, cuenta destino, monto, fecha y hora de ejecución, y estado de la operación
4. IF el Bank_Client cancela o no confirma la transferencia, THEN THE Conversational_Agent SHALL cancelar la operación e informar al Bank_Client que la transferencia no fue ejecutada
5. IF el monto de la transferencia excede el saldo disponible en la cuenta origen del Mock_Core, THEN THE Action_Group `transfer-breb` SHALL rechazar la operación e informar al Bank_Client que no tiene fondos suficientes
6. IF la cuenta destino no es válida en el Mock_Core, THEN THE Action_Group `transfer-breb` SHALL rechazar la operación e informar al Bank_Client que la cuenta destino no fue encontrada

### Requisito 9: Generación de Extractos

**Historia de Usuario:** Como Bank_Client, quiero generar extractos bancarios en PDF por WhatsApp, para que pueda obtener mis estados de cuenta sin ir a una sucursal.

#### Criterios de Aceptación

1. WHEN un Bank_Client autenticado solicita un extracto bancario, THE Conversational_Agent SHALL solicitar la fecha de corte deseada para el extracto
2. IF la fecha de corte solicitada es una fecha futura, THEN THE Conversational_Agent SHALL informar al Bank_Client que la fecha de corte debe ser una fecha pasada y solicitar una nueva fecha
3. WHEN la fecha de corte es válida (fecha pasada), THE Action_Group `statement-generator` SHALL generar el extracto en formato PDF y almacenarlo en el Statement_Bucket (S3)
4. WHEN el extracto está generado y almacenado en el Statement_Bucket, THE WhatsApp_Gateway SHALL descargar el PDF desde S3 y enviarlo al Bank_Client como mensaje de documento adjunto vía WhatsApp (tipo de mensaje document de EUMS)
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
2. WHEN un mensaje entrante es recibido por el WhatsApp_Gateway, THE system SHALL generar un `correlation_id` único en formato UUID v4 que se propague a todos los logs de todas las Lambdas involucradas en esa interacción
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

### Requisito 15: Infraestructura Serverless sin VPC

**Historia de Usuario:** Como platform engineer, quiero que las Lambdas corran sin VPC, para que el sistema sea más simple, más rápido en cold start y no consuma costos en VPC Endpoints.

#### Criterios de Aceptación

1. THE system SHALL ejecutar todas las funciones Lambda sin configuración de VPC (sin VpcConfig, sin Security Groups, sin Subnets asociadas), accediendo a todos los servicios AWS directamente vía endpoints públicos
2. THE system SHALL usar IAM roles y policies como único mecanismo de control de acceso entre las Lambdas y los servicios AWS consumidos (DynamoDB, S3, Secrets Manager, SNS, CloudWatch Logs, Amazon Bedrock, Amazon Transcribe)
3. THE system SHALL mantener cold start de Lambda por debajo de 500ms en el percentil 95 (p95), medido desde la métrica `Init Duration` reportada por CloudWatch Logs
4. WHEN se despliega la infraestructura, THE system SHALL validar que ninguna función Lambda del stack tenga la propiedad VpcConfig definida en la plantilla de despliegue
5. THE system SHALL desplegarse usando AWS CDK (TypeScript) con todas las Lambdas en runtime Node.js 20.x

---

## Extensiones Futuras (Path de Escalamiento)

Esta sección documenta el camino claro desde MVP Lite hacia producción.

### EXT-1: Integración con Core Bancario Real

**Trigger:** Cuando se reemplace el Mock_Core con el API real del core bancario de BTG Pactual.

**Path:** Agregar VPC con subnets privadas, VPC Endpoints para servicios AWS, conectividad privada al core bancario. Reemplazar datos mock con llamadas reales.

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
