# howtoFunciones — Errores y Bugs en Creación de Workflows n8n

> **IMPORTANTE:** Leer este documento ANTES de crear un nuevo workflow JSON.
> Actualizar con cada error nuevo que se encuentre.

---

## Estructura de un workflow n8n

Cada workflow JSON necesita:
- `name` — nombre del workflow
- `nodes` — array de nodos (cada uno con `id`, `name`, `type`, `typeVersion`, `position`, `parameters`)
- `connections` — mapa de conexiones entre nodos (por nombre de nodo, no por ID)
- `active` — `false` por defecto (activar manualmente en n8n UI)
- `settings` — `{ "executionOrder": "v1" }`
- `meta` — `{ "instanceId": "" }`
- `tags` — array de strings

---

## Bugs y Errores Encontrados

### 1. Gmail OAuth2 — nodos deshabilitados hasta configurar credenciales
**Problema:** Los nodos `n8n-nodes-base.gmail` fallan si no hay credencial OAuth2 configurada en n8n.
**Síntoma:** Error al importar o ejecutar el workflow.
**Solución:** Importar el JSON primero, luego asignar credencial Gmail desde n8n UI > Credentials.
**Credencial necesaria:** `gmailOAuth2` con ID placeholder `GMAIL_CREDENTIAL_ID`.

---

### 2. PostgreSQL — strftime() no funciona en Supabase
**Problema:** `strftime('%Y-%m', ...)` es sintaxis SQLite. PostgreSQL falla con IndexError porque `%` se interpreta como placeholder de parámetro en psycopg2.
**Síntoma:** `IndexError: not enough arguments for format string`
**Solución en connector.py:**
```python
if connector._USE_SQLITE:
    month_expr = "strftime('%Y-%m', datetime(date, 'unixepoch'))"
else:
    month_expr = "TO_CHAR(TO_TIMESTAMP(date), 'YYYY-MM')"
```

---

### 3. Nodo Merge con 3 entradas — índices de conexión
**Problema:** El nodo `merge` (tipo `combine`) acepta múltiples inputs. Las conexiones deben usar `index: 0`, `index: 1`, `index: 2` en el destino, no todas a `index: 0`.
**Síntoma:** Solo llega un input al merge, el resto se pierde.
**Solución:** En `connections`, cada nodo origen conecta al merge con su índice correspondiente:
```json
"NodoA": { "main": [[{ "node": "Unir datos", "type": "main", "index": 0 }]] },
"NodoB": { "main": [[{ "node": "Unir datos", "type": "main", "index": 1 }]] },
"NodoC": { "main": [[{ "node": "Unir datos", "type": "main", "index": 2 }]] }
```

---

### 4. Nodo Supabase — filterString con PostgREST syntax
**Problema:** El filtro usa sintaxis PostgREST, no SQL estándar.
**Ejemplos correctos:**
```
status=eq.4                          → status = 4
status=in.(1,2)                      → status IN (1, 2)
due_date=not.is.null                 → due_date IS NOT NULL
status=in.(1,2)&order=due_date.asc.nullslast
```
**Síntoma:** Sin resultados o error 400 si la sintaxis es incorrecta.

---

### 5. Nodo Supabase — campos de sync_logs
**Problema:** La tabla `sync_logs` tiene columnas `invoices`, `purchases`, `contacts`, `products` (integers). El endpoint `/api/sync` devuelve `record_counts` con claves `invoices`, `purchase_invoices`, `contacts`, `products`.
**Mapeo correcto en fieldsUi:**
```json
{"fieldId": "invoices",   "fieldValue": "={{ $json.record_counts.invoices }}"},
{"fieldId": "purchases",  "fieldValue": "={{ $json.record_counts.purchase_invoices }}"},
{"fieldId": "contacts",   "fieldValue": "={{ $json.record_counts.contacts }}"},
{"fieldId": "products",   "fieldValue": "={{ $json.record_counts.products }}"}
```

---

