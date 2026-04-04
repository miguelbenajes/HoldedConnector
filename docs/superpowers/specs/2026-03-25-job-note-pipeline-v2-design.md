# Job Note Pipeline v2 — Design Spec

**Date:** 2026-03-25
**Status:** Draft
**Replaces:** Parte de `2026-03-12-job-tracker-design.md` (sync estático)

---

## Problema

El pipeline actual regenera la nota de Obsidian desde cero en cada sync, machacando ediciones manuales (gastos, emails, notas). Además, la nota es pasiva — un dossier de referencia sin capacidad de disparar acciones.

## Solución

La nota de Obsidian se convierte en **centro de mando** del trabajo. La IA lee la nota, interpreta qué falta por hacer según el contexto, actúa, verifica que la acción se completó realmente, y solo entonces marca `[x]`. Si tiene dudas, pregunta a Miguel por Telegram. Las respuestas alimentan la memoria para no repetir preguntas.

---

## Principios

1. **La nota es la fuente de verdad** — no se regenera, se actualiza incrementalmente
2. **Checkbox = verificación, no acción** — solo se marca `[x]` tras comprobar que está hecho de verdad
3. **IA interpreta, no checklist fijo** — no hay lista estática de tareas; la IA lee el contexto y decide
4. **Sin dudas sin resolver** — si la IA no está segura, pregunta por Telegram antes de actuar
5. **Learning loop** — cada pregunta/respuesta alimenta la memoria para futuras decisiones
6. **Executor-agnostic** — da igual quién dispare la acción (cron, Brain, Miguel manual)

---

## Flow Principal

### 1. Trigger: Detección de documento nuevo/modificado

```
Holded sync (cron cada 15min via n8n)
  → ¿Documento nuevo con project_code?
     → SÍ: event-driven → Brain revisa esa nota
     → NO: nada

Cron periódico (Brain, cada 3h de 8:00 a 22:00)
  → Escanea todas las notas con status != closed
  → Para cada una: leer, interpretar, actuar si hay pendientes
  → Follow-ups: reminders 7 días, vencimientos, etc.
  → Horarios: 8:00, 11:00, 14:00, 17:00, 20:00, 22:00
```

### 2. Nota: Crear o actualizar

```
¿Existe nota en Obsidian con esa REF?
  NO → Crear nota con datos del documento (template inicial)
       + Pedir fechas del proyecto a Miguel por Telegram si no las tiene
       + Crear evento en Google Calendar ("compartido") con las fechas
  SÍ → Leer nota existente
       + Actualizar datos de Holded (precio, status, etc.)
       + PRESERVAR todo contenido manual (gastos, notas, ediciones)
```

### 3. Interpretación: IA lee y decide

Brain lee la nota completa + consulta estado real en Holded/Gmail. Según el tipo de documento y su estado, decide qué acciones corresponden.

No hay checklist predefinido. La IA razona sobre el contexto:
- ¿Hay gastos anotados en la nota que no están en Holded? → Acción
- ¿Hay items añadidos/modificados en la nota? → Acción
- ¿Se creó factura pero no se envió email? → Acción
- ¿Factura enviada hace >7 días sin respuesta? → Reminder

### 4. Ciclo de cada acción

```
1. Interpretar qué falta (leer nota + Holded + Gmail)
2. Ejecutar acción (Holded API, Gmail draft, etc.)
3. Verificar en el sistema destino que se hizo realmente
4. Si necesita aprobación de Miguel → preguntar por Telegram
5. Solo marcar [x] cuando está verificado
6. Añadir timestamp y detalle en la nota
```

---

## Acciones por Tipo de Documento

### Presupuesto (Estimate)

La IA interpreta la nota buscando:

| Señal en la nota | Acción | Verificación |
|-----------------|--------|-------------|
| Gastos anotados a mano sin `[x]` | Añadir como line items o compras en Holded | Re-leer Holded, confirmar que aparecen |
| Horas extras registradas | Añadir line item en Holded | Re-leer Holded |
| Items añadidos/modificados (sección manual) | Actualizar presupuesto en Holded | Re-leer, comparar, pedir OK a Miguel |
| Todo completado (shooting terminado, gastos OK) | Sugerir crear borrador de factura | Preguntar a Miguel por Telegram |
| Fechas de rodaje no definidas | Preguntar a Miguel por Telegram | Cuando responda, añadir a nota + Calendar |

