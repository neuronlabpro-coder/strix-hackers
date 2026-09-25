# Evidencia TDD — Fase 4 — Bloque 4.3 y cierre

## Alcance

Triaje de vulnerabilidades (`PATCH` con R4 y rastro de auditoría), tablero Kanban interactivo,
selector real de revisiones de PR, catálogo de Knowledge Base, checklist `Get Set Up` y cierre formal
de la Fase 4.

## RED

Cuatro archivos de pruebas escritos antes de la implementación. Los cuatro fallaron en la
recolección por módulos inexistentes, que es la forma más limpia de RED cuando el contrato completo
no existe todavía:

```
backend\tests\test_vulnerability_triage_api.py: ModuleNotFoundError: No module named 'backend.apps.audit'
backend\tests\test_repository_reviews_api.py: ModuleNotFoundError: No module named 'backend.apps.audit'
backend\tests\test_knowledge_api.py: ModuleNotFoundError: No module named 'backend.apps.knowledge'
```

## GREEN

`21 passed` en los cuatro archivos nuevos y `213 passed, 2 skipped` en la suite completa.

| Módulo | Entregable | Pruebas |
| --- | --- | --- |
| `backend/apps/audit/` | `audit_log` append-only con trigger, lectura paginada por entidad | `test_audit_log_is_append_only` |
| `backend/apps/vulnerabilities/` | `PATCH /api/v1/vulnerabilities/{id}` solo-`status` | 7 pruebas de triaje |
| `backend/apps/repositories/` | `GET /api/v1/repositories/{id}/reviews` | 3 pruebas de revisiones |
| `backend/apps/knowledge/` | Catálogo OWASP/CWE sembrado por migración | 4 pruebas de conocimiento |
| `backend/apps/onboarding/` | `GET /api/v1/onboarding/status` | 4 pruebas de onboarding |

## R4: defensa en dos capas

La inmutabilidad de las evidencias forenses se protege en dos puntos independientes, y ambos están
probados:

1. **Borde de la API.** `VulnerabilityTriageRequest` declara `extra="forbid"` y un único campo
   `status`. `test_patch_rejects_forensic_fields_with_422` envía siete mutaciones forenses
   (`title`, `poc_reproduction_raw`, `cvss_score`, `cve_id`, `affected_target`, `autofix_patch_diff`,
   `run_id`) y comprueba que las siete devuelven `422` y que ninguna altera la fila.
2. **Base de datos.** El trigger `protect_vulnerability_evidence` de la Fase 2 sigue bloqueando la
   escritura aunque alguien alcanzase la tabla por SQL directo. No se toca.

Añadir el campo a la lista de permitidos del esquema es un cambio de una línea; que la base de
datos lo empiece a permitir requiere una migración forward-only. Esa asimetría es deliberada.

## R4: el rastro de auditoría es append-only

`test_audit_log_is_append_only` comprueba las tres vías de alteración y que la fila sobrevive:

```sql
UPDATE audit_log SET to_state = 'IGNORED' WHERE id = :entry_id   -- bloqueado
DELETE FROM audit_log WHERE id = :entry_id                        -- bloqueado
TRUNCATE audit_log                                                -- bloqueado
```

Cada sentencia va en su propio savepoint. Sin ellos, la primera operación rechazada aborta la
transacción y las dos siguientes solo devolverían `current transaction is aborted`, que no demuestra
nada sobre el trigger. Es un detalle de la prueba que costó una iteración y que merece quedar
escrito: una prueba de trigger que no aísla las sentencias puede pasar sin comprobar nada.

El trigger es `FOR EACH STATEMENT`, no `FOR EACH ROW`, y cubre `UPDATE OR DELETE OR TRUNCATE` en un
solo disparador.

## Correcciones durante la implementación

1. **Pérdida de cambios en el router de repositorios.** Al añadir los imports de `PRReviewPage` y
   `PRReviewResponse` mediante una edición dirigida, el bloque de imports quedó duplicado con una
   versión desactualizada. Se reconstruyó la cabecera del archivo de forma explícita en lugar de
   encadenar parches, y se verificó que `ruff` y `pyright` quedaban limpios antes de seguir.
2. **Orden de las revisiones no determinista.** La primera versión de la prueba asumía que la
   revisión `QUEUED` aparecería primera. Las tres revisiones comparten `created_at` porque se crean
   en la misma transacción, así que el desempate cae en `id DESC` sobre UUID aleatorios. La prueba
   ahora comprueba pertenencia al conjunto y contenido, no un orden que el sistema no garantiza.
3. **Auditoría duplicada de entradas sembradas.** Las pruebas de conocimiento insertaban sus propias
   filas y chocaban con la restricción única de `reference_code` contra el catálogo ya sembrado por
   la migración. Se reescribieron para verificar el catálogo real, que es lo que el usuario ve.
4. **`S105` falso positivo.** El lint de ruff interpreta `SECRET_EXPOSURE` como una contraseña
   hardcodeada. Se silenció con un `noqa` que explica que es un nombre de categoría, no un secreto.
5. **`S608` en la prueba del trigger.** Las sentencias se construían con f-strings. Se cambiaron a
   parámetros vinculados, que además es lo correcto.
6. **`op.execute` no admite parámetros.** El siembra de `knowledge_entries` falló con
   `TypeError: execute() takes 2 positional arguments but 3 were given`. Alembic exige
   `op.get_bind()` con SQLAlchemy `text()` para consultas parametrizadas.
