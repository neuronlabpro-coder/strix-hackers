# Evidencia TDD · Fase 3 · Bloque 3.3

## Alcance

Se implementaron el flujo OAuth de GitHub y GitLab, la importación de repositorios remotos con registro automático de webhooks y la gestión multi-tenant de repositorios conectados.

Archivos nuevos: `backend/apps/repositories/oauth.py`, `router_auth.py`, `router.py`, `inventory.py`, `validation.py`, `schemas.py`, `backend/tests/test_git_oauth.py`, `backend/tests/test_repositories_api.py`, `backend/tests/test_repository_inventory.py`.

## RED

La primera ejecución falló en la recolección de pruebas por módulos inexistentes (`ModuleNotFoundError: No module named 'backend.apps.repositories.oauth'`) y, tras crear los módulos, por la ausencia de `get_repository`, `create_webhook` y `delete_webhook` en los clientes Git.

## GREEN

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
165 passed, 2 skipped

uv run --project backend ruff check backend
All checks passed!

uv run --project backend pyright --project backend/pyproject.toml
0 errors, 0 warnings, 0 informations

uv run --project backend alembic -c backend/alembic.ini current
e8a0b2c4d6e8 (head)

uv run --project backend alembic -c backend/alembic.ini check
No new upgrade operations detected.
```

## Garantías cubiertas

| Garantía | Evidencia |
| --- | --- |
| `state` firmado con HMAC-SHA256, ligado a organización, usuario y proveedor, con expiración de 10 minutos | `backend/tests/test_git_oauth.py::test_oauth_state_is_signed_bound_and_expires` |
| `state` de un solo uso: el replay del mismo callback responde `400` y un `state` manipulado no crea credenciales | `backend/tests/test_git_oauth.py` |
| Token canjeado y persistido solo como ciphertext AES-256-GCM con AAD por organización/proveedor, con expiración | `backend/tests/test_git_oauth.py::test_oauth_callback_stores_encrypted_token_and_state_is_single_use` |
| Flujo completo remoto → connect → listado → toggle de `pr_reviews_enabled` → desvinculación, con webhook remoto registrado y eliminado | `backend/tests/test_repositories_api.py::test_remote_connect_list_patch_and_delete_repository_flow` |
| Alta conservada con `webhook_registered: false` si el proveedor rechaza el hook | `backend/tests/test_repositories_api.py::test_connect_persists_repository_even_if_webhook_registration_fails` |
| Mutaciones restringidas a `ADMIN` y desvinculación bloqueada con revisiones en curso | `backend/tests/test_repositories_api.py::test_repository_management_requires_admin_role`, `::test_delete_is_blocked_while_reviews_are_in_progress` |
| Inventario remoto exige credencial conectada (`409`) y no filtra el token | `backend/tests/test_repositories_api.py::test_remote_inventory_requires_connected_credential` |
| Aislamiento multi-tenant: Alpha no lista, lee, modifica ni elimina repositorios de Beta | `backend/tests/test_repositories_api.py::test_repositories_are_strictly_isolated_between_tenants` |
| Normalización del inventario y rechazo de metadatos inseguros (host fuera de allowlist, esquema no HTTPS, credenciales en URL, id no numérico) | `backend/tests/test_repository_inventory.py` |
| Validación canónica de ramas, SHAs y URLs de clonado | `backend/tests/test_repository_inventory.py`, `backend/apps/repositories/validation.py` |
| Clientes GitHub/GitLab: metadatos por id, alta y baja de webhook con secretos de 32 bytes | `backend/tests/test_git_clients.py` |
| Configuración: HTTPS obligatorio en staging/producción, credenciales OAuth emparejadas y eventos de webhook saneados | `backend/tests/test_config.py` |

## Decisiones de seguridad aplicadas

- El `state` nunca aparece como clave Redis: se indexa por `SHA-256(state)`, de modo que un `KEYS *` o un volcado no expone el nonce de un flujo en curso.
- El callback consume el `state` con `GETDEL` antes de validar la membresía, por lo que un reintento del navegador o un atacante que capture la URL no pueden reutilizarlo.
- La pertenencia se revalida contra PostgreSQL (`memberships`, `users` y `organizations` activas): revocar el acceso de un usuario invalida los `state` en vuelo.
- La URL de callback del proveedor se deriva de `API_PUBLIC_BASE_URL` (HTTPS obligatorio fuera de desarrollo) y el secreto del webhook se genera con `secrets.token_urlsafe(32)` y nunca sale de la base de datos.
- `POST /connect` no confía en los metadatos del cliente: los vuelve a pedir al proveedor antes de insertar, y un repositorio ya vinculado a otra organización devuelve `409` sin revelar nada más que su existencia. Las mutaciones sobre recursos ajenos responden `404`.
- Los errores del proveedor se traducen a códigos HTTP saneados; los cuerpos del proveedor, el código OAuth y los tokens no se incluyen en excepciones ni en logs.

## Límites

- No se validó E2E contra GitHub/GitLab reales: las pruebas de proveedor usan `httpx.MockTransport` y el canje OAuth está simulado. Queda comprobar scopes reales (`admin:repo_hook`, `api`), la creación del webhook en repositorios de fork y la renovación con `refresh_token`.
- Bitbucket y Gitea no tienen adaptador: `build_organization_client` responde `501` para esos proveedores y el flujo OAuth solo acepta GitHub y GitLab.
- El estado OAuth en Redis es de vida corta (10 minutos). Un reinicio de Redis invalida los flujos en curso, lo que es aceptable porque el usuario puede repetir la autorización; no se requiere persistencia del estado.
- La pantalla de onboarding de repositorios del panel (MENU-MAP §6.1) sigue pendiente y corresponde a la Fase 4; este bloque entrega el contrato de API y la redirección a `/repositories?connected={PROVIDER}`.
- La unicidad global `(provider, remote_repo_id)` impide que dos organizaciones conecten el mismo repositorio. Es deliberado: evita credenciales y secretos HMAC duplicados, pero exige confirmar el modelo de-sharing con el responsable de producto antes de un despliegue multi-cliente.
- `DELETE /repositories/{id}` borra en cascada las revisiones históricas de PR del repositorio; las vulnerabilidades y su PoC (R4) no se ven afectadas porque viven en `vulnerabilities`.
- El `code` y el `state` del callback viajan en la query string por diseño del *Authorization Code Flow*. El backend no los registra, pero el *access log* de Uvicorn y el proxy de entrada sí podrían hacerlo: hay que excluir la query de `/api/v1/repositories/oauth/*/callback` en staging y producción.
