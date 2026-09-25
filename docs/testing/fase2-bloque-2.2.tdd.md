# Evidencia TDD · Fase 2 · Bloque 2.2

## Alcance

Se implementaron el runner Docker aislado, límites de recursos, cleanup R5, ejecución Celery, dispatch API, cancelación, watchdog de arranque/periódico y transición de timeouts.

## RED

- Los tests iniciales no pudieron recolectar `backend.workers.runner.sandbox` porque el módulo y la dependencia Docker aún no existían.
- El watchdog y el ciclo de vida de API también fallaron por ausencia de `execute_pentest_run`, `reconcile_orphaned_runs` y los endpoints de dispatch/abort.

## GREEN y comandos ejecutados

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
75 passed, 1 skipped

uv run --project backend ruff check backend --config backend/pyproject.toml
All checks passed!

uv run --project backend pyright backend --project backend/pyproject.toml
0 errors, 0 warnings, 0 informations

uv run --project backend alembic -c backend/alembic.ini check
No new upgrade operations detected.

uv run --project backend alembic -c backend/alembic.ini current
d0e2f4a6b8c9 (head)
```

## Pruebas del Bloque 2.2

| Garantía | Evidencia |
| --- | --- |
| Bridge dedicado, cgroups 4 GB/2 vCPU y ausencia de `docker.sock` | `backend/tests/test_docker_sandbox.py` |
| Cleanup forzoso ante excepción y timeout duro/blando | `backend/tests/test_docker_sandbox.py` |
| Dispatch Celery y persistencia del task ID | `backend/tests/test_pentest_lifecycle_api.py` |
| Abort, kill, revoke y protección cross-tenant | `backend/tests/test_pentest_lifecycle_api.py` |
| Watchdog marca runs obsoletos y purga workspace | `backend/tests/test_watchdog.py` |
| Reintento durable de cleanup pendiente | `cleanup_pending` + `backend/tests/test_watchdog.py` |
| Transición `TIMED_OUT` | `backend/tests/test_watchdog.py` |
| Configuración Celery y beat periódico | `backend/tests/test_celery_app.py` |

## Límites

- Las pruebas usan mocks de Docker; todavía no se ejecuta una validación real contra `ghcr.io/usestrix/strix-sandbox:latest`.
- El test de symlink se omite en Windows por falta de privilegios para crear enlaces; el runner usa `lstat`, `O_NOFOLLOW` y comparación de inode en el host Linux.
- La imagen real y su contrato de salida (`--scan-mode`, `--output`, `run-name` y JSON) todavía deben validarse en Linux; la referencia local de Strix no se modifica. `docker version` no pudo conectarse al daemon local (`npipe:////./pipe/dockerDesktopLinuxEngine`).
- El bridge por run aísla containers, pero el egress controlado y la protección SSRF/DNS rebinding requieren el proxy de red de producción; no se declaran cerrados en este bloque.
- La credencial LLM se inyecta porque el runner la necesita; queda pendiente sustituirla por un broker/secret scoped por run para impedir exfiltración desde el sandbox.
- El modo `REPOSITORY` presupone que el conector de Fase 3 materializa el repositorio en `/workspace/target`; este bloque no clona código en el host.
- La validación E2E de imagen, permisos del daemon, workspace real y concurrencia de contenedores queda pendiente para el cierre de Fase 2.
