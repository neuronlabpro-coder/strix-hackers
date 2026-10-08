# Despliegue en Dokploy

## Compose y dominios

Usa `docker-compose.prod.yml` como Compose Path. En la pestaña **Environment** de Dokploy, pega el contenido de `.env.example` y sustituye los valores de desarrollo por los valores de producción descritos abajo. Dokploy guarda esas entradas como `.env`; Compose lo lee para interpolar variables y los servicios backend lo cargan mediante `env_file`.

No declares un servicio Traefik en este compose ni dupliques las etiquetas que Dokploy genera al configurar **Domains**. Añade los dominios desde esa pestaña y redepliega:

| Servicio | Dominio | Puerto del contenedor |
| --- | --- | ---:|
| `backend` | `api.mindguardredteam.com` | `8000` |
| `frontend` | `panel.mindguardredteam.com` | `80` |

El panel escucha en el puerto 80 dentro del contenedor, coincidiendo con el dominio frontend de Dokploy. API se conecta a `dokploy-network` para Traefik y a `fenix-network` para alcanzar PostgreSQL y Redis. El panel solo necesita `dokploy-network`.

## Valores de producción obligatorios

Empieza con todas las claves de `.env.example`, que contiene tanto los valores de configuración del backend como los límites del runner. Cambia como mínimo estos valores:

| Variable(s) | Valor requerido |
| --- | --- |
| `SECRET_KEY` | Secreto aleatorio propio, de al menos 32 caracteres. |
| `ENVIRONMENT`, `DEBUG`, `RUN_MIGRATIONS` | `production`, `false`, `false`. El compose fija estos valores; el esquema debe estar migrado antes de desplegar. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DATABASE_URL` | PostgreSQL 16 en la red compartida: host `fenix-postgres`, puerto interno `5432`. Usa las credenciales de `FENIX_POSTGRES_*`; `DATABASE_URL` debe coincidir exactamente con `DB_*`. |
| `REDIS_PASSWORD` | Debe ser igual a `FENIX_REDIS_PASSWORD` del servidor de datos. Si incluye caracteres reservados, codifícalos en `REDIS_URL`. |
| `GIT_ENCRYPTION_KEY` | Clave base64 que decodifique a 32 bytes. No sirve el valor de ejemplo del repositorio. |
| `DEFAULT_STRIX_LLM`, `LLM_API_KEY`, `LLM_API_BASE` | Modelo existente en la cuenta del proveedor, credencial y base HTTPS OpenAI-compatible. |
| `STRIX_LLM_KEY_EXPOSURE_ACK` | Para habilitar el lanzamiento de escaneos, escribe exactamente `la-clave-del-proveedor-entra-en-el-contenedor-aceptado`. La clave LLM se entrega al sandbox; considera primero un proxy de inferencia o credenciales limitadas por escaneo. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Credenciales activas del entorno Stripe correspondiente (`sk_live_…` y `whsec_…`). Son obligatorias para arrancar en producción. |
| `EMAIL_VERIFICATION_DELIVERY_MODE`, `FRONTEND_BASE_URL`, `API_PUBLIC_BASE_URL` | `smtp`, `https://panel.mindguardredteam.com`, `https://api.mindguardredteam.com`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `EMAIL_VERIFICATION_FROM` | SMTP accesible desde Dokploy; usuario y contraseña juntos, TLS `true` y remitente válido. |
| `VITE_API_URL` | `https://api.mindguardredteam.com`. Es argumento de build; cambia el bundle cuando cambie. |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `CELERY_REDIS_DB`, `REDIS_URL` | Redis en la red compartida: host `fenix-redis`, puerto interno `6379`, bases 0 y 1. `REDIS_URL` debe coincidir con host, puerto, contraseña y base 0. |

En el Environment de producción de Dokploy, usa `DB_HOST=fenix-postgres`, `DB_PORT=5432`, `REDIS_HOST=fenix-redis` y `REDIS_PORT=6379`. Las URLs deben usar esos mismos hosts y puertos (`DATABASE_URL` con `fenix-postgres:5432` y `REDIS_URL` con `fenix-redis:6379/0`). No uses aquí los puertos publicados para Tailscale (`5433` y `6380`): esos son para desarrollo local fuera de Docker. Celery deriva la base `/1` automáticamente desde `REDIS_URL`.

El stack de aplicación debe compartir la red Docker `fenix-network` con los contenedores `fenix-postgres` y `fenix-redis`. `infra/dokploy/docker-compose.yml` declara esa red con nombre estable; despliega la red de datos primero si aún no existe.

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

