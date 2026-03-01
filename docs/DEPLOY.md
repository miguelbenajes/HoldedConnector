# Guía de despliegue — Oracle Cloud VPS

Despliegue de HoldedConnector (FastAPI + n8n) en Oracle Cloud Free Tier con Supabase como base de datos.

## Arquitectura

```
Internet → Nginx (HTTPS) → holded-api (puerto 8000)
                         → n8n         (puerto 5678)
                              ↓
                         Supabase (PostgreSQL cloud)
```

Comunicación interna: n8n llama a `http://holded-api:8000` (red Docker interna, sin pasar por Internet).

---

## Requisitos previos

- Oracle Cloud VM (siempre gratuita: 1 OCPU AMD, 1 GB RAM, Ubuntu 22.04)
- Supabase proyecto activo con `DATABASE_URL`
- Dominio apuntando a la IP del VPS (para HTTPS)
- DNS A record: `tudominio.com → IP_VPS`

---

## 1. Preparar el VPS

```bash
# Conectar por SSH
ssh ubuntu@IP_VPS

# Actualizar sistema
sudo apt update && sudo apt upgrade -y

# Instalar Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
newgrp docker

# Instalar Docker Compose v2
sudo apt install -y docker-compose-plugin
docker compose version  # debe mostrar v2.x

# Instalar Nginx + Certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# Instalar git
sudo apt install -y git curl
```

---

## 2. Clonar el repositorio

```bash
cd /opt
sudo git clone https://github.com/miguelbenajes/HoldedConnector.git holded-connector
sudo chown -R ubuntu:ubuntu holded-connector
cd holded-connector
```

---

## 3. Configurar variables de entorno

```bash
cp .env.example .env
nano .env
```

Rellenar todos los valores:

```bash
# Holded API
HOLDED_API_KEY=sk_tu_clave_holded

# Anthropic Claude
ANTHROPIC_API_KEY=sk-ant-...

# Supabase — Session Pooler (puerto 5432)
DATABASE_URL=postgresql://postgres.REFID:PASSWORD@aws-1-eu-west-1.pooler.supabase.com:5432/postgres

# App
HOLDED_SAFE_MODE=false
ALLOWED_ORIGINS=https://tudominio.com

# n8n
N8N_PASSWORD=elige_password_seguro
WEBHOOK_URL=https://tudominio.com/n8n/
```

---

## 4. Levantar servicios con Docker

```bash
# Build y arrancar en background
docker compose up -d --build

# Verificar que ambos contenedores están Up
docker compose ps

# Ver logs en tiempo real
docker compose logs -f holded-api
docker compose logs -f n8n
```

Comprobación rápida:
```bash
curl http://localhost:8000/api/status
# Debe devolver JSON con counts de tablas
```

---

## 5. Configurar Nginx (reverse proxy)

```bash
sudo nano /etc/nginx/sites-available/holded
```

Pegar esta configuración (sustituir `tudominio.com`):

```nginx
server {
    listen 80;
    server_name tudominio.com;

    # Dashboard principal
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE streaming (chat IA)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }

    # n8n
    location /n8n/ {
        proxy_pass http://localhost:5678/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

```bash
# Activar el sitio
sudo ln -s /etc/nginx/sites-available/holded /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Obtener certificado SSL (HTTPS)
sudo certbot --nginx -d tudominio.com

# Verificar renovación automática
sudo certbot renew --dry-run
```

---

## 6. Importar flujos de n8n

1. Acceder a `https://tudominio.com/n8n/`
2. Login con usuario `admin` y la password de `.env`
3. Ir a **Settings → Credentials** y crear:
   - **Supabase** → URL del proyecto + API Key (la `anon` o `service_role`)
   - **Gmail OAuth2** → seguir el wizard de Google OAuth
4. Ir a **Workflows → Import** y subir los 4 JSON de `docs/n8n-flows/`:
   - `01-sync-programado.json`
   - `02-alertas-facturas-vencidas.json`
   - `03-recordatorio-cobro-clientes.json`
   - `04-informe-semanal.json`
5. En cada workflow importado, abrir los nodos Supabase y Gmail y **asignar las credenciales** creadas en el paso anterior
6. Activar los flujos con el toggle (empieza solo por el 01 para verificar)

---

## 7. Primer sync de datos

```bash
# Lanzar sync manual desde CLI
curl -X POST https://tudominio.com/api/sync

# O desde el dashboard → botón Sync
```

---

## Mantenimiento

### Actualizar la app

```bash
cd /opt/holded-connector
git pull
docker compose up -d --build holded-api
```

### Ver logs

```bash
docker compose logs -f holded-api    # API server
docker compose logs -f n8n           # n8n
sudo journalctl -u nginx -f          # Nginx
```

### Reiniciar servicios

```bash
docker compose restart holded-api
docker compose restart n8n
```

### Backup de n8n (workflows y credenciales)

Los datos de n8n se guardan en el volumen Docker `holded-connector_n8n_data`.

```bash
docker run --rm \
  -v holded-connector_n8n_data:/source \
  -v $(pwd)/BACKUPS:/dest \
  alpine tar czf /dest/n8n_backup_$(date +%Y%m%d).tar.gz -C /source .
```

---

## Troubleshooting

| Problema | Solución |
|----------|----------|
| `docker compose up` falla por memoria | Oracle Free tier tiene 1GB — cerrar procesos innecesarios |
| n8n no arranca | Verificar `N8N_PASSWORD` en `.env`, revisar `docker compose logs n8n` |
| Nginx 502 Bad Gateway | Verificar que el contenedor está Up: `docker compose ps` |
| HTTPS no funciona | Verificar DNS propagado: `dig tudominio.com`, luego `sudo certbot --nginx` |
| Sync falla en producción | Verificar `DATABASE_URL` y `HOLDED_API_KEY` en `.env` |
| Workflows n8n sin datos | Abrir cada nodo y reasignar credenciales Supabase/Gmail |
| SSE streaming se corta | Verificar `proxy_buffering off` en Nginx config |

---

## Costes estimados

| Servicio | Plan | Coste |
|----------|------|-------|
| Oracle Cloud VPS | Always Free (AMD OCPU + 1GB RAM) | €0/mes |
| Supabase | Free (500MB DB, 2GB bandwidth) | €0/mes |
| n8n (self-hosted) | En el mismo VPS | €0/mes |
| Certbot SSL | Let's Encrypt | €0/mes |
| Dominio (opcional) | .com estándar | ~€1/mes |

**Total: €0–1/mes**
