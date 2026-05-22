# Documento de Requisitos: BTG ConnectAI (MVP)

## Introducción

BTG ConnectAI es una solución de banca conversacional impulsada por IA (Agentic AI) para BTG Pactual. El sistema integra WhatsApp con Amazon Bedrock Agents para entender solicitudes en lenguaje natural en español, mantener contexto conversacional y ejecutar flujos operativos (consultas financieras y generación de solicitudes transaccionales). Reduce fricción para ~9,176 clientes activos y ~35 colaboradores comerciales al automatizar interacciones repetitivas que hoy atienden manualmente los Relationship Managers (RMs) y Daily Bankers (DBs).

Este documento define los requisitos del **MVP** — la implementación mínima viable para salir a producción rápido sin cerrar puertas a escala futura. Los requisitos avanzados (Fast Path, Fallback con templates, deduplicación SQS FIFO, etc.) quedan documentados al final como **Fase 2 — Diferidos** con sus triggers explícitos de activación.

**Principios del MVP:**
- AWS-native y serverless-first
- Apoyarse en servicios managed (Amazon Bedrock Agents) en vez de orquestación custom
- Diferir complejidad hasta tener señal real de necesidad (no premature optimization)
- Cumplir AWS Well-Architected Framework en seguridad, confiabilidad y compliance desde el día 1
- Mantener extensibilidad: las decisiones MVP no deben requerir refactor mayor para Fase 2

---

## Alcance del Hackathon (AWS) — pitch a BTG Pactual

Este documento describe la visión **MVP completa**. Para la entrega del hackathon recortamos el scope a **arquitectura profesional + 1 flujo crítico funcional end-to-end**, suficiente para pitchear la idea a BTG Pactual sin pelear con free tier ni con integraciones bancarias reales.

### En scope del hackathon (deploy real)

| Componente | Detalle |
|------------|---------|
| **Flujo crítico** | Bank_Client envía "¿cuál es mi saldo?" por WhatsApp → respuesta con saldo + disclaimer |
| **Canal** | WhatsApp Business API real vía AWS End User Messaging Social (número de prueba aprobado por Meta) |
| **AI Core** | Amazon Bedrock Agent (Claude Haiku 3.5) + Guardrails básicos (banca-only) |
| **Action Group** | `financial-query` Lambda — solo operación `getBalance` |
| **Mock Bank Core** | Lambda `mock-bank-core` dentro de la VPC que retorna saldos sintéticos. Reemplaza al core real para el demo |
| **Red** | Reutiliza VPC ya desplegada **`myproject-sandbox`** (10.0.0.0/16) del repo `C:\WorkSpace\AWS\infra`. Subnets privadas 10.0.11.0/24 y 10.0.12.0/24 |
| **VPC Endpoints** | DDB GW, S3 GW, Secrets Mgr IF, KMS IF, CW Logs IF (a configurar) |
| **Datastore** | DynamoDB `Conversations` (solo dedup + sesión hot) |
| **Observabilidad** | CloudWatch Logs estructurados + 1 dashboard básico |
| **Seguridad** | KMS, Secrets Manager, IAM least-privilege, SG `sg-compute` para Lambdas en VPC |

### Diferido a F2 / producción target (diagramado pero NO desplegado en hackathon)

- Requisitos 5 (análisis de gasto), 6 (estado de productos completo), 7 (operaciones/transferencias), 8 (Secure Links HMAC) — quedan en specs como vision producción
- Action Groups `submit-operation` y `generate-secure-link`
- DynamoDB `SecureLinks`
- Pipeline de auditoría completo (Firehose → S3 → Glue → Athena, retención 7 años)
- Direct Connect / VPN al Bank Core real (el mock-bank-core Lambda cubre el demo)
- Property-based tests con Hypothesis (10 properties)
- Provisioned concurrency, multi-region, DR
- F2.1–F2.7 ya documentados al final del archivo