### Borrador de Factura (Draft Invoice)

| Señal | Acción | Verificación |
|-------|--------|-------------|
| Borrador creado, no enviado al cliente | Preparar Gmail draft con PDF adjunto | Verificar draft existe en Gmail |
| Draft enviado, esperando aprobación | Monitorear (cron diario) | Comprobar respuesta en Gmail |
| >7 días sin respuesta del cliente | Avisar a Miguel + sugerir draft reminder | Preguntar a Miguel si enviar |
| Cliente aprobó | Notificar a Miguel para aprobar en Holded | Miguel confirma por Telegram |

### Factura Aprobada (Approved Invoice)

| Señal | Acción | Verificación |
|-------|--------|-------------|
| Aprobada, no enviada | Enviar por Gmail con PDF | Verificar en Gmail sent |
| Enviada, pendiente de pago | Monitorear vencimiento | Comprobar `payments_pending` en Holded |
| Vencida sin pagar | Avisar a Miguel | Telegram notification |
| Pagada | Marcar job como closed | Verificar status en Holded |

---

## Google Calendar Integration

- Al crear una nota con fechas de shooting → crear evento en Google Calendar (calendario "compartido")
- Si no hay fechas en el documento → preguntar a Miguel por Telegram
- Formato del evento: `[CODE] - Cliente` (ej. `[BIRK-18032026] - ANNEX 00, SCP`)
- Evento de día completo, abarcando todas las fechas de shooting
- Si las fechas cambian en la nota → actualizar el evento existente
- Brain ya tiene skills de gcal (`gcal_create_event`, `gcal_update_event`, `gcal_list_events`)

---

## Learning Loop

Mismo patrón que Gaffer SP5:

1. La IA tiene una duda → pregunta a Miguel por Telegram
2. Miguel responde
3. La respuesta se guarda en memoria de Brain (contexto del proyecto/cliente)
4. Próxima vez que surja la misma situación → la IA actúa sin preguntar

**Ejemplos de aprendizaje:**
- "¿A quién envío la factura de ANNEX 00?" → Miguel: "A Luciagrau@mac.com, no al email de Holded" → Memoria: contacto facturación ANNEX 00 = Luciagrau@mac.com
- "¿Incluyo las horas extra en la factura?" → Miguel: "Sí, siempre como line item separado" → Memoria: horas extra = line item separado
- "¿Envío reminder después de 7 días?" → Miguel: "Para este cliente sí, pero para Netflix espera 14" → Memoria: reminder Netflix = 14 días

---

## Resiliencia ante el Caos Humano

### Preguntas pendientes de respuesta

Brain mantiene una **cola de preguntas pendientes** por job en la nota (frontmatter o sección dedicada):

```yaml
---
pending_questions:
  - id: "q1_birk_dates"
    asked_at: "2026-03-25T14:00"
    question: "¿Fechas de rodaje para BIRK?"
    reminded_at: null
---
```

**Reglas:**
- Si hay pregunta pendiente sin respuesta → **no repetir** en el siguiente cron
- Si lleva >24h sin respuesta → enviar **un** reminder por Telegram ("Sigo esperando tu respuesta sobre BIRK...")
- Si lleva >72h → dejar de insistir, marcar como `stale` en la nota
- **Máximo 2 preguntas por job por ejecución de cron** — no bombardear
- **Máximo 5 preguntas totales por ejecución de cron** (across all jobs) — agrupar si hay muchas

### Vincular respuesta a pregunta

Cuando Miguel contesta por Telegram:
1. Brain busca en `pending_questions` de todos los jobs abiertos
2. Usa contexto conversacional para vincular respuesta → pregunta (ya lo hace el router de Brain)
3. Si la respuesta es ambigua ("sí") → Brain pide clarificación: "¿Te refieres a BIRK o a HOFF?"
4. Al procesar la respuesta: borrar de `pending_questions`, guardar en memoria, actuar

