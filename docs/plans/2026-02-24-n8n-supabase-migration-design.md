# Diseño: Migración a Supabase + Automatizaciones con n8n

**Fecha:** 2026-02-24
**Estado:** Aprobado por el usuario
**Repo:** https://github.com/miguelbenajes/HoldedConnector

---

## Contexto y Motivación

HoldedConnector actualmente corre en local (Mac) con SQLite como base de datos. Esto impide:
- Acceso desde otros dispositivos / fuera de la red local
- Automatizaciones programadas sin depender de que el Mac esté encendido
- Escalabilidad futura (múltiples usuarios, acceso cloud)

La solución es migrar la DB a **Supabase (PostgreSQL cloud)**, desplegar **FastAPI en Oracle Cloud VPS** (free tier disponible), y añadir **n8n** como orquestador de flujos automáticos de negocio.

El chat AI con Claude (panel flotante) se mantiene sin cambios.

---

## Arquitectura Final

```
┌─────────────────────────────────────────────────────────┐
│               ORACLE CLOUD VPS (free tier)              │
│                                                         │
│  ┌──────────────────────┐   ┌────────────────────────┐  │
│  │   Docker: FastAPI    │   │    Docker: n8n         │  │
│  │   (HoldedConnector)  │   │    (automatizaciones)  │  │
│  │   port 8000          │   │    port 5678           │  │
│  └──────────┬───────────┘   └───────────┬────────────┘  │
│             │ psycopg2                  │ nativo         │
└─────────────┼───────────────────────────┼───────────────┘
              │                           │
              └─────────────┬─────────────┘
                            │
                   ┌────────▼────────┐
                   │   SUPABASE      │
                   │  (PostgreSQL)   │
                   │  cloud gratuito │
                   └─────────────────┘
                            │
                   ┌────────▼────────┐
                   │  Dashboard UI   │
                   │  (mismo JS)     │
                   │  HTTPS via VPS  │
                   └─────────────────┘
```

**Flujos de datos:**
- `Holded API → FastAPI (sync) → Supabase` (igual que ahora, pero en la nube)
- `n8n → FastAPI endpoints` (sync programado, informes)
- `n8n → Supabase directo` (queries para alertas y recordatorios)
- `n8n → Email/Notificaciones` (SMTP o servicio externo)

---

## Fase 1: Migración SQLite → Supabase

### Cambios en Python

**Archivo principal: `connector.py`**

Reemplazar la función de conexión a DB:

```python
# ANTES
import sqlite3
conn = sqlite3.connect("holded.db")

# DESPUÉS
import psycopg2
import psycopg2.extras
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://user:pass@host:5432/db

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn
```

**Compatibilidad de queries:**
- SQLite `?` placeholders → PostgreSQL `%s` (buscar/reemplazar)
- `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (id) DO UPDATE SET ...`
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`
- `REAL` → `NUMERIC`
- `conn.row_factory = sqlite3.Row` → `cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)`

**Archivos afectados:**
- `connector.py` — todas las funciones `get_db()`, `init_db()`, `sync_*()`, CRUD
- `api.py` — endpoints que usan `sqlite3.connect()` directamente (PDF filename helper)
- `ai_agent.py` — `query_database` tool executor

### Schema en Supabase

Supabase crea las tablas vía el mismo `init_db()` adaptado a PostgreSQL. Las tablas son idénticas:

```
invoices, purchase_invoices, estimates
invoice_items, purchase_items, estimate_items
contacts, products, payments, projects, ledger_accounts
ai_history, ai_favorites, settings
purchase_analysis, inventory_matches
amortizations, amortization_purchases, product_type_rules
```

### Variables de entorno

```bash
# .env (producción en VPS)
DATABASE_URL=postgresql://postgres:[password]@db.[project].supabase.co:5432/postgres
HOLDED_API_KEY=sk_...
ANTHROPIC_API_KEY=sk-ant-...
HOLDED_SAFE_MODE=false
ALLOWED_ORIGINS=https://tudominio.com
```

---

## Fase 2: Despliegue en Oracle Cloud VPS

### docker-compose.yml (nuevo archivo)

```yaml
version: "3.9"
services:
  holded-api:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    restart: unless-stopped

  n8n:
    image: n8nio/n8n:latest
    ports:
      - "5678:5678"
    environment:
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=admin
      - N8N_BASIC_AUTH_PASSWORD=${N8N_PASSWORD}
      - WEBHOOK_URL=https://tudominio.com/n8n/
    volumes:
      - n8n_data:/home/node/.n8n
    restart: unless-stopped

volumes:
  n8n_data:
```

### Dockerfile (FastAPI)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
```

### requirements.txt — añadir

```
psycopg2-binary>=2.9
uvicorn>=0.24
```

### Nginx (reverse proxy)

```nginx
server {
    listen 443 ssl;
    server_name tudominio.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /n8n/ {
        proxy_pass http://localhost:5678/;
    }
}
```

---

## Fase 3: Flujos de n8n

### Flujo 1 — Sync programado de Holded

**Trigger:** Cron → cada 6 horas (06:00, 12:00, 18:00, 00:00)
**Nodos:**
1. `Schedule Trigger` — `0 */6 * * *`
2. `HTTP Request` — `POST https://tudominio.com/api/sync`
3. `HTTP Request` — `GET https://tudominio.com/api/sync/status` (poll hasta completado)
4. `Supabase` — INSERT en `sync_logs` (timestamp, duration, status, counts)

**Sin tocar código Python** — usa endpoints existentes.

---