### Qué demuestra el demo

1. **Conversación natural** en español por WhatsApp real → no es un mockup, llega de Meta a AWS
2. **Bedrock Agents orquesta** sin código custom — el ReAct loop nativo invoca la Lambda
3. **Lambda en VPC con PrivateLink** — pitch de seguridad bancaria (sin egress público)
4. **Guardrails activos** — preguntar algo off-topic ("recomiéndame Bitcoin") muestra redirección
5. **Audit hot en CW Logs** — toda la conversación trazable con `correlation_id`
6. **Extensibilidad** — agregar `submit-operation` o `generate-secure-link` no requiere refactor

> El resto de este documento describe el **production target** (MVP completo). Cada requisito aplicable al hackathon viene marcado con **[HACK]** en el título. Los no marcados son F2.

---

## Glosario

- **Conversational_Agent**: Amazon Bedrock Agent que interpreta mensajes, mantiene memoria de sesión, decide qué tools invocar y formula respuestas. Es el núcleo agentic, managed por AWS.
- **WhatsApp_Gateway**: Función Lambda que recibe mensajes entrantes vía AWS End User Messaging Social, los normaliza, ejecuta deduplicación inline e invoca al Conversational_Agent.
- **Action_Group**: Conjunto de tools (Lambdas) que el Conversational_Agent puede invocar — `financial-query`, `submit-operation`, `generate-secure-link`.
- **Bank_Client**: Cliente activo de BTG Pactual que interactúa con el sistema vía WhatsApp.
- **Session**: Interacción conversacional acotada gestionada por Bedrock Agents con expiración por inactividad de 30 minutos.
- **Operational_Request**: Solicitud formal (transferencia, pago, orden) generada por el sistema, con una `idempotency_key` inline que garantiza procesamiento exactly-once.
- **Secure_Link**: URL firmada con HMAC-SHA256 que redirige al Bank_Client al portal oficial del banco para autenticación o validación crítica.
- **Audit_Pipeline**: Pipeline de auditoría compuesto por logs estructurados → CloudWatch Logs → Kinesis Data Firehose → S3, con retención de 7 años para cumplimiento regulatorio.
- **Bedrock_Guardrails**: Feature managed de Amazon Bedrock que aplica content filtering, topic restrictions y PII redaction sobre las respuestas del Conversational_Agent.
- **Bank_Core_API**: APIs externas del core bancario (consultas y sistema transaccional).

## Requisitos

### Requisito 1: Integración del Canal WhatsApp

**Historia de Usuario:** Como Bank_Client, quiero interactuar con mi banco a través de WhatsApp, para que pueda acceder a servicios bancarios mediante una plataforma de mensajería familiar.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje vía WhatsApp, THE WhatsApp_Gateway SHALL recibirlo vía AWS End User Messaging Social e invocar al Conversational_Agent dentro de 3 segundos
2. WHEN el Conversational_Agent produce una respuesta, THE WhatsApp_Gateway SHALL entregarla al Bank_Client dentro de 2 segundos
3. THE WhatsApp_Gateway SHALL soportar mensajes de texto, botones interactivos y listas interactivas como formatos de respuesta
4. WHEN el WhatsApp_Gateway recibe un mensaje de un número telefónico no registrado, THE WhatsApp_Gateway SHALL responder con un mensaje de bienvenida y un prompt de verificación de identidad
5. WHEN el WhatsApp_Gateway recibe un mensaje con el mismo `whatsapp_message_id` dentro de una ventana de 5 minutos, THE WhatsApp_Gateway SHALL descartarlo como duplicado usando una escritura condicional en DynamoDB (sin servicio de deduplicación dedicado)

### Requisito 2: Natural Language Understanding

**Historia de Usuario:** Como Bank_Client, quiero comunicarme con el asistente usando español natural, para que pueda expresar mis necesidades bancarias de forma conversacional sin aprender comandos.