### Deduplicación entre crons

Cada ejecución de cron registra un **hash de estado** por job:

```yaml
---
last_review:
  at: "2026-03-25T14:00"
  state_hash: "abc123"  # hash de: nota + holded state + gmail threads
  actions_taken: ["added_expense_taxi", "asked_dates"]
---
```

- Si `state_hash` no cambió desde el último cron → **skip** (nada nuevo que procesar)
- Si cambió → re-evaluar y actuar solo sobre lo nuevo
- Esto evita: preguntas duplicadas, acciones repetidas, spam a Miguel

### Race conditions (edición simultánea)

- Brain **lee** la nota al inicio del review
- Brain **escribe** solo las líneas que modifica (checkboxes, timestamps)
- Si la nota cambió entre lectura y escritura → **re-leer y mergear** antes de escribir
- Usar `notes_hash` (ya existe en DB) como optimistic lock

### Checkboxes manuales

- Si Miguel marca `[x]` a mano en Obsidian → Brain lo **respeta** y no lo desmarca
- Brain solo marca checkboxes, nunca los desmarca
- Si Brain detecta un `[x]` que no verificó él → lo acepta sin cuestionar

### Fallos de servicios externos

| Servicio caído | Comportamiento |
|---------------|---------------|
| Holded API | No marcar [x], reintentar en siguiente cron |
| Gmail API | Idem — draft queda pendiente |
| Google Calendar | Crear evento queda pendiente, anotar en nota |
| Telegram | Cola de mensajes pendientes (Brain ya maneja esto) |
| CouchDB/Obsidian | No escribir, reintentar en siguiente cron |

Regla general: **si no puedo verificar, no marco hecho. Reintento en el siguiente ciclo.**

### Cancelación por Telegram

Si Miguel dice "cancela BIRK" o "olvídate de BIRK":
1. Brain marca el job como `closed` en la DB
2. Actualiza la nota con status closed
3. Borra `pending_questions`
4. Confirma por Telegram: "BIRK-18032026 cerrado. No procesaré más."

### Horarios de notificación

- **No enviar mensajes de Telegram antes de las 8:00 ni después de las 22:00**
- Si el cron de las 22:00 genera preguntas → encolarlas para las 8:00 del día siguiente
- Excepciones: facturas vencidas >30 días (urgente) → enviar siempre

### Vacation mode

- Miguel activa con `/vacation on` por Telegram
- Mientras está activo: sin Telegram (todo encolado), sin follow-ups, solo alertas críticas
- Al desactivar `/vacation off`: Brain envía resumen de todo lo acumulado
- Estado guardado en core_memory (persiste entre reinicios)

### Códigos de proyecto duplicados

- Si Miguel crea 2 presupuestos para el mismo cliente el mismo día → mismo código `BIRK-25032026`
- Brain verifica si el código ya existe antes de sugerir
- Si existe → sugiere `BIRK-25032026-B`, `BIRK-25032026-C`
- Miguel siempre puede elegir otro código

---

## Bugs, Fallos y Edge Cases

### Integridad de datos

| Fallo | Impacto | Mitigación |
|-------|---------|-----------|
| Frontmatter de nota corrupto (YAML roto) | Brain no puede leer la nota | Catch parse error, notificar a Miguel por Telegram, no tocar la nota |
| CouchDB y filesystem desincronizados | Nota se ve distinta en iPhone vs servidor | LiveSync reconcilia, pero puede tardar minutos. Brain solo lee del filesystem (fuente canónica en servidor) |
| DB dice que el job existe pero la nota no | Brain intenta leer nota → not found | Re-crear nota desde datos de DB (ya lo hace el sync actual) |
| Nota existe pero el job se borró de DB | Nota huérfana en Obsidian | Cron detecta notas sin job en DB → avisar a Miguel, no borrar |
| Dos notas para el mismo project_code | Confusión, acciones duplicadas | Buscar por `project_code` en frontmatter, si hay >1 → avisar, no actuar |
| Presupuesto borrado en Holded pero job/nota sigue | Brain sigue procesando un doc que no existe | Al verificar en Holded y no encontrar → marcar como `cancelled`, notificar |
| Holded doc modificado (nueva versión del presupuesto) | Nota tiene datos antiguos | El sync de holded-connector actualiza DB → trigger event → Brain re-lee |

