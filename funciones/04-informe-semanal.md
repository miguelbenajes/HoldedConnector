# 04 — Informe Semanal

**Archivo JSON:** `docs/n8n-flows/04-informe-semanal.json`
**Tags:** holded, informe, semanal
**Estado:** Importado en n8n, credenciales asignadas

---

## ¿Qué hace?

Cada lunes a las 08:00 obtiene en paralelo el resumen financiero, las estadísticas mensuales y las facturas pendientes de cobro más próximas. Combina todo en un email HTML con informe visual y lo guarda como borrador en Gmail.

---

## Flujo

```
[Lunes a las 08:00]
    ↓ (3 ramas en paralelo)
[GET /api/summary]          [GET /api/stats/monthly]     [Facturas pendientes cobro]
        ↓                           ↓                              ↓
        └───────────── [Unir datos] ───────────────────────────────┘
                              ↓
                    [Componer HTML informe]
                              ↓
                    [Gmail — Crear Borrador]
```

---

## Nodos

| Nodo | Tipo | Descripción |
|------|------|-------------|
| Lunes a las 08:00 | scheduleTrigger | Cron `0 8 * * 1` |
| GET /api/summary | httpRequest | Resumen financiero acumulado (ingresos, gastos, balance) |
| GET /api/stats/monthly | httpRequest | Estadísticas por mes (últimos meses) |
| Facturas pendientes cobro | supabase | Top 5 facturas status IN (1,2) ordenadas por due_date ASC |
| Unir datos | merge (combine) | Combina los 3 inputs (índices 0, 1, 2) |
| Componer HTML informe | code (JS) | Genera HTML completo del informe |
| Gmail — Crear Borrador | gmail | `create draft` → miguelbenajes@gmail.com |

---

## Secciones del informe HTML

1. **Resumen financiero acumulado** — 3 tarjetas: ingresos (verde), gastos (rojo), balance (verde/rojo según signo)
2. **Últimos 4 meses** — Tabla con ingresos, gastos y balance mensual
3. **Próximas facturas pendientes de cobro** — Top 5 por fecha de vencimiento más próxima

---

## APIs utilizadas

| Endpoint | Devuelve |
|----------|----------|
| `GET /api/summary` | `{ total_income, total_expenses, balance }` |
| `GET /api/stats/monthly` | Array de `{ month, income, expenses }` |
| Supabase invoices | Facturas con `status IN (1,2)`, ordenadas `due_date ASC`, límite 5 |

---

## Email generado

- **Asunto:** `📊 Informe semanal HoldedConnector — [fecha lunes]`
- **Cuerpo:** HTML responsivo con tablas y tarjetas de colores
- **Destino:** Borrador en miguelbenajes@gmail.com

---

## Credenciales necesarias

- `supabaseApi` → credencial "Supabase account"
- `gmailOAuth2` → credencial "Gmail account"

---

## Notas sobre el nodo Merge

El trigger dispara 3 ramas en paralelo. El merge tipo `combine` espera los 3 inputs:
- Input 0 ← GET /api/summary
- Input 1 ← GET /api/stats/monthly
- Input 2 ← Facturas pendientes cobro

El código JS en "Componer HTML informe" identifica cada tipo de dato por sus campos:
- Summary → tiene `total_income`
- Monthly → tienen `month`
- Pending → tienen `doc_number` y `status` IN (1,2)
