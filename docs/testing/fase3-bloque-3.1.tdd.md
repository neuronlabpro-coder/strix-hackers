# Evidencia TDD · Fase 3 · Bloque 3.1

## Alcance

Se implementaron la criptografía AES-256-GCM, los modelos Git, la migración PostgreSQL, los clientes REST de GitHub/GitLab y el endpoint unificado de webhooks con validación HMAC.

## RED

Los tests iniciales fallaron porque no existían `backend.core.crypto`, `backend.apps.repositories`, los clientes ni el endpoint de webhooks.

## GREEN

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
101 passed, 1 skipped

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
| AES-256-GCM round-trip, tamper y formato inválido | `backend/tests/test_git_crypto.py` |
| Modelos, enums, índices y secreto HMAC aleatorio | `backend/tests/test_git_models.py` |
| Firmas válidas GitHub/GitLab/Bitbucket/Gitea → 202 | `backend/tests/test_git_webhooks.py` |
| Firma manipulada o ausente → 401 | `backend/tests/test_git_webhooks.py` |
| Credenciales cifradas y no reutilizables entre tenants | `backend/tests/test_git_webhooks.py` + AAD por organización/proveedor/campo |
| Revisiones Git vinculadas al tenant del repositorio/run | `backend/tests/test_git_webhooks.py` + FK/trigger R3 |
| Auth REST y sanitización de errores | `backend/tests/test_git_clients.py` |

## Límites

- OAuth, registro de webhooks, normalización de eventos PR/MR, escaneo CI/CD, ChatOps y Autofix quedan para los siguientes bloques de Fase 3.
- La protección de replay usa `SET NX` en Redis con TTL; si el proveedor no envía delivery ID, se deriva un hash del body firmado. Queda validar el broker Redis real y el proxy de egress de runners en staging.
- La semántica exactly-once frente a una caída entre la reserva Redis y `delay` requiere un inbox/outbox durable en el siguiente bloque; el código actual libera la reserva cuando el publish falla.
- No se han ejecutado llamadas reales contra GitHub/GitLab; las pruebas usan `httpx.MockTransport`.