### Concurrencia

| Fallo | Impacto | Mitigación |
|-------|---------|-----------|
| Dos crons se solapan (el anterior no terminó) | Acciones duplicadas, preguntas repetidas | **Concurrency guard** — flag `job_review_running` en DB. Si está activo, el nuevo cron se salta. Timeout de 30min para liberar el flag si el anterior murió |
| Event-driven trigger llega mientras el cron está corriendo | Mismo job procesado dos veces simultáneamente | El event-driven encola en `job_note_queue`, el cron lo procesa. No ejecución directa |
| Miguel edita nota en iPhone mientras Brain escribe | Brain machaca la edición de Miguel | Brain lee → actúa → re-lee antes de escribir. Si cambió entre lectura y escritura → mergear, no sobrescribir |
| Brain se reinicia a mitad de un review | Acción ejecutada pero [x] no marcado | No es grave — el siguiente cron re-evalúa. La verificación detecta que ya está hecho y marca [x]. Idempotencia por diseño |
| Replicación CouchDB lenta (Miguel edita en iPhone, tarda en llegar al servidor) | Brain lee versión antigua | Aceptable — en el siguiente ciclo de cron (3h) ya estará sincronizado. Si es urgente, Miguel puede forzar via Telegram |

### Alucinaciones y errores de la IA

| Fallo | Impacto | Mitigación |
|-------|---------|-----------|
| LLM interpreta un comentario como instrucción ("quizás añadir X" → añade X) | Acción no deseada en Holded | **Toda acción de escritura en Holded requiere confirmación de Miguel por Telegram.** Solo las verificaciones (leer estado) son automáticas |
| LLM confunde dos jobs del mismo cliente | Email/factura enviada al job equivocado | Siempre usar `project_code` como clave, nunca nombre de cliente. Incluir REF en cada acción |
| LLM inventa un checkbox que no existía | Marca [x] algo que Miguel no escribió | Brain solo marca checkboxes existentes `[ ]` → `[x]`, nunca crea checkboxes nuevos sin pedir confirmación |
| LLM decide actuar con baja confianza | Acción incorrecta | **Threshold de confianza**: si la IA no está >80% segura → preguntar a Miguel en vez de actuar |
| LLM parsea mal la tabla de gastos | Añade gasto incorrecto a Holded | Mostrar a Miguel por Telegram el detalle exacto antes de añadir: "Voy a añadir 'taxi Shelphy 14,75€' al presupuesto BIRK. ¿OK?" |

### Fallos humanos

| Situación | Impacto | Mitigación |
|-----------|---------|-----------|
| Miguel crea presupuesto SIN "Proyect REF:" | No se crea job ni nota — invisible para el sistema | Cron diario de auditoría: buscar presupuestos recientes en Holded sin project_code → avisar por Telegram |
| Miguel usa formato de código incorrecto ("birk" en vez de "BIRK-18032026") | Job no se vincula correctamente | `_extract_project_code()` ya es case-insensitive. Añadir validación de formato y sugerir corrección |
| Miguel edita nota en Obsidian mobile con formato roto (tablas mal formateadas) | Brain no puede parsear la tabla de gastos | Parseo tolerante — si la tabla está rota, extraer lo que se pueda + avisar a Miguel |
| Miguel contesta "sí" a una pregunta de hace 3 días, Brain ya no recuerda | Respuesta huérfana | `pending_questions` en frontmatter persiste entre sesiones. Brain siempre puede vincular |
| Miguel marca [x] un gasto que NO está en Holded | Brain cree que está hecho pero no lo está | En el cron, Brain verifica todos los [x] contra Holded. Si un gasto marcado no existe → avisar: "Marcaste 'taxi' como hecho pero no está en Holded. ¿Lo añado?" |
| Miguel borra una línea de la nota sin querer | Datos perdidos | No hay recovery automático — pero el sync de Holded puede re-generar datos que vengan de la DB. Contenido manual perdido es irrecuperable (como cualquier edición en Obsidian) |
| Miguel crea factura manualmente en Holded sin pasar por Brain | Brain no se entera hasta el sync | El sync de holded-connector detecta facturas nuevas con project_code → crea/actualiza job → trigger event → Brain revisa |
| Cliente responde a un email viejo, no al hilo con REF | Brain no encuentra la respuesta buscando por REF | Buscar también por nombre de cliente + subject similar. Si no encuentra → no marcar, Miguel verifica manualmente |
| Miguel reenvía presupuesto (nueva versión) con mismo project_code | Dos estimates en Holded para el mismo job | `ensure_job()` ya maneja upsert — actualiza el estimate_id al más reciente. Nota se actualiza con nuevo PDF |

