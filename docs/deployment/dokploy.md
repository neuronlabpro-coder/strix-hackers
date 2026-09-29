# Despliegue en Dokploy

## Compose y dominios

Usa `docker-compose.prod.yml` como Compose Path. En la pestaña **Environment** de Dokploy, pega el contenido de `.env.example` y sustituye los valores de desarrollo por los valores de producción descritos abajo. Dokploy guarda esas entradas como `.env`; Compose lo lee para interpolar variables y los servicios backend lo cargan mediante `env_file`.

No declares un servicio Traefik en este compose ni dupliques las etiquetas que Dokploy genera al configurar **Domains**. Añade los dominios desde esa pestaña y redepliega:

| Servicio | Dominio | Puerto del contenedor |
| --- | --- | ---:|
| `backend` | `api.mindguardredteam.com` | `8000` |
| `frontend` | `panel.mindguardredteam.com` | `8080` |

El panel escucha en 8080 dentro del contenedor. Si Dokploy lo enruta al puerto 80 (como en una configuración anterior), Traefik intentará conectar al puerto equivocado. El compose conecta solo API y panel a la red externa `dokploy-network`; el resto queda en la red privada del servicio.

## Valores de producción obligatorios

Empieza con todas las claves de `.env.example`, que contiene tanto los valores de configuración del backend como los límites del runner. Cambia como mínimo estos valores:

| Variable(s) | Valor requerido |
| --- | --- |
| `SECRET_KEY` | Secreto aleatorio propio, de al menos 32 caracteres. |
| `ENVIRONMENT`, `DEBUG` | `production`, `false`. El compose fija estos valores. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DATABASE_URL` | Datos de PostgreSQL 16 alcanzables desde el host Dokploy por Tailscale. `DATABASE_URL` debe coincidir exactamente con `DB_*`; usa el formato `postgresql+asyncpg://usuario:contraseña@host:5433/base`. |
| `REDIS_PASSWORD` | Secreto de Redis; no uses `@` ni `:` porque Compose lo inserta en `REDIS_URL`. |
| `GIT_ENCRYPTION_KEY` | Clave base64 que decodifique a 32 bytes. No sirve el valor de ejemplo del repositorio. |
| `DEFAULT_STRIX_LLM`, `LLM_API_KEY`, `LLM_API_BASE` | Modelo existente en la cuenta del proveedor, credencial y base HTTPS OpenAI-compatible. |
| `STRIX_LLM_KEY_EXPOSURE_ACK` | Para habilitar el lanzamiento de escaneos, escribe exactamente `la-clave-del-proveedor-entra-en-el-contenedor-aceptado`. La clave LLM se entrega al sandbox; considera primero un proxy de inferencia o credenciales limitadas por escaneo. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Credenciales activas del entorno Stripe correspondiente (`sk_live_…` y `whsec_…`). Son obligatorias para arrancar en producción. |
| `EMAIL_VERIFICATION_DELIVERY_MODE`, `FRONTEND_BASE_URL`, `API_PUBLIC_BASE_URL` | `smtp`, `https://panel.mindguardredteam.com`, `https://api.mindguardredteam.com`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `EMAIL_VERIFICATION_FROM` | SMTP accesible desde Dokploy; usuario y contraseña juntos, TLS `true` y remitente válido. |
| `VITE_API_URL` | `https://api.mindguardredteam.com`. Es argumento de build; cambia el bundle cuando cambie. |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `CELERY_REDIS_DB` | El compose deriva la conexión interna a `redis:6379`, bases 0 y 1. La contraseña debe ser la misma que `REDIS_PASSWORD`. |

Genera los secretos fuera del repositorio. Por ejemplo, para `GIT_ENCRYPTION_KEY`:

```powershell
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Si una contraseña de PostgreSQL contiene caracteres reservados de URL (`@`, `:`, `/`, `#` o `%`), codifícala en `DATABASE_URL` mientras mantienes el valor original en `DB_PASSWORD`.

## Variables de Compose opcionales

`.env.example` deja valores iniciales para `IMAGE_TAG`, `API_WORKERS`, `BACKEND_MEM_LIMIT`, `BACKEND_CPUS`, `BACKEND_PIDS_LIMIT`, `CELERY_LOGLEVEL`, `CELERY_CONCURRENCY`, `CELERY_MAX_TASKS_PER_CHILD` y `REDIS_MEM_LIMIT`. Se pueden ajustar desde Dokploy. `DOCKER_GID` debe ser el GID del grupo dueño de `/var/run/docker.sock` en el host; consulta con `stat -c '%g' /var/run/docker.sock` y reemplaza el `999` de ejemplo.

Antes de desplegar, prepara en el host Dokploy la ruta temporal de los workspaces y dale acceso al UID/GID de la aplicación (`10001`):

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /tmp/fenix_workspaces
```

El worker usa el socket Docker del host para crear los contenedores sandbox. El `:ro` del montaje del socket no limita las operaciones de Docker; protege el filesystem del montaje, no las llamadas al daemon. Restringe acceso al servicio worker y configura el cerco de egreso de Strix en el host antes de permitir escaneos: `sudo bash scripts/harden_runner_egress.sh`, con persistencia de esas reglas tras reinicio. `STRIX_REQUIRE_EGRESS_FENCE=true` hace que los escaneos fallen cerrados si falta el cerco.

## Acceso a los datos

Dokploy y sus contenedores deben tener Tailscale conectado a la red donde escucha PostgreSQL. Mantén PostgreSQL sin puertos públicos; `DB_HOST` debe ser una IP Tailscale alcanzable desde Dokploy, no `localhost` ni un nombre DNS público. Redis queda en el compose y no publica puertos al host.

Tras guardar Environment y configurar Domains, redepliega. Si falla el build del frontend, confirma que no aparece `setcap` y que el paso de runtime termina. Si frontend compila pero el dominio no responde, revisa que el dominio del panel tenga puerto `8080`, que el de API tenga `8000` y que ambos contenedores estén conectados a `dokploy-network`.
