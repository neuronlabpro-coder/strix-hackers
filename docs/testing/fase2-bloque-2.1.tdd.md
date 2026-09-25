# Evidencia TDD · Fase 2 · Bloque 2.1

## Alcance

Se implementaron los modelos `PentestRun` y `Vulnerability`, la migración de esquema y endurecimiento R4, el parser Strix, la configuración Celery, la ingesta atómica y los endpoints multi-tenant. El runner Docker, el despacho de un sandbox y la cancelación de scans siguen fuera de este bloque y pendientes de la siguiente etapa de Fase 2.

## RED

- Los tests iniciales de parser/modelos fallaron durante la recolección por ausencia de `backend.apps.pentests`, `backend.apps.vulnerabilities` y `backend.workers`.
- Los tests de configuración y flujo de invitación fallaron porque producción aceptaba el modo `development`, no existía reenvío de verificación y no existía aceptación de invitaciones.
- El test de inmutabilidad de identidad/eliminación falló antes de la migración `c3f8a1d9e2b4`, confirmando que el trigger inicial solo cubría `UPDATE` de tres campos.

## GREEN y verificación final

Comandos ejecutados desde la raíz salvo indicación contraria:

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

cd frontend && npm run typecheck
pass

cd frontend && npm run lint
pass

cd frontend && npm run build
pass

python .agents/skills/i18n-localization/scripts/i18n_checker.py frontend
PASSED
```

## Garantías cubiertas

| Garantía | Evidencia |
| --- | --- |
| Correlación tenant/run y aislamiento de lectura | `backend/tests/test_phase2_api.py` |
| FK compuesta que impide mezclar organización y run | `backend/tests/test_evidence_immutability.py` |
| PoC inmutable y prohibición de delete/truncate | `backend/tests/test_evidence_immutability.py` |
| Normalización y errores tipados del parser | `backend/tests/test_strix_parser.py` |
| Configuración Celery JSON, Redis separado y límites | `backend/tests/test_celery_app.py` |
| Validación SSRF básica del target | `backend/tests/test_pentest_target_validation.py` |
| Rate limiting por cuenta y contratos de reenvío | `backend/tests/test_rate_limit.py`, `backend/tests/test_auth_flow_security.py` |
| Verificación de email e invitaciones | `backend/tests/test_auth_flow_security.py` |

## Límites conocidos

- No se ejecutó un worker Celery real ni un contenedor Docker: el runner sandbox no pertenece al bloque 2.1 solicitado.
- La Fase 1 sigue abierta hasta obtener evidencia externa `nmap` de que `5433` y `6380` están cerrados o filtrados en la IP pública del VPS.
- El despliegue de las nuevas migraciones requiere revisão del rol de aplicación y de la política de RLS antes de producción.