### 6. Nodo If — condición sobre longitud de array
**Problema:** `$input.all().length` dentro de un nodo If no funciona directamente como `leftValue` string.
**Solución probada:**
```json
"leftValue": "={{ $input.all().length }}",
"rightValue": 0,
"operator": { "type": "number", "operation": "gt" }
```
Funciona en `typeVersion: 2`. No funciona igual en v1.

---

### 7. n8n detrás de Nginx — proxy headers
**Problema:** n8n 2.9.4 lanza `ERR_ERL_UNEXPECTED_X_FORWARDED_FOR` si recibe X-Forwarded-For y no sabe cuántos proxies hay.
**Solución:** Añadir en `docker-compose.yml` bajo el servicio n8n:
```yaml
environment:
  N8N_PROXY_HOPS: "1"
```

---

### 8. IDs de nodos — deben ser UUIDs únicos
**Problema:** Copiar un nodo sin cambiar su `id` causa conflictos al importar.
**Solución:** Generar UUID v4 único para cada nodo. Formato: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.

---

## Credenciales requeridas (placeholders en JSON)

| Placeholder | Tipo | Descripción |
|---|---|---|
| `SUPABASE_CREDENTIAL_ID` | `supabaseApi` | Credencial Supabase en n8n |
| `GMAIL_CREDENTIAL_ID` | `gmailOAuth2` | Gmail OAuth2 en n8n |

Asignar desde n8n UI > Credentials después de importar cada workflow.

---

### 9. Nodo Merge con 3+ fuentes distintas — usar `append` en lugar de `combine`
**Problema:** El modo `combine` en el nodo Merge intenta hacer join por campos comunes. Cuando las 3 entradas tienen estructuras distintas (ej: resumen financiero + stats mensuales + facturas), no funciona correctamente.
**Síntoma:** El nodo "Unir datos" no produce output, o produce output vacío/incorrecto.
**Solución:** Usar `mode: "append"` para simplemente concatenar todos los items de todos los inputs en un array:
```json
{
  "mode": "append",
  "options": {}
}
```
El código JS posterior usa `$input.all()` e identifica cada tipo de dato por sus campos propios (`total_income`, `month`, `doc_number`, etc.), por lo que `append` es suficiente.
**Afecta a:** Workflow 04 — Informe Semanal (nodo "Unir datos").

---

### 10. /api/summary y /api/stats/monthly — estructura real de respuesta
**Problema:** Los campos devueltos por la API no son planos. El JS del workflow debe acceder correctamente.

**`GET /api/summary` devuelve:**
```json
{ "counts": {...}, "totals": { "income": 382206.89, "expenses": 151096.22, "balance": 231110.67 } }
```
→ Identificar por `i.json.totals !== undefined`. Usar `item.json.totals.income`, `.expenses`, `.balance`.

**`GET /api/stats/monthly` devuelve:**
```json
{ "income": [{"month": "2025-03", "total": 17931.86}, ...], "expenses": [{...}] }
```
→ Identificar por `Array.isArray(i.json.income)`. Son dos arrays separados — combinar por mes con un `monthMap`.

**Patrón correcto para combinar por mes:**
```js
const monthMap = {};
incomeArr.forEach(m => { monthMap[m.month] = { income: m.total, expenses: 0 }; });
expensesArr.forEach(m => {
  if (monthMap[m.month]) monthMap[m.month].expenses = m.total;
  else monthMap[m.month] = { income: 0, expenses: m.total };
});
const months = Object.keys(monthMap).sort().slice(-4);
```

---

## Checklist antes de crear un nuevo workflow

- [ ] ¿Usa Supabase? → Revisar bug #4 (filterString syntax)
- [ ] ¿Usa Gmail? → Revisar bug #1 (OAuth2 debe estar configurado)
- [ ] ¿Tiene nodo Merge? → Revisar bug #3 (índices de conexión)
- [ ] ¿Escribe en sync_logs? → Revisar bug #5 (mapeo de campos)
- [ ] ¿Tiene nodo If con array? → Revisar bug #6
- [ ] ¿Todos los IDs de nodo son únicos? → Revisar bug #8
