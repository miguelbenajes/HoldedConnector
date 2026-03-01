# HoldedConnector

Dashboard financiero que sincroniza datos de Holded API a PostgreSQL (Supabase) con asistente virtual IA integrado.

## Stack

- **Backend:** FastAPI + Python 3.9+
- **Base de datos:** PostgreSQL (Supabase) / SQLite (desarrollo local)
- **IA:** Claude API (tool_use) con 19 herramientas y respuestas en streaming
- **Frontend:** Vanilla JS + Chart.js + PWA instalable
- **Integraciones:** Holded API (facturación, CRM, contabilidad)

## Funcionalidades

- **Sync de datos:** Facturas, gastos, presupuestos, contactos, productos, pagos, proyectos
- **Dashboard:** Vista general con ingresos/gastos/balance, tendencias mensuales, top clientes
- **Asistente IA:** Chat con Claude que consulta y analiza datos financieros en tiempo real
  - Consultas SQL seguras (solo SELECT)
  - Creacion de facturas y presupuestos con confirmacion
  - Graficos inline (Chart.js)
  - Historial de conversaciones y favoritos
- **Amortizaciones:** Tracking de ROI para productos alquilados
- **Analisis de compras:** Categorizacion automatica con IA
- **PWA:** Instalable en movil y escritorio
- **Tema claro/oscuro**

## Setup rapido

```bash
git clone https://github.com/miguelbenajes/HoldedConnector.git
cd HoldedConnector
pip install -r requirements.txt
cp .env.example .env
# Editar .env con tus claves
python3 api.py
```

Abrir `http://localhost:8000`

## Configuracion (.env)

```bash
HOLDED_API_KEY=tu_clave_holded
ANTHROPIC_API_KEY=sk-ant-...       # Opcional, se puede configurar desde la UI
HOLDED_SAFE_MODE=true              # true = modo prueba para operaciones de escritura

# PostgreSQL (Supabase) — dejar vacio para SQLite local
DATABASE_URL=postgresql://postgres.[ref]:[pass]@pooler.supabase.com:5432/postgres
```

## Arquitectura

```
Holded API  --->  connector.py  --->  PostgreSQL (Supabase)
                      |                      |
                  api.py (FastAPI)  <---------+
                  /    |     \
            static/  ai_agent.py  reports.py
            (SPA)    (Claude)     (PDF/Excel)
```

- `connector.py` — Capa de datos: sync, queries, helpers DB (dual SQLite/PostgreSQL)
- `api.py` — Servidor HTTP, todos los endpoints REST
- `ai_agent.py` — Agente Claude con 19 herramientas y streaming SSE
- `reports.py` — Generacion de informes PDF y Excel
- `static/` — Frontend SPA (HTML + JS + CSS)

## Docker

```bash
docker-compose up -d --build
```

Servicios: `holded-api` (puerto 8000) + `n8n` (puerto 5678, automatizaciones)

## Licencia

Proyecto privado.
