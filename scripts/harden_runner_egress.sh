#!/usr/bin/env bash
# El cerco anterior sobre DOCKER-USER no cubre la arquitectura broker + red /24.
# Se conserva la ruta para que despliegues antiguos fallen de forma explícita.
set -euo pipefail
printf '%s\n' 'Este script está retirado. Usa fenix-egress-guard.service en el host.' >&2
exit 1
