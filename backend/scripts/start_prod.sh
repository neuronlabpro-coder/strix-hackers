#!/usr/bin/env bash
# =============================================================================
# Arranque de producción del backend.
#
# ## Por qué este script y no un `command:` en el compose
#
# Porque hay tres cosas que solo se pueden hacer una vez y en un orden, y ninguna de ellas es
# "arrancar Uvicorn": esperar a que PostgreSQL acepte conexiones, aplicar las migraciones, y
# opcionalmente sembrar. Encadenarlas en un `command:` de shell dentro del YAML las haría
# ilegibles, y un YAML ilegible es un YAML que nadie se atreve a tocar.
#
# ## Por qué las migraciones NO se ejecutan por defecto
#
# Porque es la decisión que más caro sale cuando sale mal, y la razón es la concurrencia.
#
# `alembic upgrade head` no es idempotente frente a dos procesos: si dos contenedores arrancan a
# la vez, los dos ven que la migración no está aplicada y los dos intentan aplicarla. Uno gana y
# el otro se estrella, o peor: los dos leen el *nivel* y calculan mal el siguiente. Con un
# `deploy: replicas: 2` en el backend, eso no es un caso raro sino el caso normal de un despliegue.
#
# Además, un contenedor que migra en cada arranque hace que un simple reinicio de la aplicación
# acquisitiona un bloqueo sobre la base, aunque no haya cambiado ningún esquema. En una ventana de
# mantenimiento eso significa caída sin motivo.
#
# Por eso las migraciones van en un servicio aparte, `migrate`, que corre una vez, termina, y no
# se reinicia. Este script las ejecuta **solo** si se le pide explícitamente con `--migrate`, que
# es lo que hace ese servicio.
#
# ## Por qué el esperón de conectividad no espera "a que la base esté"
#
# Porque "estar" no es un estado observable desde fuera. `pg_isready` devuelve que el proceso
# acepta conexiones antes de que la base acepte el primer `SELECT` de una base recién
# inicializada, y la conexión a través de Tailscale añade otra capa de carrera. Lo que se
# comprueba es lo que importa de verdad: que una consulta trivial traverse la conexión y vuelva.
# =============================================================================
set -Eeuo pipefail

# --- Configuración -----------------------------------------------------------------
# Los valores vienen del entorno, nunca de aquí. R1: nada configurable en el código.
readonly APP_HOST="${APP_HOST:-0.0.0.0}"
readonly APP_PORT="${APP_PORT:-8000}"
readonly APP_WORKERS="${APP_WORKERS:-2}"

# Cuánto se espera a que PostgreSQL responda antes de rendirse. El límite es una decisión
# operativa, no técnica: más allá, un contenedor que no arranca es peor que uno que arranca y
# falla de forma visible, porque `restart: unless-stopped` lo convertiría en un bucle silencioso.
readonly DB_WAIT_SECONDS="${DB_WAIT_SECONDS:-90}"
readonly REDIS_WAIT_SECONDS="${REDIS_WAIT_SECONDS:-30}"
readonly WAIT_STEP_SECONDS="${WAIT_STEP_SECONDS:-2}"

# El seeder solo corre si se pide con SEED_ON_EMPTY=true, y solo si la base está vacía.
readonly SEED_ON_EMPTY="${SEED_ON_EMPTY:-false}"

# --- Salida estructurada ----------------------------------------------------------
# Los logs van a stderr, que es donde Docker los recoge, con un prefijo reconocible. Un
# contenedor que no dice qué está haciendo durante un arranque de cuarenta segundos parece
# colgado, y parece colgado es como se depuran los despliegues lentos.

# Se marca cuando el error ya se ha explicado, para que el `trap` no lo repita. Sin la marca, un
# fallo deliberado —que ya había impreso su causa— produce dos líneas de error para un solo
# problema, y la segunda dice "fallo en la linea 207: return 1", que no le aporta nada a quien
# lee el log de un despliegue. Peor aún: hace que parezca un fallo de bash donde lo que hay es
# una causa de configuración, que es justo lo que se empieza a buscar en el sitio equivocado.
fallo_explicado=""