#### Criterios de Aceptación

1. WHEN un Bank_Client envía un mensaje en español, THE Conversational_Agent (Bedrock Agent) SHALL interpretar la intención y extraer entidades financieras (números de cuenta, montos, fechas, monedas, nombres de productos) usando un foundation model Claude
2. WHEN el Conversational_Agent no logra determinar una acción ejecutable con confianza suficiente, THE Conversational_Agent SHALL hacerle al Bank_Client una pregunta de aclaración antes de proceder
3. THE Conversational_Agent SHALL manejar expresiones coloquiales, abreviaturas y terminología bancaria común del mercado colombiano
4. WHEN un Bank_Client envía un mensaje con múltiples intenciones, THE Conversational_Agent SHALL atenderlas secuencialmente dentro de la misma sesión

### Requisito 3: Gestión del Contexto Conversacional

**Historia de Usuario:** Como Bank_Client, quiero que el asistente recuerde lo que conversamos, para que no tenga que repetir información en cada mensaje.

#### Criterios de Aceptación

1. THE Conversational_Agent SHALL mantener memoria de sesión nativa de Bedrock Agents durante toda la conversación activa
2. WHEN un Bank_Client referencia un tema anterior dentro de la misma sesión, THE Conversational_Agent SHALL resolverlo desde la memoria de sesión
3. WHEN una sesión ha estado inactiva por 30 minutos, THE Conversational_Agent SHALL expirar la sesión y la siguiente interacción SHALL iniciar una nueva sesión
4. WHEN una nueva sesión inicia para el mismo Bank_Client, THE system SHALL preservar la identidad del cliente pero limpiar el contexto conversacional de la sesión anterior

### Requisito 4: Consultas de Movimientos y Transacciones

**Historia de Usuario:** Como Bank_Client, quiero consultar mis movimientos y transacciones, para que pueda monitorear mi actividad financiera sin llamar a mi banquero.

#### Criterios de Aceptación

1. WHEN un Bank_Client solicita movimientos de cuenta, THE Action_Group `financial-query` SHALL recuperar las transacciones por cuenta y rango de fechas desde la Bank_Core_API
2. WHEN un Bank_Client no especifica un rango de fechas, THE Action_Group `financial-query` SHALL retornar transacciones de los últimos 30 días por defecto
3. THE Action_Group `financial-query` SHALL retornar los datos de cada transacción incluyendo fecha, descripción, monto, moneda y saldo corriente
4. WHEN el resultado tiene más de 10 transacciones, THE Conversational_Agent SHALL presentar un resumen y ofrecer opciones de paginación
5. WHEN un Bank_Client solicita el saldo actual, THE Action_Group `financial-query` SHALL retornar el saldo disponible y el saldo total de la cuenta

### Requisito 5: Análisis de Gasto

**Historia de Usuario:** Como Bank_Client, quiero recibir análisis de mis patrones de gasto, para que pueda entender mi comportamiento financiero.

#### Criterios de Aceptación

1. WHEN un Bank_Client solicita análisis de gasto, THE Action_Group `financial-query` SHALL categorizar las transacciones y retornar la distribución de gasto por categoría
2. WHEN un Bank_Client no especifica periodo, THE Action_Group `financial-query` SHALL analizar el mes calendario actual por defecto
3. THE Action_Group `financial-query` SHALL identificar las top 5 categorías de gasto con sus montos y porcentajes
4. WHEN un Bank_Client pregunta por tendencias, THE Action_Group `financial-query` SHALL comparar contra el periodo equivalente anterior y marcar cambios > 20% como significativos

### Requisito 6: Estado de Productos

**Historia de Usuario:** Como Bank_Client, quiero consultar el estado de mis productos financieros, para que pueda mantenerme informado sin contactar a mi banquero.

#### Criterios de Aceptación

