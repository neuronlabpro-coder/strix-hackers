# Evidencia TDD · Fase 3 · Bloque 3.2

## Alcance

Se implementaron el pipeline de revisión de Pull Requests, materialización shallow y efímera, feedback de findings, ChatOps, autofix y las protecciones de aislamiento del workspace.

## RED

Las pruebas iniciales fallaron porque no existían `workspace.py`, `pipeline.py`, `autofix.py`, `chatops.py` ni la tarea `run_pr_security_pipeline`.

## GREEN

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
126 passed, 2 skipped

uv run --project backend ruff check backend --config backend/pyproject.toml
All checks passed!

uv run --project backend pyright backend --project backend/pyproject.toml
0 errors, 0 warnings, 0 informations

uv run --project backend alembic -c backend/alembic.ini check
No new upgrade operations detected.

uv run --project backend alembic -c backend/alembic.ini current
e8a0b2c4d6e8 (head)
```

## Garantías cubiertas

| Garantía | Evidencia |
| --- | --- |
| Clonado `--depth=1`, `--single-branch`, `--no-tags` y token solo en entorno | `backend/tests/test_pr_workspace.py` |
| Commit head y base inmutables, incluidos forks, y diff sin depender de merge-base | `backend/tests/test_pr_workspace.py` |
| Diff acotado, traversal y symlinks rechazados | `backend/tests/test_pr_workspace.py` |
| Workspace preparado montado y purgado en `finally`; cleanup parcial queda durable | `backend/tests/test_pr_pipeline.py`, `backend/tests/test_docker_sandbox.py` |
| Transiciones `QUEUED → SCANNING → PASSED/FAILED` | `backend/tests/test_pr_pipeline.py` |
| Checks `pending/failure/success` y comentarios con PoC | `backend/tests/test_pr_pipeline.py` |
| Feedback limpio sin comentario intrusivo y actualización de comentario previo | `backend/tests/test_pr_pipeline.py`, `backend/apps/repositories/feedback.py` |
| Autofix con patch validado y rama `fenix/fix-{id}` | `backend/tests/test_pr_autofix.py` |
| Clientes GitHub/GitLab para comentario, status, permisos y autofix | `backend/tests/test_git_clients.py` |
| Parser ChatOps y eventos PR/comment | `backend/tests/test_chatops.py` |
| Idempotencia de revisión por repositorio, PR, head y base | `backend/migrations/versions/e6d8f0a2b4c6_add_pr_review_idempotency.py`, `e8a0b2c4d6e8_scope_pr_review_idempotency.py` |
| Replay ligado al body firmado y retry de encolado | `backend/tests/test_git_webhooks.py`, `backend/tests/test_chatops.py` |
| Watchdog reconcilia runs, reviews y revisiones QUEUED obsoletas | `backend/tests/test_watchdog.py` |

## Límites

- Falta ejecutar el pipeline contra GitHub/GitLab reales y Docker Linux; las pruebas de proveedor usan `httpx.MockTransport`.
- La garantía exactly-once frente a una caída entre Redis, PostgreSQL, Celery y Git requiere un inbox/outbox durable; el watchdog actual es una compensación best-effort.
- R5 requiere que `STRIX_WORKSPACE_ROOT` se monte en tmpfs o volumen cifrado efímero y que la verificación forense se ejecute en staging.
- Los clientes de autofix cubren GitHub y GitLab; Bitbucket/Gitea quedan para el siguiente bloque de conectores.
- La imagen Strix debe consumir `STRIX_INCREMENTAL_FILES`; el contrato de la imagen real está pendiente de validación E2E.