log() { printf '[start_prod] %s\n' "$*" >&2; }
error() { printf '[start_prod] ERROR: %s\n' "$*" >&2; fallo_explicado="1"; }

trap '[[ -n "${fallo_explicado}" ]] || error "fallo inesperado en la linea ${LINENO}: ${BASH_COMMAND}"' ERR

# --- Esperas de conectividad ------------------------------------------------------

# `pg_isready` y `redis-cli` vienen de los paquetes cliente de sus imágenes, no de la nuestra. El
# script no los instala ni los compila: si faltan, es que la imagen está mal construida y es mejor
# que se note en el arranque y no en la primera conexión de un usuario.
esperar_postgres() {
  local objetivo="$1" espera="$2" restante="$3" inicio=$SECONDS

  log "esperando a PostgreSQL (${espera}s maximo)"

  while (( restante > 0 )); do
    if pg_isready --dbname="${objetivo}" --quiet; then
      # `pg_isready` dice que el *proceso* responde. La consulta de verdad va aparte, y es la
      # que detecta la base recién creada que aún no acepta consultas.
      if psql "${objetivo}" --quiet --no-align --tuples-only \
        --command 'SELECT 1' >/dev/null 2>&1; then
        log "PostgreSQL operativo tras $(( SECONDS - inicio ))s"
        return 0
      fi
    fi
    sleep "${WAIT_STEP_SECONDS}"
    restante=$(( espera - ( SECONDS - inicio ) ))
  done

  error "PostgreSQL no respondio en ${espera}s"
  return 1
}

esperar_redis() {
  local url="$1" espera="$2" restante="$3" inicio=$SECONDS

  log "esperando a Redis (${espera}s maximo)"

  while (( restante > 0 )); do
    # `redis-cli -u` con la URL completa, porque Redis va con contraseña y sin ella el PONG que
    # devuelve el servidor sin autenticar es un `NOAUTH` con forma de respuesta.
    if redis-cli -u "${url}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
      log "Redis operativo tras $(( SECONDS - inicio ))s"
      return 0
    fi
    sleep "${WAIT_STEP_SECONDS}"
    restante=$(( espera - ( SECONDS - inicio ) ))
  done

  error "Redis no respondio en ${espera}s"
  return 1
}