1. WHEN un Bank_Client consulta el estado de un producto, THE Action_Group `financial-query` SHALL recuperarlo de la Bank_Core_API
2. THE Action_Group `financial-query` SHALL soportar consultas sobre cuentas de ahorro, cuentas corrientes, portafolios de inversión, productos de crédito y CDs
3. WHEN un Bank_Client no especifica producto, THE Conversational_Agent SHALL listar los productos activos del cliente y preguntar cuál consultar
4. THE Action_Group `financial-query` SHALL retornar valor actual, estado, fecha de vencimiento (cuando aplique) y tasa de interés

### Requisito 7: Generación de Solicitudes Operativas

**Historia de Usuario:** Como Bank_Client, quiero iniciar solicitudes operativas como transferencias desde la conversación, para que pueda arrancar operaciones sin navegar interfaces complejas.

#### Criterios de Aceptación

1. WHEN un Bank_Client solicita una transferencia u orden operativa, THE Conversational_Agent SHALL recolectar conversacionalmente los parámetros requeridos (cuenta origen, cuenta destino, monto, moneda, descripción)
2. WHEN todos los parámetros están completos, THE Conversational_Agent SHALL presentar un resumen y pedir confirmación explícita al Bank_Client
3. WHEN el Bank_Client confirma, THE Action_Group `submit-operation` SHALL generar una `idempotency_key` única, validar la suficiencia de saldo, y enviar el Operational_Request a la Bank_Core_API
4. WHEN el Action_Group `submit-operation` recibe un envío duplicado con la misma `idempotency_key` dentro de 24 horas, THE Action_Group `submit-operation` SHALL retornar el resultado original sin re-ejecutar la operación
5. WHEN el envío es exitoso, THE Conversational_Agent SHALL entregar al Bank_Client el número de confirmación y el tiempo estimado de procesamiento

### Requisito 8: Generación de Secure Links

**Historia de Usuario:** Como Bank_Client, quiero ser redirigido a portales seguros del banco cuando una operación requiera autenticación formal, para que mis validaciones críticas queden protegidas dentro de canales autorizados.

#### Criterios de Aceptación

1. WHEN una operación requiere autenticación formal, THE Action_Group `generate-secure-link` SHALL crear una URL única firmada con HMAC-SHA256 que apunte al portal oficial del banco
2. THE Action_Group `generate-secure-link` SHALL codificar el contexto de la operación (tipo, parámetros, referencia de sesión) dentro del link para que el portal pre-llene la validación
3. THE Action_Group `generate-secure-link` SHALL configurar 10 minutos de expiración en cada link generado
4. WHEN un Bank_Client clickea el link, THE portal del banco SHALL validar firma y expiración antes de presentar el formulario de autenticación
5. WHEN un link expira sin usarse, THE Conversational_Agent SHALL informar al Bank_Client que se puede generar uno nuevo

### Requisito 9: Orquestación Agentic Nativa

**Historia de Usuario:** Como Bank_Client, quiero que el asistente coordine solicitudes complejas autónomamente, para que pueda completar tareas bancarias en una sola conversación sin manejar cada paso.

#### Criterios de Aceptación

1. WHEN un Bank_Client hace una solicitud que requiere múltiples pasos, THE Conversational_Agent SHALL descomponerla en una secuencia de invocaciones de Action_Groups, usando el loop ReAct nativo de Bedrock Agents
2. WHEN una invocación de Action_Group falla durante el flujo, THE Conversational_Agent SHALL informar al Bank_Client el punto de falla y ofrecer reintentar o abortar
3. THE Conversational_Agent SHALL informar el progreso al Bank_Client después de cada paso completado en flujos de múltiples pasos
4. WHEN el flujo requiere autenticación, THE Conversational_Agent SHALL pausar la conversación, invocar `generate-secure-link` y retomar tras la validación del Bank_Client

### Requisito 10: Trazabilidad y Auditoría

