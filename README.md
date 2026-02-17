# Holded Dashboard Connector 📊🚀

Este proyecto es un conector y panel de control para visualizar tus datos de Holded (Facturas, Gastos, Presupuestos y Contactos) de forma local, rápida y con una interfaz premium.

## 🚀 Cómo ejecutarlo en cualquier máquina

La mejor manera de ejecutar esta aplicación de forma idéntica en cualquier sistema (Windows, Mac, Linux) es usando **Docker**.

### Opción 1: Docker (Recomendado)

Si tienes Docker instalado, solo necesitas ejecutar un comando:

1. Crea o asegúrate de tener el archivo `.env` con tu clave de API:
   ```env
   HOLDED_API_KEY=tu_clave_aqui
   HOLDED_SAFE_MODE=true
   ```
2. Ejecuta el comando:
   ```bash
   docker-compose up -d --build
   ```
3. Abre tu navegador en: `http://localhost:8000`

### Opción 2: Instalación Manual (Python)

Si prefieres ejecutarlo nativamente sin Docker:

1. **Instala las dependencias**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Inicia el servidor**:
   ```bash
   python api.py
   ```
3. Abre tu navegador en: `http://localhost:8000`

---

## 🛠️ Tecnologías utilizadas

- **Backend**: Python (FastAPI + SQLite)
- **Frontend**: Vanilla HTML / JS / CSS (Rich aesthetics & Micro-animations)
- **Integración**: API de Holded (Invoicing, Accounting, CRM)

---

## 🏛️ Características principales

- ✅ **Sincronización Inteligente**: Descarga facturas, gastos y presupuestos.
- ✅ **Mapeo Contable**: Resuelve IDs de cuentas a nombres reales (ej: Ventas de mercaderías).
- ✅ **Vista Detallada**: Desglose de productos con IVA e IRPF desglosado.
- ✅ **Previsualización de PDFs**: Visualiza y comparte tus facturas sin salir del dashboard.
- ✅ **Filtros Avanzados**: Búsqueda en tiempo real y filtrado por fechas.
- ✅ **UX Premium**: Modales con cierre inteligente (clic fuera) y diseño con desenfoques (glassmorphism).

Desarrollado con ❤️ para Miguel.
