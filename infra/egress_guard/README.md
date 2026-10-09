# Guard de salida Strix Fenix Team

El servicio corre en el **host** del daemon Docker, no en Compose. No se instala ni
arranca con este bloque. El código versionado es `fenix_egress_guard/`; la unidad
`fenix-egress-guard.service` espera ese paquete en `/opt/mindguard-fenix/` y un
`/etc/mindguard-fenix/egress-guard.env` con:

```ini
FENIX_STRIX_NETWORK=fenix-team-strix-prod
FENIX_STRIX_SUBNET=172.31.250.0/24
FENIX_DNS_RESOLVERS=1.1.1.1,1.0.0.1
```

En una VM Linux desechable se puede probar una red distinta con
`FENIX_GUARD_TEST_MODE=true` y `FENIX_STRIX_NETWORK=fenix-team-strix-test`.
La subnet debe ser un `/24` privado sin solapamiento Docker. El broker de prueba
usa `FENIX_REQUIRE_PROD_NETWORK=false` y mantiene
`FENIX_REQUIRE_EGRESS_ATTESTATION=true`; la producción conserva ambos requisitos
en `true` y su red fija `172.31.250.0/24`.

Antes de arrancarlo habrá que crear la red exclusiva con IPAM `/24`, IPv6 deshabilitado,
label `com.mindguard.fenix=true` y opción
`com.docker.network.bridge.enable_icc=false`. El guard aborta ante solapamiento con
otra red Docker o ante contenedores sin la label Fenix conectados a ella.

Los dos resolvedores están aprobados. Solo se permiten UDP/TCP 53 hacia esas IP.

`python3 -m fenix_egress_guard.guard policy-hash` calcula el hash de las reglas
versionadas. Ese valor se configura en Dokploy como `FENIX_EGRESS_POLICY_HASH`.
Worker y broker leen `/run/mindguard-fenix/egress-status.json` mediante bind mount de
solo lectura. Cada atestación dura 5 segundos y el servicio verifica cada 2.

`apply` crea únicamente `FENIX_TEAM_EGRESS` y un salto acotado al `/24` al inicio de
`DOCKER-USER` y otro de `INPUT`. No vacía ninguna cadena. `check` solo lee Docker e
iptables. El servicio ejecuta `apply → check → attest` al arrancar; si un check
periódico falla, elimina la atestación. Ante una cadena propia alterada, `apply`
falla cerrado y exige intervención explícita, sin reescribir una política en uso.

La política usa **DROP** para que el tráfico no autorizado no reciba respuestas del
host. Bloquea destinos locales y privados, incluidos el gateway, antes de permitir
DNS a los dos resolvedores aprobados y TCP/443 público. Docker DNS `127.0.0.11` reside en el
namespace del sandbox; el broker fija ambos resolvedores externos al crearlo.

La atestación ofrece una ventana máxima de 5 segundos ante pérdida súbita de reglas.
No es una defensa frente a root o un administrador con acceso directo al daemon.