**Historia de Usuario:** Como compliance officer, quiero que todas las decisiones e interacciones del asistente sean trazables, para que el banco pueda auditar y demostrar cumplimiento regulatorio.

#### Criterios de Aceptación

1. THE Audit_Pipeline SHALL registrar cada interacción Bank_Client ↔ Conversational_Agent (timestamp, contenido del mensaje, intent inferida, decisión)
2. THE Audit_Pipeline SHALL registrar cada invocación de Action_Group (nombre, input, output, duración, estado)
3. THE Audit_Pipeline SHALL registrar cada Operational_Request generado (parámetros, identidad del Bank_Client, estado de aprobación, estado de finalización)
4. THE Audit_Pipeline SHALL retener los registros por un mínimo de 7 años en S3 (Standard 30 días → Glacier IA 1 año → Glacier Deep Archive 7 años) en cumplimiento con la regulación financiera colombiana
5. WHEN un compliance officer necesita consultar registros, THE system SHALL permitir consultas ad-hoc sobre los datos archivados en S3 vía Amazon Athena (sin infraestructura always-on)
6. THE Audit_Pipeline SHALL emitir un `correlation_id` único por sesión que enlace todos los registros relacionados

### Requisito 11: Gobierno y Seguridad de la IA

**Historia de Usuario:** Como risk manager, quiero que el sistema opere dentro de guardrails definidos, para que el banco mantenga control sobre las respuestas generadas y prevenga salidas no conformes.

#### Criterios de Aceptación

1. THE Conversational_Agent SHALL usar Bedrock_Guardrails con políticas que restrinjan respuestas al dominio bancario/financiero de BTG Pactual
2. WHEN un Bank_Client pregunta por temas fuera del dominio bancario, THE Conversational_Agent SHALL declinar cortésmente y redirigir a los servicios disponibles
3. THE Bedrock_Guardrails SHALL aplicar content filtering para bloquear asesoría financiera personalizada, recomendaciones de inversión y statements prospectivos de mercado
4. THE Bedrock_Guardrails SHALL detectar patrones de ingeniería social y fraude en los mensajes entrantes
5. WHEN un patrón de fraude es detectado, THE Conversational_Agent SHALL terminar la interacción y THE Audit_Pipeline SHALL emitir una alerta de seguridad
6. THE Conversational_Agent SHALL incluir un disclaimer en las respuestas con datos financieros, indicando que la información es referencial y los registros oficiales están en los portales del banco

### Requisito 12: Escalabilidad y Disponibilidad (MVP)

**Historia de Usuario:** Como platform engineer, quiero que el sistema escale automáticamente con el tráfico y sea resiliente a fallas zonales, sin tener que sobre-provisionar recursos para el volumen actual.

#### Criterios de Aceptación

1. THE system SHALL apoyarse en el auto-scaling nativo de AWS Lambda y DynamoDB on-demand para manejar el volumen de carga actual (estimado en cientos de mensajes por minuto en pico) sin configuración explícita
2. THE Conversational_Agent SHALL mantener tiempos de respuesta < 5 segundos en el p95 bajo carga normal
3. THE system SHALL mantener disponibilidad del 99.9% medida mensualmente (apoyado en SLAs de Bedrock, Lambda y DynamoDB que ya cumplen ese nivel)
4. THE system SHALL desplegarse en una región AWS con DynamoDB y Lambda multi-AZ por defecto (configuración estándar AWS, sin trabajo adicional)
5. IF un Action_Group queda no disponible, THEN THE Conversational_Agent SHALL informar al Bank_Client la indisponibilidad temporal de esa capacidad sin caerse la conversación completa

### Requisito 13: Observabilidad

**Historia de Usuario:** Como platform engineer, quiero observabilidad básica del sistema, para que pueda detectar incidentes y mantener su salud.

#### Criterios de Aceptación