### Seguridad crítica

| Riesgo | Impacto | Mitigación |
|--------|---------|-----------|
| Brain envía email con factura al contacto equivocado | Datos financieros expuestos a tercero | **NUNCA enviar email automáticamente sin draft previo.** Siempre: crear Gmail draft → mostrar a Miguel por Telegram (destinatario, asunto, adjuntos) → Miguel aprueba → enviar |
| Brain aprueba factura en Holded sin OK de Miguel | Factura enviada a Hacienda (irreversible) | **PROHIBIDO.** El sistema NUNCA llama a `approveDoc`. Solo Miguel puede aprobar facturas. Brain solo crea borradores |
| Brain modifica presupuesto en Holded con datos incorrectos | Cliente ve precios erróneos | Toda modificación de items → preview a Miguel por Telegram antes de ejecutar |
| Telegram envía detalles de job a chat equivocado | Info confidencial filtrada | Brain solo envía a `ADMIN_CHAT_ID` (configurado, no dinámico). Nunca a grupos ni a otros usuarios |

### Auditoría de presupuestos sin REF

**Fecha de corte:** 2026-03-25 (hoy). A partir de esta fecha, todo presupuesto en Holded debe tener "Proyect REF:".

**Cron de auditoría** (se ejecuta junto con el review periódico):

```
1. Buscar en Holded estimates con fecha >= 2026-03-25 que NO tienen project_code
2. Para cada uno:
   a. Consultar contacto → obtener nombre del cliente
   b. Preguntar a Miguel por Telegram:
      "Nuevo presupuesto para THE HOFF BRAND del 25/3, sin REF.
       ¿Le pongo HOFF-25032026 o prefieres otro código?"
   c. Miguel responde → Brain:
      - Añade el line item "Proyect REF:" con el código en Holded
      - Crea el job en DB
      - Crea la nota en Obsidian
      - Guarda en memoria: THE HOFF BRAND → HOFF (learning loop)
3. Próxima vez que aparezca THE HOFF BRAND sin REF:
   - Brain sugiere HOFF-DDMMYYYY directamente (ya aprendió)
   - Sigue pidiendo confirmación por Telegram (pero la sugerencia será correcta)
```

**Learning loop para códigos de cliente:**
- Brain memoriza la relación `nombre_holded → código_corto` (ej. "THE HOFF BRAND, S.L." → "HOFF")
- Después de 2-3 presupuestos del mismo cliente, la sugerencia será siempre correcta
- Miguel siempre tiene la última palabra — el código sugerido se puede cambiar

### Idempotencia (regla de oro)

**Cada acción debe poder ejecutarse N veces sin efectos secundarios.**

- Añadir gasto a Holded → verificar primero si ya existe (por concepto + importe + fecha)
- Crear evento en Calendar → verificar si ya existe `calendar_event_id` en frontmatter
- Enviar Gmail draft → verificar si ya existe draft con esa REF
- Marcar checkbox → verificar que está en `[ ]` antes de cambiar a `[x]`

Si ya está hecho → skip silencioso, no error.

---

## Preservación de Contenido Manual (ya implementado)

El fix desplegado hoy maneja esto:

