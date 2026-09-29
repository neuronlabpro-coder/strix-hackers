#!/usr/bin/env bash
# Limita la salida de red de los contenedores del runner a DNS y HTTPS.
#
# ## Que resuelve
#
# Cada trabajo de escaneo crea una red bridge propia y el contenedor sale de ella con la
# misma salida que el host. Eso incluye los metadatos de la nube (`169.254.169.254`), la red
# de Tailscale, Redis y PostgreSQL del despliegue, y cualquier otra red interna. Un motor de
# escaneo **necesita** salir: tiene que clonar repositorios, resolver nombres y llamar al
# proveedor de inferencia. Lo que no necesita, y que es el riesgo, es salir a donde no toca.
#
# Este script instala las reglas en la cadena `DOCKER-USER`, que es la unica de las cadenas de
# Docker que se evalua **antes** de las reglas de aceptacion propias. Ponerlas en `FORBID` o en
# la cadena del puente no funciona: Docker inserta sus propios `ACCEPT` despues y se comerian
# las reglas.
#
# ## Que permite
#
#   - Lo ya establecido, para que una respuesta pueda volver.
#   - DNS: UDP y TCP al 53, hacia el resolvedor del host.
#   - HTTPS: TCP al 443, que es por donde va el trafico del proveedor de inferencia, de Git y
#     de todo lo demas que el escaneo consulta.
#
# Todo lo demas —HTTP a proposito, FTP, SSH desde dentro, SMB, NFS, y cualquier otra cosa que
# se pueda hablar por TCP o UDP— se descarta en silencio. Sin `REJECT`: un `DROP` no genera
# respuestas, y no darle pistas al que escanea es parte del objetivo.
#
# ## Por que el pool de subredes
#
# Las reglas necesitan **nombrar** el trafico. El nombre del puente lo asigna Docker como
# `br-<id de red>`, y ese identificador no existe hasta que el trabajo crea la red: una regla
# escrita antes del despliegue no puede referirse a el.
#
# Por eso el runner declara su propio pool (`STRIX_NETWORK_POOL`, por defecto
# `172.31.0.0/16`) y cada red corta una subred de ahi. El pool si se puede nombrar, y las reglas
# apuntan a el.
#
# ## Idempotencia
#
# Se puede ejecutar tantas veces como haga falta: cada regla se borra por su comentario antes
# de anadirla. Sin eso, un segundo despliegue dejaria reglas duplicadas y un
# `docker network prune` no las limpiaria nunca.
#
# ## Lo que NO hace
#
# No restringe la entrada al host desde el contenedor: `DOCKER-USER` gobierna el trafico que
# cruza el puente, y para la entrada hace falta la lista de la propia interfaz del host. Un
# contenedor con `cap_drop=ALL` y `no-new-privileges` no puede abrir un puerto de escucha que
# se vea desde fuera, pero un proceso en el host si puede alcanzar el contenedor.
#
# Uso: `sudo bash scripts/harden_runner_egress.sh [pool]`
# Por defecto usa `172.31.0.0/16`, el mismo valor que `STRIX_NETWORK_POOL` por defecto.

set -euo pipefail

POOL="${1:-172.31.0.0/16}"
COMENTARIO="fenix: egress del runner"
CADENA="DOCKER-USER"

log() { printf '%s\n' "$*" >&2; }

if ! command -v iptables >/dev/null 2>&1; then
  log "FALLO: iptables no esta instalado. Sin el no hay forma de restringir la salida."
  exit 1
fi

if ! iptables -n -L "${CADENA}" >/dev/null 2>&1; then
  log "FALLO: la cadena ${CADENA} no existe, o el modulo de Docker no esta cargado."
  log "Arranca Docker y vuelve a intentarlo: 'systemctl start docker'."
  exit 1
fi

# Se comprueba que el pool es un CIDR antes de tocar la cadena. Un valor mal escrito aqui
# produciria una regla que no corta nada, y creeria que si.
if ! ipcalc -s -c "${POOL}/32" >/dev/null 2>&1; then
  # `ipcalc` no siempre esta. Se cae a una validacion con python3, que el despliegue ya
  # necesita para el resto de la plataforma.
  if ! python3 -c "import ipaddress,sys; ipaddress.ip_network(sys.argv[1])" "${POOL}" 2>/dev/null; then
    log "FALLO: '${POOL}' no es un CIDR valido."
    exit 1
  fi
fi

log "Pool de subredes del runner: ${POOL}"
log "Cadena: ${CADENA}"

# Se borran las reglas propias antes de anadir las de este despliegue, para que repetidas
# ejecuciones no acumulen duplicados.
while iptables -n -L "${CADENA}" --line-numbers 2>/dev/null \
    | grep -F "${COMENTARIO}" \
    | awk '{print $1}' \
    | sort -rn \
    | while read -r numero; do
        log "  quitando regla previa ${numero}"
        iptables -D "${CADENA}" "${numero}" 2>/dev/null || true
    done
do :; done

anadir() {
  local regla=("$@")
  # `iptables -C` dice si la regla ya existe; se salta para que el log no mienta.
  if iptables -C "${CADENA}" "${regla[@]}" 2>/dev/null; then
    log "  ya estaba: ${regla[*]}"
    return 0
  fi
  iptables -I "${CADENA}" "$@" -m comment --comment "${COMENTARIO}"
  log "  anadida: ${regla[*]}"
}

# El orden importa y va de mas especifico a mas general: `FORWARD` acepta la respuesta de lo
# que ya salio, y el `DROP` va **al final** para no cutting el trafico permitido. Insertar con
# `-I` pone cada regla al principio, asi que se insertan en orden inverso al logical.

# 1. Lo permitido, arriba del todo.
anadir -s "${POOL}" -p udp --dport 53 -j RETURN
anadir -s "${POOL}" -p tcp --dport 53 -j RETURN
anadir -s "${POOL}" -p tcp --dport 443 -j RETURN
anadir -s "${POOL}" -p tcp -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

# 2. Y al final, todo lo demas. Con `-A` para que quede despues de lo anterior.
if iptables -C "${CADENA}" -s "${POOL}" -j DROP 2>/dev/null; then
  log "  ya estaba: el DROP de salida"
else
  iptables -A "${CADENA}" -s "${POOL}" -j DROP -m comment --comment "${COMENTARIO}"
  log "  anadida: DROP de todo lo demas que salga de ${POOL}"
fi

log ""
log "Reglas de ${CADENA} que pertenecen a este despliegue:"
iptables -n -L "${CADENA}" -v --line-numbers | grep -F "${COMENTARIO}" >&2 || true
log ""
log "Comprobacion de que la regla se aplica:"
log "  iptables -t filter -L ${CADENA} -v --line-numbers"
log ""
log "Ojo: las reglas de iptables no sobreviven a un reinicio del host."
log "Este script tiene que correr en el arranque, no una vez en el despliegue."
