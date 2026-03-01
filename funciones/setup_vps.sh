#!/bin/bash
# setup_vps.sh — Instala el auto-generador de workflows en el VPS
#
# Ejecutar UNA SOLA VEZ en el VPS:
#   cd /opt/holded-connector
#   bash funciones/setup_vps.sh

set -e

REPO_DIR="/opt/holded-connector"
HOOK_FILE="$REPO_DIR/.git/hooks/post-merge"
CRON_LOG="/var/log/holded-git-pull.log"

echo "=== Instalando auto-generador de workflows n8n ==="

# 1. Git hook post-merge
echo "→ Instalando git hook post-merge..."
cat > "$HOOK_FILE" << 'HOOK_EOF'
#!/bin/bash
# post-merge hook: genera JSON n8n cuando se añaden nuevos .md en funciones/
REPO_DIR="/opt/holded-connector"

NEW_FILES=$(git diff-tree -r --name-only --no-commit-id ORIG_HEAD HEAD \
  | grep '^funciones/.*\.md$' \
  | grep -v 'howtoFunciones.md' || true)

if [ -z "$NEW_FILES" ]; then
    exit 0
fi

echo "[holded-gen] Nuevos .md detectados:"
for md_file in $NEW_FILES; do
    echo "  → $md_file"
    python3 "$REPO_DIR/$md_file" 2>&1 || true
done
# Nota: el script generate_workflow.py se invoca con su propia ruta
# pero el argumento es el path del .md. Corrección:
for md_file in $NEW_FILES; do
    python3 "$REPO_DIR/funciones/generate_workflow.py" "$REPO_DIR/$md_file" 2>&1 || true
done
HOOK_EOF

chmod +x "$HOOK_FILE"
echo "   ✓ Hook instalado en $HOOK_FILE"

# 2. Cron job para git pull automático cada 10 minutos
echo "→ Instalando cron job (git pull cada 10 min)..."
CRON_CMD="*/10 * * * * cd $REPO_DIR && git pull --ff-only >> $CRON_LOG 2>&1"

# Añadir solo si no existe ya
if crontab -l 2>/dev/null | grep -q "holded-connector.*git pull"; then
    echo "   (cron ya existe, no se duplica)"
else
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "   ✓ Cron instalado: $CRON_CMD"
fi

echo ""
echo "=== Setup completado ==="
echo ""
echo "Cómo usar:"
echo "  1. En tu Mac: escribe funciones/05-nombre.md"
echo "  2. git push"
echo "  3. El VPS hace git pull automáticamente (máx 10 min)"
echo "  4. El hook genera docs/n8n-flows/05-nombre.json"
echo "  5. Importa el JSON en https://n8n.coyoterent.com"
echo ""
echo "Para forzar git pull ahora:"
echo "  cd $REPO_DIR && git pull"
echo ""
echo "Logs del cron:"
echo "  tail -f $CRON_LOG"