comprobar_dependencias() {
  local ejecutables=(alembic pg_isready psql redis-cli)
  local ausente=()

  for ejecutable in "${ejecutables[@]}"; do
    command -v "${ejecutable}" >/dev/null 2>&1 || ausente+=("${ejecutable}")
  done

  if (( ${#ausente[@]} > 0 )); then
    error "faltan ejecutables en la imagen: ${ausente[*]}"
    error "la imagen no esta bien construida; revisar Dockerfile.backend"
    return 1
  fi
}

# --- Migraciones y siembra --------------------------------------------------------

aplicar_migraciones() {
  log "aplicando migraciones hasta head"
  # `--no-version-check` evita un SELECT extra a `alembic_version` que en este punto ya se ha
  # tocado. No es una optimizacion: es que la propia migracion puede haberla cambiado.
  alembic upgrade head
  log "migraciones aplicadas"
}

# El seeder es idempotente por construccion, asi que ejecutarlo con la base ya sembrada no
# duplica nada. Aun asi, se comprueba antes, porque en una base de produccion real el seeder es
# una operacion que no deberia depender de esa propiedad para no ejecutarse por sorpresa.
base_vacia() {
  local total
  total="$(psql "${DATABASE_URL_SYNC}" --quiet --no-align --tuples-only \
    --command "SELECT count(*) FROM organizations" 2>/dev/null || echo '-1')"

  [[ "${total}" == "0" ]]
}

sembrar_si_esta_vacia() {
  if [[ "${SEED_ON_EMPTY}" != "true" ]]; then
    log "SEED_ON_EMPTY!=true: no se siembra"
    return 0
  fi

  if ! base_vacia; then
    log "la base ya tiene organizaciones: no se siembra"
    return 0
  fi

  log "sembrando el workspace de demostracion (base vacia)"
  python scripts/seed_demo_workspace.py
  log "siembra completada"
}

# --- Arranque ---------------------------------------------------------------------

arrancar_uvicorn() {
  # `exec` y no un subshell con `&`: reemplaza el proceso de este script por el de Uvicorn, de
  # modo que SIGTERM de Docker llega directamente a Uvicorn. Con un subshell intermedio, Uvicorn
  # no recibe la senal y el contenedor se mata por la via dura, que es `stop_grace_period`
  # desperdiciado y peticiones cortadas a la mitad.
  #
  # El modulo es `backend.main:app` y no `apps.main:app`: la instancia vive en
  # `backend/main.py`, y `apps` es el paquete donde están los routers y los módulos de dominio.
  # Un nombre inventado no falla en el arranque, falla con "ModuleNotFoundError" en el primer
  # despliegue, que es justo cuando peor se lleva.
  log "arrancando uvicorn en ${APP_HOST}:${APP_PORT} con ${APP_WORKERS} workers"
  exec uvicorn backend.main:app \
    --host "${APP_HOST}" \
    --port "${APP_PORT}" \
    --workers "${APP_WORKERS}" \
    --proxy-headers \
    --forwarded-allow-ips '*'
}

main() {
  local aplicar_migraciones_flag="${RUN_MIGRATIONS:-false}"

  while (( $# > 0 )); do
    case "$1" in
      --migrate) aplicar_migraciones_flag="true" ;;
      --seed) SEED_ON_EMPTY="true" ;;
      --help|-h)
        cat <<'AYUDA'
uso: start_prod.sh [--migrate] [--seed]

  --migrate   aplica `alembic upgrade head` antes de arrancar. Lo usa el servicio `migrate`.
  --seed      siembra el workspace de demostracion si la base esta vacia.
              Equivale a SEED_ON_EMPTY=true.
AYUDA
        return 0
        ;;
      *) error "opcion desconocida: $1"; return 2 ;;
    esac
    shift
  done

  comprobar_dependencias

  # La conexion que usa `psql` es la sincrona, porque el cliente `psql` no habla asyncpg. La
  # conversion de una a otra la hace el modulo de configuracion; aqui solo se hace una vez y con
  # un mensaje si falta, porque sin esto el fallo es un error de conexion que no dice que la
  # variable no existe.
  if [[ -z "${DATABASE_URL_SYNC:-}" ]]; then
    log "derivando DATABASE_URL_SYNC desde DATABASE_URL"
    export DATABASE_URL_SYNC
    DATABASE_URL_SYNC="$(python -c '
import os
from backend.core.config import get_settings
print(get_settings().database_url_sync)
')"
  fi

  # `pg_isready` no acepta la URL de SQLAlchemy, que empieza por `postgresql+asyncpg://`. Se le
  # pasa el DSN de Postgres, que es lo mismo con otro esquema.
  local dsn_postgres="${DATABASE_URL_SYNC/postgresql+asyncpg/postgresql}"

  esperar_postgres "${dsn_postgres}" "${DB_WAIT_SECONDS}" "${DB_WAIT_SECONDS}"

  if [[ -n "${REDIS_URL:-}" ]]; then
    esperar_redis "${REDIS_URL}" "${REDIS_WAIT_SECONDS}" "${REDIS_WAIT_SECONDS}"
  else
    # No es un aviso: el backend lo necesita para las colas, y arrancar sin el convierte un
    # fallo de arranque en un fallo de la primera tarea encolada, que es mucho mas dificil de
    # atribuir.
    error "REDIS_URL no esta definida: el backend no puede arrancar sin ella"
    return 1
  fi

  if [[ "${aplicar_migraciones_flag}" == "true" ]]; then
    aplicar_migraciones
  fi

  sembrar_si_esta_vacia

  arrancar_uvicorn
}

main "$@"