### Flujo 2 — Alertas de facturas vencidas

**Trigger:** Cron → diario a las 09:00
**Nodos:**
1. `Schedule Trigger` — `0 9 * * *`
2. `Supabase` — query directa:
   ```sql
   SELECT contact_name, doc_number, amount, due_date,
          CURRENT_DATE - due_date::date AS days_overdue
   FROM invoices
   WHERE status = 4
     AND due_date IS NOT NULL
     AND CURRENT_DATE - due_date::date > 7
   ORDER BY days_overdue DESC
   ```
3. `IF` — si hay resultados
4. `Send Email` (o `Slack`) — asunto: "⚠️ X facturas vencidas" con tabla HTML

**Resultado:** Email a tu bandeja cuando haya facturas realmente problemáticas.

---

### Flujo 3 — Recordatorio de cobro a clientes

**Trigger:** Cron → diario a las 08:30
**Nodos:**
1. `Schedule Trigger` — `30 8 * * *`
2. `Supabase` — query:
   ```sql
   SELECT i.id, i.contact_name, i.amount, i.doc_number,
          i.due_date, c.email
   FROM invoices i
   JOIN contacts c ON c.id = i.contact_id
   WHERE i.status IN (1, 2)
     AND c.email IS NOT NULL AND c.email != ''
     AND (
       i.due_date::date = CURRENT_DATE + 5   -- recordatorio previo
       OR i.due_date::date = CURRENT_DATE    -- día de vencimiento
       OR i.due_date::date = CURRENT_DATE - 3 -- 3 días después
     )
   ```
3. `Loop over items` — para cada factura
4. `Send Email` — email personalizado al cliente con:
   - Nombre del cliente
   - Número y descripción de la factura
   - Importe pendiente
   - Fecha de vencimiento
   - Enlace de pago (si existe)

**Opcional:** añadir campo `reminder_sent_at` en Supabase para no reenviar.

---

### Flujo 4 — Informe semanal

**Trigger:** Cron → lunes a las 08:00
**Nodos:**
1. `Schedule Trigger` — `0 8 * * 1`
2. `HTTP Request` — `GET https://tudominio.com/api/summary`
3. `HTTP Request` — `GET https://tudominio.com/api/stats/monthly`
4. `HTTP Request` — `GET https://tudominio.com/api/stats/top-contacts`
5. `Code` — formatear HTML del informe con los datos
6. `Send Email` — asunto: "📊 Informe semanal HoldedConnector"

**Contenido del email:**
- Balance total (ingresos - gastos)
- Comparativa vs semana anterior
- Top 3 clientes de la semana
- Facturas pendientes de cobro

---

## Ficheros a crear/modificar

| Archivo | Acción | Descripción |
|---------|--------|-------------|
| `connector.py` | MODIFY | Cambiar driver SQLite → psycopg2, adaptar placeholders y `INSERT OR REPLACE` |
| `api.py` | MODIFY | Cambiar helper PDF (usa sqlite3 directo), añadir endpoint `/api/sync/status` |
| `ai_agent.py` | MODIFY | Adaptar `query_database` tool (row_factory → RealDictCursor) |
| `requirements.txt` | MODIFY | Añadir psycopg2-binary, uvicorn |
| `Dockerfile` | CREATE | Imagen Python para FastAPI |
| `docker-compose.yml` | CREATE | Orquesta FastAPI + n8n |
| `.env.example` | MODIFY | Añadir DATABASE_URL |
| `docs/n8n-flows/` | CREATE | JSON exports de los 4 flujos de n8n |

**No cambia:**
- `static/` (HTML, CSS, JS) — idéntico
- Schema de tablas — idéntico
- Lógica de negocio — idéntica
- AI agent tools — idénticos (solo el cursor)

---

## Verificación

### Fase 1 (Supabase)
1. Crear proyecto en [supabase.com](https://supabase.com) (gratuito)
2. Copiar `DATABASE_URL` → `.env`
3. Arrancar servidor: `python api.py` → `init_db()` crea todas las tablas en Supabase
4. `POST /api/sync` → datos aparecen en Supabase Table Editor
5. Dashboard carga datos normalmente

### Fase 2 (Docker en Oracle Cloud)
1. `docker-compose up -d` en el VPS
2. Acceder a `https://tudominio.com` → dashboard funciona
3. Acceder a `https://tudominio.com/n8n/` → interfaz n8n

### Fase 3 (Flujos n8n)
1. Importar JSON de cada flujo en n8n
2. Configurar credenciales (Supabase URL+Key, SMTP)
3. Activar flujos
4. Testear manualmente cada nodo
5. Verificar: sync automático popula Supabase, email de alerta llega

---

## Estimación de coste

| Servicio | Coste |
|----------|-------|
| Oracle Cloud VPS | **Gratuito** (Always Free tier: 1 OCPU, 1GB RAM) |
| Supabase | **Gratuito** (hasta 500MB DB, 2GB bandwidth) |
| n8n (self-hosted en VPS) | **Gratuito** |
| Dominio (opcional) | ~€10/año |
| **Total** | **€0–10/año** |

---

## Próximos pasos sugeridos

1. Crear proyecto Supabase y obtener `DATABASE_URL`
2. Migrar `connector.py` a psycopg2 (el cambio más grande)
3. Testear localmente con Supabase antes de mover a VPS
4. Crear `Dockerfile` + `docker-compose.yml`
5. Desplegar en Oracle Cloud
6. Configurar n8n y crear los 4 flujos

---

*Diseño elaborado en sesión 2026-02-24. Aprobado por el usuario.*