El worker usa el socket Docker del host para crear los contenedores sandbox. El `:ro` del montaje del socket no limita las operaciones de Docker; protege el filesystem del montaje, no las llamadas al demonio. Restringe acceso al servicio worker y configura el cerco de egreso de Strix en el host antes de permitir escaneos: `sudo bash scripts/harden_runner_egress.sh`, con persistencia de esas reglas tras reinicio. `STRIX_REQUIRE_EGRESS_FENCE=true` hace que los escaneos fallen cerrados si falta el cerco.

La imagen instala `strix-agent==1.7.0` en `/opt/strix`; el Compose fija `STRIX_EXECUTION_MODE=host`, `STRIX_CLI_PATH=/opt/strix/bin/strix` y monta `/tmp/fenix_workspaces` en la misma ruta del host y del worker. El script `start_worker.sh` comprueba `strix --version`, el socket y el workspace antes de iniciar Celery. El CLI corre como proceso del worker y crea sus contenedores sandbox sibling mediante el daemon del host. Comprueba `docker compose exec celery_worker /opt/strix/bin/strix --version` sin lanzar un escaneo ni llamar al LLM. En producción, `container` se rechaza durante la validación de configuración.

## Por qué un escaneo puede fallar en menos de un segundo

Porque hay cuatro comprobaciones de **despliegue** antes de que el contenedor exista, y ninguna es de código. Todas fallan en el mismo intervalo —sub segundo— y sin dejar contenedor, que es lo que las hace indistinguibles a simple vista.

| Síntoma en el panel | Qué pasa en realidad | Qué hacer en el host |
| --- | --- | --- |
| `STRIX_DOCKER_UNAVAILABLE` | El worker no puede abrir el socket. Es `DOCKER_GID` distinto del GID real del socket, o el socket sin montar | `stat -c '%g' /var/run/docker.sock` y corregir `DOCKER_GID` |
| `STRIX_EGRESS_FENCE_MISSING` | Falta el cerco de salida y el runner se niega a lanzar | `sudo bash scripts/harden_runner_egress.sh`, en el arranque del host |
| `STRIX_LLM_KEY_ACK_MISSING` | Falta `STRIX_LLM_KEY_EXPOSURE_ACK` con la frase exacta | Ponerla en el Environment de Dokploy |
| `STRIX_IMAGE_UNAVAILABLE` | La imagen de `STRIX_SANDBOX_IMAGE` no está descargada | `docker pull` de esa imagen en el host del worker |

El panel **explica** el motivo en la ficha del escaneo, y `GET /api/v1/pentests/readiness` dice por adelantado lo que sí se puede comprobar desde el proceso de la API: el directorio de trabajo, el cerco y el reconocimiento de la clave. El demonio Docker y la imagen **no** se comprueban desde ahí a propósito, porque el proceso que responde es el de la API y en este despliegue el worker corre en otro contenedor: preguntar ahí describiría el host equivocado. Para esos dos motivos, la autoridad es el código del escaneo fallido.

## El comando más rápido para diagnosticar un escaneo que falla

```bash
docker compose exec celery_worker sh -c 'ls -l /var/run/docker.sock; stat -c "%g" /var/run/docker.sock'
```

Si el `stat` no devuelve el GID que tiene `DOCKER_GID`, esa es la causa. Es el caso más frecuente y el que produce un `STRIX_EXECUTION_FAILED` sin ningún otro rastro: el `PermissionError(13)` ocurre dentro de `docker.from_env()` y, sin el diagnóstico, la excepción se perdía dentro de un `SandboxExecutionError` genérico.

## Acceso a los datos

En producción, los stacks de aplicación y datos están en el mismo host Docker y comparten `fenix-network`. Usa los alias `fenix-postgres:5432` y `fenix-redis:6379`; los puertos publicados `5433` y `6380` son exclusivamente para desarrollo local a través de Tailscale. Este compose no contiene servicio `migrate`, PostgreSQL ni Redis. `RUN_MIGRATIONS=false` impide que el entrypoint aplique cambios de esquema durante el despliegue.


Tras guardar Environment y configurar Domains, redepliega. Si falla el build del frontend, confirma que no aparece `setcap` y que el paso de runtime termina. Si frontend compila pero el dominio no responde, revisa que el dominio del panel tenga puerto `80`, que el de API tenga `8000` y que ambos contenedores estén conectados a `dokploy-network`.