- **Secciones por encima del MANUAL_MARKER** (Expenses, Email Thread): si tienen contenido manual y el render nuevo solo tiene defaults, se preserva el manual
- **Email Thread**: si difiere del template, se preserva la versión existente
- **Todo debajo del MANUAL_MARKER**: siempre preservado

En v2 esto sigue siendo relevante para los casos donde el sync de Holded actualiza datos de la nota. La regla: **datos manuales nunca se pierden; datos de Holded actualizan lo que les corresponde**.

---

## Arquitectura Técnica

### Dónde vive cada cosa

| Componente | Servicio | Responsabilidad |
|-----------|----------|----------------|
| Sync Holded → DB | holded-connector | Detectar documentos nuevos/modificados, extraer project_code |
| Crear/actualizar nota | Brain | Leer template, preservar contenido, escribir a Obsidian |
| Interpretar nota + actuar | Brain | Leer nota, razonar, ejecutar acciones, verificar, marcar |
| Acciones en Holded | Brain (via holded skill) | Añadir items, crear facturas, etc. |
| Acciones en Gmail | Brain (via gmail skill) | Drafts, envíos, verificar respuestas |
| Acciones en Calendar | Brain (via gcal skill) | Crear/actualizar eventos |
| Preguntas a Miguel | Brain (via telegram) | Dudas, confirmaciones, reminders |
| Learning loop | Brain (memoria) | Guardar respuestas para futuras decisiones |

### Trigger: Event-driven (holded-connector → Brain)

Cuando `ensure_job()` detecta un cambio (create o update con cambios reales):

```python
# En ensure_job(), después de queue note sync:
if action:
    # Notify Brain to review this job
    requests.post(f"{BRAIN_API_URL}/internal/job-review",
        json={"project_code": project_code, "action": action},
        headers={"x-api-key": BRAIN_INTERNAL_KEY},
        timeout=10)
```

### Trigger: Cron periódico (Brain)

```typescript
// Brain cron: cada 3h de 8:00 a 22:00 (8,11,14,17,20,22)
async function periodicJobReview() {
  const openJobs = await getOpenJobs(); // holded skill
  for (const job of openJobs) {
    const note = await readNote({ path: job.note_path });
    await reviewJobNote(job, note); // IA interpreta y actúa
  }
}
```

### Brain: reviewJobNote()

Nueva función central en Brain. No tiene checklist hardcodeado — usa el LLM para interpretar:

```typescript
async function reviewJobNote(job: Job, noteContent: string) {
  // 1. Gather context
  const holdedState = await getJobDetail(job.project_code);  // estado real en Holded
  const gmailThreads = await searchGmail(`REF: ${job.project_code}`); // emails relacionados
  const calEvents = await searchCalendar(job.project_code);  // eventos existentes

  // 2. Ask LLM to interpret and decide
  const prompt = buildReviewPrompt(noteContent, holdedState, gmailThreads, calEvents);
  const actions = await llmDecide(prompt); // returns list of actions with confidence

  // 3. Execute actions with verification
  for (const action of actions) {
    if (action.needsConfirmation) {
      await askMiguel(action.question); // Telegram
      // Wait for response, save to memory
    } else {
      const result = await executeAction(action);
      const verified = await verifyAction(action, result);
      if (verified) {
        await markCheckbox(job.note_path, action.checkboxLine);
      }
    }
  }
}
```

### Nuevo endpoint Brain

```
POST /internal/job-review
  Body: { project_code, action: "create"|"update" }
  Auth: x-api-key (BRAIN_INTERNAL_KEY)
  Response: { reviewed: true, actions_taken: [...] }
```

---

## Formato de Nota (v2)

La nota mantiene estructura libre para que la IA interprete, pero con secciones reconocibles:

