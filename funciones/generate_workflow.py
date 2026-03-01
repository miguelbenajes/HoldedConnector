#!/usr/bin/env python3
"""
generate_workflow.py — Genera un JSON de n8n a partir de un .md en funciones/

Uso:
    python3 funciones/generate_workflow.py funciones/05-nueva-funcion.md

Requiere:
    - ANTHROPIC_API_KEY en .env
    - paquete 'anthropic' instalado (ya está en requirements.txt)
"""

import sys
import os
import json
import re

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env(env_path=".env"):
    """Lee variables del .env sin depender de python-dotenv."""
    env = {}
    if not os.path.exists(env_path):
        return env
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_json_block(text):
    """Extrae el primer bloque ```json ... ``` de la respuesta."""
    match = re.search(r"```json\s*([\s\S]+?)```", text)
    if match:
        return match.group(1).strip()
    # Fallback: intentar parsear el texto completo
    return text.strip()


def validate_workflow(data):
    """Valida que el JSON tenga la estructura mínima de un workflow n8n."""
    required = ["name", "nodes", "connections", "active", "settings"]
    for key in required:
        if key not in data:
            raise ValueError(f"JSON inválido: falta campo '{key}'")
    if not isinstance(data["nodes"], list) or len(data["nodes"]) == 0:
        raise ValueError("JSON inválido: 'nodes' debe ser un array no vacío")
    if data.get("active") is not False:
        data["active"] = False  # Siempre false por seguridad


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Eres un experto en n8n (herramienta de automatización de workflows).
Tu tarea es generar un archivo JSON válido e importable en n8n a partir de una descripción en Markdown.

REGLAS OBLIGATORIAS:
- Responde SOLO con el bloque JSON, nada más. Formato: ```json ... ```
- El JSON debe ser válido y parseable
- Usa siempre "active": false
- Usa siempre "settings": {"executionOrder": "v1"}
- Usa siempre "meta": {"instanceId": ""}
- Cada nodo debe tener un "id" UUID v4 único
- Las conexiones van por nombre de nodo (no por id)
- Credenciales Supabase: id "SUPABASE_CREDENTIAL_ID", name "Supabase account"
- Credenciales Gmail: id "GMAIL_CREDENTIAL_ID", name "Gmail account"
- El servidor interno de la API es: http://holded-api:8000
- Los borradores de Gmail siempre van a: miguelbenajes@gmail.com
- NUNCA uses "send" en Gmail, siempre "create draft"
"""


def build_user_prompt(md_content, howto_content, example_json):
    return f"""# Descripción del workflow a generar

{md_content}

---

# Bugs y errores a evitar (lee esto ANTES de generar)

{howto_content}

---

# Ejemplo de estructura JSON correcta de referencia

```json
{example_json}
```

---

Genera ahora el JSON completo del workflow descrito. Solo el JSON, nada más.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Argumentos
    if len(sys.argv) < 2:
        print("Uso: python3 funciones/generate_workflow.py funciones/<nombre>.md")
        sys.exit(1)

    md_path = sys.argv[1]

    # Saltar howtoFunciones.md
    if os.path.basename(md_path) == "howtoFunciones.md":
        print("Skipping howtoFunciones.md")
        sys.exit(0)

    if not md_path.endswith(".md"):
        print(f"Error: el archivo debe ser .md, recibido: {md_path}")
        sys.exit(1)

    if not os.path.exists(md_path):
        print(f"Error: no existe {md_path}")
        sys.exit(1)

    # Ruta de salida
    base_name = os.path.splitext(os.path.basename(md_path))[0]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(md_path)))
    output_path = os.path.join(repo_root, "docs", "n8n-flows", f"{base_name}.json")

    # No sobreescribir si ya existe
    if os.path.exists(output_path):
        print(f"JSON ya existe, no se sobreescribe: {output_path}")
        sys.exit(0)

    # API key
    env = load_env(os.path.join(repo_root, ".env"))
    api_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY no encontrado en .env ni en variables de entorno")
        sys.exit(1)

    # Leer contexto
    howto_path = os.path.join(repo_root, "funciones", "howtoFunciones.md")
    howto_content = read_file(howto_path) if os.path.exists(howto_path) else "(sin howto disponible)"

    # Leer un JSON de ejemplo (el más sencillo: 01)
    example_path = os.path.join(repo_root, "docs", "n8n-flows", "01-sync-programado.json")
    if os.path.exists(example_path):
        example_json = read_file(example_path)
    else:
        # Fallback: buscar cualquier JSON en n8n-flows
        flows_dir = os.path.join(repo_root, "docs", "n8n-flows")
        jsons = [f for f in os.listdir(flows_dir) if f.endswith(".json")]
        example_json = read_file(os.path.join(flows_dir, jsons[0])) if jsons else "{}"

    md_content = read_file(md_path)

    # Llamar a Claude API
    try:
        import anthropic
    except ImportError:
        print("Error: paquete 'anthropic' no instalado. Ejecuta: pip install anthropic")
        sys.exit(1)

    print(f"Generando workflow para: {md_path}")
    print("Llamando a Claude API...")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": build_user_prompt(md_content, howto_content, example_json)
            }
        ]
    )

    raw = response.content[0].text
    json_str = extract_json_block(raw)

    # Parsear y validar
    try:
        data = json.loads(json_str)
        validate_workflow(data)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error validando JSON generado: {e}")
        print("--- Respuesta raw ---")
        print(raw[:2000])
        sys.exit(1)

    # Guardar
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✓ JSON generado: {output_path}")
    print(f"  Nodos: {len(data['nodes'])}")
    print(f"  Nombre: {data['name']}")
    print("  → Importa el JSON en n8n UI y asigna las credenciales.")


if __name__ == "__main__":
    main()