7. **`alembic check` con deriva.** Tras la migración, `alembic check` propuso borrar `audit_log` y
   `knowledge_entries` porque `env.py` importaba los modelos de forma explícita y los nuevos no
   estaban en la lista. Se añadieron `backend.apps.audit.models` y `backend.apps.knowledge.models`.
8. **Advertencias `react(set-state-in-effect)`.** Los cuatro componentes nuevos escribían estado
   sincrónicamente en el efecto. Se aplicó el mismo patrón derivado que en el Bloque 4.2: una clave
   que identifica la consulta resuelta, y `isLoading` como resultado del render.

## ElKanban es accesible sin puntero

El arrastre con `draggable` es la vía rápida, no la única. Cada tarjeta expone además un `<select>`
de destino con el resto de estados. Un tablero que solo se pudiera usar arrastrando dejaría fuera a
quien navega con teclado o usa lector de pantalla, y el cambio de estado es la acción central de la
vista. Los dos caminos llaman a la misma función `moveFinding`.

## Decisiones de producto que conviene revisar

- **El catálogo de conocimiento no es la base de conocimiento de MENU-MAP §7.** §7 describe reglas
  de negocio escritas por el tenant (Custom / Internal / Connected sources) con formulario de alta.
  Lo implementado es un catálogo técnico compartido de remediación OWASP/CWE, que es lo que pedía
  la descripción del bloque. Son dos cosas distintas y esta es la que se ha construido. La §7
  original sigue pendiente.
- **El checklist tiene 3 pasos, no los 6 de §1.1.** Los pasos 4 a 6 dependen de pantallas que
  siguen siendo placeholder (`/pr-reviews`, integraciones, miembros). Implementarlos como
  marca de completada sin la pantalla destino sería mentir sobre el estado del producto.
- **El repositorio de origen del hallazgo se resuelve por nombre de target.** No existe columna que
  enlace una vulnerabilidad con su repositorio, así que `ReviewPicker` compara `affected_target`
  contra `full_name` en el resumen del dashboard. Funciona mientras el motor reporte el repositorio
  como target, que es el caso de las revisiones de PR, y falla con gracia en los demás: el
  componente lo declara en pantalla en lugar de mostrar un desplegable inútil.
- **`GET /api/v1/audit-log/` es la primera lectura del rastro.** Solo admite `GET`; no existe ruta
  de escritura ni de borrado, y la tabla no los admitiría.

## Adhesión a `design-dark.md`

- Los tintes del ejemplo vulnerable y del ejemplo seguro se aplican con `color-mix` sobre
  `--color-critical` y `--color-accent`. No hay ningún `rgba` de color nuevo.
- El catálogo usa la rampa de severidad ya tokenizada para el swatch de cada ficha, igual que el
  gestor de issues.
- La barra de progreso del onboarding usa `--color-accent` y una pista `--color-background`.
- Todos los bordes nuevos son de 1px; el marco de las tarjetas de conocimiento y del checklist es
  `--color-secondary`.
- En `/issues` el acento sigue estando en una sola acción por pantalla: el conmutador de vista. Los
  selectores de triaje son neutros con borde.

## Verificación

- Backend: `213 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project
  backend/pyproject.toml` con 0 errores; `alembic check` sin drift tras `upgrade head`.
- Frontend: `typecheck` y `lint` sin errores ni advertencias; `build` con chunk principal de 456 kB
  (134 kB gzip) y ECharts de 453 kB (153 kB gzip).
- i18n: 373 claves usadas, 13 namespaces en paridad es/en, sin divergencias ni claves huérfanas.
  Auditoría de literales visibles en JSX: limpia.
- En el backend en ejecución: las cinco rutas nuevas aparecen en `/openapi.json`, la ruta de
  vulnerabilidades declara `get` y `patch`, y las cinco responden `401` sin token.
- `engines/`, `saas-boilerplate/` y `strix/` sin modificaciones.

## Límites y riesgos

- **Sin validación visual del shell autenticado.** El login automatizado en headless no es fiable en
  este entorno, de modo que el Kanban, el catálogo, el checklist y el historial de auditoría no se
  han visto en un navegador. La revisión visual sigue siendo obligatoria antes de publicar.
- **El "primer escaneo" del onboarding excluye los runs `QUICK`.** Un `QUICK` lo dispara el webhook de
  un PR, no el usuario, así que contarlo completaría el checklist sin que nadie haya lanzado nada
  desde el panel. La decisión es discutible y está aislada en `_has_full_scan`.
- **La severidad del checklist de conocimiento se muestra como literal de enum.** Se eligió así
  porque el dominio técnico de la severidad se escribe igual en ambos idiomas; los swatches ya
  comunican el color y traducir `CRITICAL` a «Crítico» en un catálogo CWE sería ruido.
- **El `mailto:` del modal Enterprise sigue siendo ficticio** y el punto 5 del bloque anterior
  sigue sin resolver.
- **Verificación de superusuario en cliente.** `StoredUser.is_superuser` viaja en `sessionStorage`;
  no es una frontera de seguridad. El backend valida en cada petición.
- **La calidad del catálogo es responsabilidad editorial.** Diez apuntes son un punto de partida, no
  cobertura. Ampliarlo es añadir filas a la tabla mediante migración, no código.