1. THE system SHALL emitir logs estructurados en formato JSON usando AWS Lambda Powertools, incluyendo `correlation_id`, `request_id`, latencia, status y nombre del componente, hacia Amazon CloudWatch Logs
2. THE system SHALL publicar métricas custom hacia CloudWatch Metrics: throughput de mensajes, percentiles de latencia (p50/p95/p99), error rate y número de sesiones activas
3. THE system SHALL generar trazas distribuidas usando AWS X-Ray, con propagación automática del `correlation_id` a través de Lambda → Bedrock Agent → Action_Groups → Bank_Core_API
4. WHEN error rate > 5% en una ventana de 5 minutos, THE system SHALL disparar una alarma de CloudWatch hacia el equipo de operaciones
5. WHEN p95 de latencia > 5 segundos en una ventana de 5 minutos, THE system SHALL disparar una alarma de CloudWatch hacia el equipo de operaciones
6. THE system SHALL proveer un CloudWatch Dashboard con las métricas clave de salud y errores

### Requisito 14: Seguridad y Protección de Datos

**Historia de Usuario:** Como security architect, quiero que toda la data esté protegida en tránsito y en reposo con controles de acceso, para que la información del Bank_Client permanezca confidencial y se cumplan los requisitos regulatorios.

#### Criterios de Aceptación

1. THE system SHALL cifrar toda la data en reposo usando AWS KMS (AES-256), aplicado a tablas DynamoDB, buckets S3 y logs de CloudWatch
2. THE system SHALL cifrar toda la data en tránsito usando TLS 1.2 o superior
3. THE system SHALL autenticar todas las llamadas API entre componentes internos usando IAM roles con políticas de least-privilege
4. THE system SHALL enmascarar la data financiera sensible (números de cuenta, saldos) en logs, reteniendo solo los últimos 4 dígitos de los números de cuenta
5. WHEN se verifica la identidad de un Bank_Client, THE system SHALL emitir un session token con vigencia máxima de 30 minutos
6. THE system SHALL no almacenar credenciales del Bank_Client; toda la autenticación se delega al identity provider del banco vía redirección por Secure_Link
7. THE system SHALL cumplir con la Ley 1581 de 2012 (protección de datos personales en Colombia) respecto al manejo y almacenamiento de datos personales

---

## Fase 2 — Requisitos Diferidos

Esta sección documenta capacidades que **se identificaron pero se difieren** para no sobre-construir el MVP. Cada una incluye su **trigger** explícito — la señal cuantitativa o cualitativa que justifica activarla. No se implementan en el MVP; solo se documentan para que el equipo y stakeholders entiendan dónde queda la puerta abierta.

### F2.1: Fast Path Router para Consultas Simples

**Capacidad diferida:** Ruteo directo de consultas simples (saldo, últimas N transacciones) saltándose el Conversational_Agent para respuestas sub-2s.

**Trigger de activación:** p95 de latencia end-to-end > 5 segundos sostenido por > 2 semanas en queries identificadas como "simples" (saldo, últimos movimientos), con datos de CloudWatch que demuestren que el bottleneck es el reasoning de Bedrock y no la Bank_Core_API.

**Razón para diferir:** Bedrock Agents con Claude Haiku ya responde sub-2s para queries simples en la mayoría de los casos. Implementar un ruteo pre-Bedrock antes de validar el problema es premature optimization.

### F2.2: Fallback Engine con Templates Dedicado

**Capacidad diferida:** Engine de respuestas basadas en templates pre-renderizadas para cuando Bedrock esté no disponible, con health checks cada 10 segundos y 20+ templates.

**Trigger de activación:** Un incidente real de Bedrock con duración > 15 minutos y > 50 clientes impactados, O dos incidentes menores en un trimestre.

**Razón para diferir:** Bedrock tiene SLA del 99.9%. En el MVP, ante indisponibilidad de Bedrock, el WhatsApp_Gateway responde con un mensaje gentil ("nuestro asistente está temporalmente no disponible, intenta en unos minutos"). El esfuerzo de construir, mantener y testear 20+ templates con health checks es desproporcionado a la frecuencia esperada del problema.

