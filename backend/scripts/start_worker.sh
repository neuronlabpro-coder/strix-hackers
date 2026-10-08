#!/bin/sh
set -eu

# Falla antes de que Celery consuma trabajos si falta el CLI o el daemon Docker.
python -m backend.workers.runner.production_preflight
exec celery -A backend.workers.celery_app:celery_app worker "$@"
