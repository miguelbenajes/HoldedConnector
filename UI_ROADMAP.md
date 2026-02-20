# UI Roadmap — HoldedConnector

Estado: 🔴 Pendiente · 🟡 En progreso · 🟢 Completado

---

## Feature 1 — Limpieza de columnas por defecto 🔴
**Descripción:** Los cabeceros de las tablas no deben mostrar IDs internos (contact_id, invoice_id, etc.) por defecto. Son identificadores técnicos sin valor para un humano.
**Alcance:** Todas las tablas de entidades (invoices, contacts, products, estimates, purchases)
**Comportamiento:**
- Ocultar por defecto: `id`, `contact_id`, `invoice_id`, `product_id` y cualquier campo que termine en `_id`
- Siempre visibles por defecto: nombre, fecha, importe, estado

---

## Feature 2 — Configurador de columnas con botón derecho 🔴
**Descripción:** Al hacer clic derecho sobre cualquier cabecero de tabla, aparece un menú contextual con todos los campos disponibles marcados con checkboxes. Permite elegir exactamente qué columnas mostrar.
**Alcance:** Todas las tablas de entidades
**Comportamiento:**
- Click derecho en cabecero → menú contextual con checkboxes
- Reordenación de columnas arrastrando el cabecero
- Guardar configuración por vista en `localStorage` (clave: `col_config_<viewName>`)
- Botón "Restablecer por defecto" en el menú

---

## Feature 3 — Selector de fechas tipo Holded 🔴
**Descripción:** Reemplazar cualquier selector de fechas por un componente idéntico al de Holded: dropdown de texto con presets + calendarios inline para rango personalizado.
**Presets disponibles:**
- Trimestre actual *(por defecto)*
- Año actual
- Año anterior
- Última semana
- Últimos 7 días
- Mes actual
- Mes anterior
- Personalizado → abre dos calendarios (desde / hasta) con selección de mes, día, año
**Alcance:** Vista Overview, Invoices, Purchases, Estimates, Análisis Gastos
**Notas:** El selector personalizado muestra dos calendarios en paralelo, con navegación por mes y año. Selección de rango con highlight entre las dos fechas.

---

## Backlog (sin prioridad aún)

- [ ] Dark/light theme toggle
- [ ] Búsqueda y filtrado en el chat
- [ ] Notificaciones en tiempo real para facturas vencidas
- [ ] Informes programados por email (resumen semanal)
- [ ] Soporte multiidioma (actualmente español/inglés mezclado)
- [ ] Autenticación de usuario y roles
- [ ] Integración webhook de Holded para sync en vivo

---

*Última actualización: 2026-02-20*
