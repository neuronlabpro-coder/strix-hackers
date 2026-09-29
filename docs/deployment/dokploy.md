# Despliegue en Dokploy

## Compose y dominios

Usa `docker-compose.prod.yml` como Compose Path. En la pestaña **Environment** de Dokploy, pega el contenido de `.env.example` y sustituye los valores de desarrollo por los valores de producción descritos abajo. Dokploy guarda esas entradas como `.env`; Compose lo lee para interpolar variables y los servicios backend lo cargan mediante `env_file`.

No declares un servicio Traefik en este compose ni dupliques las etiquetas que Dokploy genera al configurar **Domains**. Añade los dominios desde esa pestaña y redepliega:

| Servicio | Dominio | Puerto del contenedor |
| --- | --- | ---:|
| `backend` | `api.mindguardredteam.com` | `8000` |
| `frontend` | `panel.mindguardredteam.com` | `80` |

El panel escucha en el puerto 80 dentro del contenedor, coincidiendo con el dominio frontend de Dokploy. El compose conecta solo API y panel a la red externa `dokploy-network`; el resto queda en la red privada del servicio.

## Valores de producción obligatorios

Empieza con todas las claves de `.env.example`, que contiene tanto los valores de configuración del backend como los límites del runner. Cambia como mínimo estos valores:

| Variable(s) | Valor requerido |
| --- | --- |
| `SECRET_KEY` | Secreto aleatorio propio, de al menos 32 caracteres. |
| `ENVIRONMENT`, `DEBUG`, `RUN_MIGRATIONS` | `production`, `false`, `false`. El compose fija estos valores; el esquema debe estar migrado antes de desplegar. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DATABASE_URL` | Datos de PostgreSQL 16 alcanzables desde el host Dokploy por Tailscale. Usa las credenciales de `FENIX_POSTGRES_*` del servidor de datos; `DATABASE_URL` debe coincidir exactamente con `DB_*`. |
| `REDIS_PASSWORD` | Debe ser igual a `FENIX_REDIS_PASSWORD` del servidor de datos. Si incluye caracteres reservados, codifícalos en `REDIS_URL`. |
| `GIT_ENCRYPTION_KEY` | Clave base64 que decodifique a 32 bytes. No sirve el valor de ejemplo del repositorio. |
| `DEFAULT_STRIX_LLM`, `LLM_API_KEY`, `LLM_API_BASE` | Modelo existente en la cuenta del proveedor, credencial y base HTTPS OpenAI-compatible. |
| `STRIX_LLM_KEY_EXPOSURE_ACK` | Para habilitar el lanzamiento de escaneos, escribe exactamente `la-clave-del-proveedor-entra-en-el-contenedor-aceptado`. La clave LLM se entrega al sandbox; considera primero un proxy de inferencia o credenciales limitadas por escaneo. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Credenciales activas del entorno Stripe correspondiente (`sk_live_…` y `whsec_…`). Son obligatorias para arrancar en producción. |
| `EMAIL_VERIFICATION_DELIVERY_MODE`, `FRONTEND_BASE_URL`, `API_PUBLIC_BASE_URL` | `smtp`, `https://panel.mindguardredteam.com`, `https://api.mindguardredteam.com`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `EMAIL_VERIFICATION_FROM` | SMTP accesible desde Dokploy; usuario y contraseña juntos, TLS `true` y remitente válido. |
| `VITE_API_URL` | `https://api.mindguardredteam.com`. Es argumento de build; cambia el bundle cuando cambie. |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `CELERY_REDIS_DB`, `REDIS_URL` | Dirección Tailscale del Redis externo (por ejemplo `100.89.59.70:6380`), bases 0 y 1. `REDIS_URL` debe coincidir con host, puerto, contraseña y base 0. |

Genera los secretos fuera del repositorio. Por ejemplo, para `GIT_ENCRYPTION_KEY`:

```powershell
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Si una contraseña de PostgreSQL contiene caracteres reservados de URL (`@`, `:`, `/`, `#` o `%`), codifícala en `DATABASE_URL` mientras mantienes el valor original en `DB_PASSWORD`.

## Variables de Compose opcionales

`.env.example` deja valores iniciales para `IMAGE_TAG`, `API_WORKERS`, `BACKEND_MEM_LIMIT`, `BACKEND_CPUS`, `BACKEND_PIDS_LIMIT`, `CELERY_LOGLEVEL` y `CELERY_MAX_TASKS_PER_CHILD`. Se pueden ajustar desde Dokploy. `DOCKER_GID` debe ser el GID del grupo dueño de `/var/run/docker.sock` en el host; consulta con `stat -c '%g' /var/run/docker.sock` y reemplaza el `999` de ejemplo.

Antes de desplegar, prepara en el host Dokploy la ruta temporal de los workspaces y dale acceso al UID/GID de la aplicación (`10001`):

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /tmp/fenix_workspaces
```

El worker usa el socket Docker del host para crear los contenedores sandbox. El `:ro` del montaje del socket no limita las operaciones de Docker; protege el filesystem del montaje, no las llamadas al daemon. Restringe acceso al servicio worker y configura el cerco de egreso de Strix en el host antes de permitir escaneos: `sudo bash scripts/harden_runner_egress.sh`, con persistencia de esas reglas tras reinicio. `STRIX_REQUIRE_EGRESS_FENCE=true` hace que los escaneos fallen cerrados si falta el cerco.

## Acceso a los datos

El host Dokploy y el servidor de datos deben estar en la misma tailnet. Usa en `DB_HOST` y `REDIS_HOST` la IP Tailscale del servidor de datos (la captura muestra `100.89.59.70`), con los puertos `5433` y `6380`. El compose de la aplicación conserva su bridge de salida `fenix` y conecta únicamente API y panel a `dokploy-network` para el proxy entrante. No intentes unir este stack a `fenix-network` del servidor de datos: las redes bridge son locales a cada host Docker, así que la conexión entre servidores debe ir por Tailscale.

Este compose no contiene servicio `migrate`, PostgreSQL ni Redis. `RUN_MIGRATIONS=false` impide que el entrypoint intente aplicar cambios de esquema durante el despliegue. API y workers conectan a las bases existentes por Tailscale.

Tras guardar Environment y configurar Domains, redepliega. Si falla el build del frontend, confirma que no aparece `setcap` y que el paso de runtime termina. Si frontend compila pero el dominio no responde, revisa que el dominio del panel tenga puerto `80`, que el de API tenga `8000` y que ambos contenedores estén conectados a `dokploy-network`.