### F2.3: Deduplicación con SQS FIFO Dedicada

**Capacidad diferida:** SQS FIFO queue con `ContentBasedDeduplication` entre WhatsApp_Gateway y Conversational_Agent.

**Trigger de activación:** Métrica de duplicados detectados (vía DynamoDB inline) > 0.1% del tráfico sostenido durante 1 semana, O bursts > 1,000 msg/min observados en logs.

**Razón para diferir:** El MVP detecta duplicados con un `PutItem` condicional sobre la tabla `Conversations` (`whatsapp_message_id` como key, TTL 5 min). Esto cubre el caso típico de re-entregas de WhatsApp con cero infraestructura adicional. SQS FIFO solo aporta cuando hay throughput sostenido lo suficientemente alto como para necesitar el buffer.

### F2.4: Mitigación de Cold Start con Provisioned Concurrency

**Capacidad diferida:** Provisioned concurrency 24/7 (5-30 instancias) sobre WhatsApp_Gateway y Conversational_Agent, pre-scaling 15 minutos antes de picos.

**Trigger de activación:** Cold start rate > 5% en ventana de 15 minutos durante 3 días consecutivos, **y** evidencia (vía logs o feedback de clientes) de impacto en la experiencia.

**Razón para diferir:** A volumen actual estimado (cientos de msg/min en pico), el cold start ocasional (<200ms para una Lambda en Node.js bien empaquetada) no es perceptible para el cliente final en un canal asíncrono como WhatsApp. Provisioned concurrency 24/7 cuesta dinero constante para un problema que aún no existe.

### F2.5: Límites de Invocación de Tools Configurables y Granulares

**Capacidad diferida:** Sistema de límites configurables vía SSM Parameter Store que distinga pasos internos (validación, audit, contexto) de pasos externos (tool calls visibles al usuario), con warning al 80%.

**Trigger de activación:** Workflows que consistentemente necesiten > 5 hops de tool calling (el tope default de Bedrock Agents) y donde los logs muestren que se está truncando reasoning legítimo.

**Razón para diferir:** Bedrock Agents tiene un tope nativo configurable de hops por turn (default razonable para queries normales). El sistema custom de límites con clasificación interna/externa solo aporta cuando hay workflows complejos demostrados, no en el MVP.

### F2.6: Capa de Resiliencia Avanzada para Bank Core API

**Capacidad diferida:** Capa Lambda dedicada con circuit breaker custom (5 fallas → open, 30s recovery), exponential backoff con jitter para HTTP 429, API version negotiation con soporte concurrente de versiones por 30 días, y response cache en DynamoDB (TTL 5 min).

**Trigger de activación:** Una cascada de fallas del core que llegue al cliente final, O un cambio de versión de la Bank_Core_API que requiera transición coordinada.

**Razón para diferir:** El AWS SDK ya implementa retries con exponential backoff por defecto. Para el MVP, los Action_Groups llaman directamente al core con la configuración por defecto del SDK. Una capa dedicada con circuit breaker y caching es valiosa pero no crítica hasta que haya señal real.

### F2.7: Queries Ad-Hoc de Compliance sobre > 1 Año

**Capacidad diferida:** Infraestructura Athena pre-configurada con tablas particionadas, vistas reutilizables y dashboards de compliance.

**Trigger de activación:** Compliance solicita queries recurrentes (mensuales o más frecuentes) sobre registros > 1 año, o se requiere capacidad self-service para auditores.

**Razón para diferir:** El MVP guarda los registros en S3 con estructura particionada por fecha (suficiente para Athena on-demand cuando se necesite). Crear vistas, dashboards y permisos pre-configurados antes de tener un caso de uso recurrente es trabajo que envejece sin generar valor.
