# 01 — Sync Holded Programado

**Archivo JSON:** `docs/n8n-flows/01-sync-programado.json`
**Tags:** holded, sync
**Estado:** Importado en n8n, credenciales asignadas

---

## ¿Qué hace?

Ejecuta automáticamente cada 6 horas una sincronización completa de datos desde Holded API hacia Supabase, y registra el resultado en la tabla `sync_logs`.

---

## Flujo

```
[Cada 6 horas]
    ↓
[POST /api/sync]          → Sincroniza invoices, purchases, contacts, products desde Holded
    ↓
[GET /api/backup/status]  → Obtiene recuento de registros sincronizados
    ↓
[Registrar sync]          → Escribe fila en tabla sync_logs (Supabase)
```

---

## Nodos

| Nodo | Tipo | Descripción |
|------|------|-------------|
| Cada 6 horas | scheduleTrigger | Cron `0 */6 * * *` |
| POST /api/sync | httpRequest | `POST http://holded-api:8000/api/sync` (timeout 5 min) |
| GET /api/backup/status | httpRequest | `GET http://holded-api:8000/api/backup/status` |
| Registrar sync | supabase | INSERT en tabla `sync_logs` |

---

## Tabla sync_logs (Supabase)

| Campo | Valor |
|-------|-------|
| `executed_at` | `new Date().toISOString()` |
| `status` | `"ok"` |
| `invoices` | `$json.record_counts.invoices` |
| `purchases` | `$json.record_counts.purchase_invoices` |
| `contacts` | `$json.record_counts.contacts` |
| `products` | `$json.record_counts.products` |

---

## Credenciales necesarias

- `supabaseApi` → asignar credencial "Supabase account" en n8n

---

## Notas

- El timeout del POST /api/sync es de 300.000 ms (5 min) porque la sincronización puede tardar.
- El endpoint `/api/backup/status` devuelve `record_counts` con los totales actuales en BD.
- Si el sync falla, el nodo HTTP lanzará error y el workflow se detendrá (no registra en sync_logs).