```markdown
---
project_code: "BIRK-18032026"
client: "ANNEX 00, SCP"
status: open
type: estimate  # estimate | draft_invoice | invoice
shooting_dates: ["2026-03-26", "2026-03-27"]
calendar_event_id: "abc123"  # Google Calendar event ID
tags: [coyote, job, seguimiento]
---

# BIRK-18032026

> **Client:** ANNEX 00, SCP
> **Shooting:** 26/03 - 27/03
> **Status:** 🟢 open (estimate)

---

## Quote
![[BIRK-18032026_QUOTE-260016.pdf]]

## Gastos y Extras

| Fecha | Concepto | Importe | En Holded |
|-------|----------|---------|-----------|
| 25/3  | taxi Shelphy | 14,75€ | [x] |
| 25/3  | canon selphy con papel | — | [ ] |
| 26/3  | horas extra iluminador 3h | 180€ | [ ] |

## Email y Contacto
- **REF:** BIRK-18032026
- **Facturación:** Luciagrau@mac.com
- **Producción:** producer@annex00.com

## Notas
- Cliente pide factura antes del 5 de abril
- Descuento 10% acordado por volumen

## Modificaciones al Presupuesto
- [ ] Añadir Sony FX3 x1 @150€/día
- [x] Quitar Ronin RS3 (no se usó)

## Facturación
<!-- Esta sección aparece cuando el proyecto avanza a factura -->
- [ ] Borrador de factura creado
- [ ] Email enviado al cliente con draft
- [ ] Cliente aprobó
- [ ] Factura aprobada en Holded
- [ ] Factura final enviada
- [ ] Pago recibido
```

**Nota:** Esta estructura es orientativa. La IA no depende de secciones exactas — interpreta el contenido libremente. Miguel puede escribir en formato libre y la IA lo entiende.

---

## Verificación y Testing

### Cómo probar el pipeline E2E

1. **Crear presupuesto en Holded** con "Proyect REF:" → verificar que aparece nota en Obsidian
2. **Editar nota a mano** (añadir gasto) → esperar al cron o forzar review → verificar que aparece en Holded
3. **Crear borrador factura** → verificar que Brain detecta y prepara Gmail draft
4. **Simular 7 días sin respuesta** → verificar que Brain avisa por Telegram
5. **Responder por Telegram** → verificar que Brain aprende y no repite la pregunta

### Tests automatizados

- Unit tests para `_preserve_manual_content` (ya implementados, 42 pasando)
- Integration test: `POST /internal/job-review` con nota mock → verificar acciones decididas
- E2E: crear presupuesto test → review → verificar nota + Holded + Calendar

---

## Migración desde v1

1. Las notas existentes (HOFF, BIRK) siguen funcionando — la IA las interpreta tal cual
2. El sync de holded-connector sigue creando/actualizando notas (con preservación de contenido manual)
3. Se añade el trigger event-driven (`POST /internal/job-review`)
4. Se añade el cron diario en Brain
5. Se implementa `reviewJobNote()` en Brain

No hay migración destructiva — v2 es aditivo sobre v1.

---

## Dependencias

- Brain skills existentes: holded, gmail, gcal, obsidian, telegram
- Fix de preservación de contenido manual (desplegado 2026-03-25)
- Learning loop de Brain (memoria 4-tier, ya implementado)

## Reutilización con Gaffer

Este pipeline se diseña para ser reutilizado por Gaffer al gestionar presupuestos con clientes vía WhatsApp/Telegram. Arquitectura modular:

| Capa | Archivo | Reutilizable | Qué contiene |
|------|---------|-------------|-------------|
| **Engine** | `src/shared/note-review-engine.ts` | ✅ Sí | Pending questions, quiet hours, calendar sync, rate limits, learning loop helpers |
| **Brain Jobs** | `src/skills/job-review.ts` | No (Holded-specific) | Presupuesto audit, invoice follow-up, Brain cron |
| **Gaffer (futuro)** | `src/skills/gaffer-review.ts` | No (Gaffer-specific) | Quote flow follow-up, WhatsApp client comms |

Gaffer importará `note-review-engine.ts` y creará su propio orchestrator con configuración diferente (calendar distinto, flujo de aprobación WhatsApp, etc.).

---

## Fuera de alcance (por ahora)

- Dashboard web de jobs (la nota de Obsidian es suficiente)
- Automatización de aprobación en Holded (siempre requiere confirmación de Miguel por el tema Hacienda/SII)
- Multi-currency (todo en EUR)
- Gaffer review (se implementará después, reutilizando el engine)
