# Revisión de seguridad Fase 1 · 2026-09-25

## Correcciones aplicadas

- El frontend ejecuta la verificación una sola vez y sale del estado `loading` en éxito o error.
- `ENVIRONMENT=staging|production` exige SMTP, HTTPS, STARTTLS y credenciales SMTP completas; el token local nunca se devuelve en ese entorno.
- El reenvío de verificación rota el token y permite recuperar cuentas cuando el primer entrega SMTP falla.
- Las invitaciones se envían por email, tienen endpoint de aceptación y el enlace frontend completa el flujo.
- `get_current_user` vuelve a exigir `email_verified` en cada petición protegida.
- Login usa buckets Redis por IP y cuenta; el contrato de rate limiting del reenvío fue separado del de verificación.
- `AuthContext` restaura `isLoading` en `finally` para que los errores no bloqueen la UI.
- STARTTLS usa `ssl.create_default_context()` para verificar certificado y hostname.

## Evidencia

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
75 passed, 1 skipped

uv run --project backend ruff check backend --config backend/pyproject.toml
All checks passed!

uv run --project backend pyright backend --project backend/pyproject.toml
0 errors, 0 warnings, 0 informations
```

## Cierre de exposición

Las capturas externas aportadas por el responsable muestran `5433` y `6380` en estado `closed`. La exposición pública R6 queda documentada en `docs/verification/fase1-infra-2026-09-25.md` y la Fase 1 puede marcarse como completada.
