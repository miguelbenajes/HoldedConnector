# 02 — Alertas Facturas Vencidas

**Archivo JSON:** `docs/n8n-flows/02-alertas-facturas-vencidas.json`
**Tags:** holded, alertas, facturas
**Estado:** Importado en n8n, credenciales asignadas

---

## ¿Qué hace?

Cada día a las 09:00 revisa si hay facturas con más de 7 días de vencimiento (status=4 en Supabase). Si las hay, genera un borrador de email en Gmail con la lista ordenada por antigüedad.

---

## Flujo

```
[Diario a las 09:00]
    ↓
[Facturas vencidas >7 días]   → Supabase: status=4, due_date no nulo
    ↓
[Filtrar >7 días]             → JS: filtra las que llevan >7 días vencidas, calcula days_overdue
    ↓
[¿Hay vencidas?]              → IF: count > 0
    ↓ (sí)              ↓ (no)
[Preparar HTML]           [fin]
    ↓
[Gmail — Crear Borrador]
```

---

## Nodos

| Nodo | Tipo | Descripción |
|------|------|-------------|
| Diario a las 09:00 | scheduleTrigger | Cron `0 9 * * *` |
| Facturas vencidas >7 días | supabase | getAll de `invoices` con `status=eq.4&due_date=not.is.null` |
| Filtrar >7 días | code (JS) | Filtra facturas con `days_overdue > 7`, agrega campo `days_overdue` |
| ¿Hay vencidas? | if | `$json.count > 0` |
| Preparar HTML | code (JS) | Genera tabla HTML ordenada por días vencida |
| Gmail — Crear Borrador | gmail | `create draft` → miguelbenajes@gmail.com |

---

## Lógica JS — Filtrar >7 días

```js
const today = new Date();
const overdue = items.filter(item => {
  const due = new Date(item.json.due_date);
  const diffDays = Math.floor((today - due) / (1000 * 60 * 60 * 24));
  return diffDays > 7;
}).map(item => ({
  json: { ...item.json, days_overdue: Math.floor((today - new Date(item.json.due_date)) / 86400000) }
}));
// Devuelve: [{ json: { count, invoices[] } }] o [] si no hay
```

---

## Email generado

- **Asunto:** `⚠️ N facturas vencidas — HoldedConnector`
- **Cuerpo:** Tabla HTML con cliente, nº factura, importe, fecha vencimiento, días vencida
- **Destino:** Borrador en miguelbenajes@gmail.com (no se envía automáticamente)

---

## Credenciales necesarias

- `supabaseApi` → credencial "Supabase account"
- `gmailOAuth2` → credencial "Gmail account" (OAuth2 con scope de borradores)

---

## Status codes de facturas

| Valor | Estado |
|-------|--------|
| 0 | Borrador |
| 1 | Emitida |
| 2 | Pago parcial |
| 3 | Pagada |
| 4 | Vencida |
| 5 | Cancelada |
