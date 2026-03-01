# Cómo crear una nueva función (workflow n8n)

> Instrucciones para humanos. Paso a paso, sin saltarse nada.

---

## Resumen rápido

1. Escribe `funciones/XX-nombre.md` describiendo lo que quiere hacer el workflow
2. `git push`
3. El VPS genera el JSON automáticamente (máx 10 min)
4. Importas el JSON en n8n y asignas credenciales

---

## Paso 1 — Primera vez en el VPS (solo una vez)

Conéctate al VPS y ejecuta el script de setup:

```bash
ssh ubuntu@158.179.215.185
cd /opt/holded-connector
git pull
bash funciones/setup_vps.sh
```

Esto instala:
- Un **git hook** que detecta nuevos `.md` y genera el JSON
- Un **cron job** que hace `git pull` cada 10 minutos

No hace falta repetirlo nunca más.

---

## Paso 2 — Crear el .md de la nueva función

En tu Mac, crea un archivo en `funciones/` con el número siguiente al último:

```
funciones/05-nombre-descriptivo.md
```

**Estructura recomendada del .md** (cuanto más detallado, mejor JSON genera):

```markdown
# 05 — Nombre de la función

## ¿Qué hace?
Descripción clara en 2-3 frases de qué automatiza este workflow.

## Trigger
Cuándo se ejecuta: cada X horas / diario a las HH:MM / webhook / manual.

## Flujo
Paso 1 → Paso 2 → Paso 3 (describe cada nodo)

## Datos que usa
- ¿Llama a algún endpoint de holded-api? ¿cuál?
- ¿Consulta Supabase? ¿qué tabla y filtro?
- ¿Envía email? ¿borrador o directo?

## Lógica especial
Si hay cálculos, filtros por fecha, o condiciones, descríbelos aquí.

## Credenciales necesarias
- Supabase / Gmail / ninguna
```

**Antes de escribir el .md**, lee `funciones/howtoFunciones.md` para evitar errores conocidos.

---

## Paso 3 — Push a git

```bash
git add funciones/05-nombre-descriptivo.md
git commit -m "feat: nueva función 05 - nombre descriptivo"
PATH="$HOME/bin:$PATH" git push
```

---

## Paso 4 — Esperar la generación (máx 10 min)

El VPS hace `git pull` cada 10 minutos. Cuando detecta el nuevo `.md`, llama a Claude API y genera el JSON.

Para no esperar, puedes conectarte al VPS y hacer el pull manualmente:

```bash
ssh ubuntu@158.179.215.185
cd /opt/holded-connector && git pull
```

El JSON aparecerá en `docs/n8n-flows/05-nombre-descriptivo.json`.

---

## Paso 5 — Importar en n8n

1. Abre https://n8n.coyoterent.com
2. Ve a **Workflows → Import from file**
3. Selecciona `docs/n8n-flows/05-nombre-descriptivo.json`
4. Asigna las credenciales (Supabase y/o Gmail) en cada nodo que lo pida
5. Haz clic en **Execute workflow** para probar manualmente
6. Si funciona, actívalo con el toggle

---

## Paso 6 — Si el JSON tiene errores

El JSON generado por IA puede necesitar ajustes. Revisa:

- ¿Las conexiones entre nodos son correctas?
- ¿Las expresiones `={{ $json.campo }}` son correctas?
- ¿El filtro de Supabase usa sintaxis PostgREST? (ver `howtoFunciones.md`)

Si hay errores recurrentes, añádelos a `funciones/howtoFunciones.md` para que la próxima generación los evite.

---

## Comandos útiles en el VPS

```bash
# Ver logs del cron de git pull
tail -f /var/log/holded-git-pull.log

# Generar JSON manualmente (sin esperar cron)
cd /opt/holded-connector
python3 funciones/generate_workflow.py funciones/05-nombre.md

# Ver estado de los contenedores Docker
docker compose ps

# Ver logs de n8n
docker compose logs n8n --tail=50
```

---

## Estructura de carpetas del proyecto

```
holded-connector/
├── INSTRUCCIONES/          ← Aquí. Guías para humanos.
├── funciones/              ← Un .md por workflow. Fuente de verdad.
│   ├── howtoFunciones.md   ← LEER ANTES de crear una función nueva
│   ├── generate_workflow.py ← Script que genera el JSON (corre en VPS)
│   ├── setup_vps.sh        ← Script de setup inicial (corre una vez en VPS)
│   ├── 01-sync-programado.md
│   └── ...
├── docs/n8n-flows/         ← JSONs generados para importar en n8n
├── api.py                  ← Servidor FastAPI (endpoints)
├── connector.py            ← Acceso a BD (SQLite/Supabase)
└── ai_agent.py             ← Agente IA con 19 herramientas
```
