# 03 — Recordatorio Cobro a Clientes

**Archivo JSON:** `docs/n8n-flows/03-recordatorio-cobro-clientes.json`
**Tags:** holded, recordatorios, cobro
**Estado:** Importado en n8n, credenciales asignadas

---

## ¿Qué hace?

Cada día a las 08:30 revisa facturas pendientes de cobro (status 1 o 2) con fecha de vencimiento. Para las que vencen en exactamente 5 días, hoy, o vencieron hace 3 días, genera un borrador de email de recordatorio personalizado por factura.

---

## Flujo

```
[Diario a las 08:30]
    ↓
[Facturas próximas a vencer]   → Supabase: status IN (1,2), due_date no nulo
    ↓
[Filtrar vencimientos relevantes] → JS: filtra días -3, 0, +5; añade days_until_due y reminder_type
    ↓
[¿Hay recordatorios?]          → IF: count > 0
    ↓ (sí)                ↓ (no)
[Loop por factura]          [fin]
    ↓ (por cada una)
[Preparar borrador recordatorio]  → JS: genera subject + HTML personalizado
    ↓
[Gmail — Crear Borrador]
    ↓ (vuelve al loop)
```

---

## Nodos

| Nodo | Tipo | Descripción |
|------|------|-------------|
| Diario a las 08:30 | scheduleTrigger | Cron `30 8 * * *` |
| Facturas próximas a vencer | supabase | getAll de `invoices` con `status=in.(1,2)&due_date=not.is.null` |
| Filtrar vencimientos relevantes | code (JS) | Filtra TARGET_DAYS = [5, 0, -3] |
| ¿Hay recordatorios? | if | `$input.all().length > 0` |
| Loop por factura | splitInBatches | batchSize: 1 (procesa de una en una) |
| Preparar borrador recordatorio | code (JS) | Genera subject y HTML según tipo de recordatorio |
| Gmail — Crear Borrador | gmail | `create draft` → miguelbenajes@gmail.com |

---

## Tipos de recordatorio

| `days_until_due` | `reminder_type` | Asunto |
|-----------------|-----------------|--------|
| +5 | `previo` | "Recordatorio: factura vence en 5 días" |
| 0 | `vencimiento` | "Aviso: factura vence hoy" |
| -3 | `seguimiento` | "Seguimiento: factura vencida hace 3 días" |

---

## Email generado (por factura)

- **Asunto:** Depende del `reminder_type` + nº de factura
- **Cuerpo:** Email formal con tabla (nº factura, importe, fecha vencimiento)
- **Saludo:** Personalizado con `contact_name`
- **Destino:** Borrador en miguelbenajes@gmail.com

---

## Credenciales necesarias

- `supabaseApi` → credencial "Supabase account"
- `gmailOAuth2` → credencial "Gmail account"

---

## Notas

- El loop `splitInBatches` con batchSize=1 permite crear un borrador separado por cada factura.
- Si hay 3 facturas relevantes, se crean 3 borradores independientes.
- El nodo Loop tiene salida `main[0]` (hay más) y `main[1]` (terminó) — solo `main[0]` conecta al siguiente nodo.
