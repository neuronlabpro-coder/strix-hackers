# MEMORY — Mind Guard Fenix Team

> Estado al cerrar la sesión del **5 de octubre de 2026**. Lee esto primero en una sesión nueva.
>
> **Regla de lectura de este fichero**: lo que está bajo «HECHO» está aplicado **y** verificado
> con una prueba o una medición. Lo que está bajo «PENDIENTE» **no** está arreglado. Si algo
> aparece en «PENDIENTE», nadie lo ha tocado todavía, por mucho que el código parezca listo.

## Cómo trabajar aquí

- **Idioma: todo en español.** Comentarios y docstrings con tildes. Hay una **contradicción
  abierta** sobre si los valores de los JSON de traducción llevan tildes: ver
  «Contradicción abierta: los JSON de traducción», más abajo. **No la resuelvas por tu cuenta.**
- **Nada commiteado ni push.** El humano hace los commits.
- **Nada se borra sin permiso del humano.** Si algo sobra, se deja con un comentario explicando
  por qué. Hay ficheros de andamiaje commiteados en la raíz que **no** se borran: ver
  «Andamiaje en la raíz» más abajo.
- **Reglas del proyecto en `AGENTS.md`**: R1 nada hardcodeado + i18n estricto, R2 `engines/` solo
  lectura, R3 aislamiento multi-tenant, R4 inmutabilidad, R5 cero datos, R6 datos remotos solo por
  Tailscale.
- **Agentes y reglas del proyecto en `.agents/`** (react-reviewer, typescript-reviewer,
  fastapi-reviewer, database-reviewer, silent-failure-hunter, y `.agents/rules/{react,typescript,
  python,web}/*`). Los subagentes no los invocan por nombre: hay que decirles que los **lean**.
- Skills en `.opencode/skills/` (espejo de `.agents/skills/`).

---

## Contradicción abierta: los JSON de traducción

**Hay dos reglas en el repositorio que se contradicen, y la contradicción sigue viva. Está aquí
para que el fichero se la pueda enseñar al humano, no para resolverla.**

| Lo que dice una cosa | Lo que dice el fichero |
| :--- | :--- |
| La cabecera de este mismo `MEMORY.md` (y la instrucción de sesión de siempre): **los valores de los JSON van SIN tildes** | `frontend/src/locales/es/pentests.json` tiene **62 caracteres acentuados** repartidos en **36 valores** |

Medidas reproducibles (contando `ord(c) > 127` sobre el fichero entero):

| Fichero | Acentos en `HEAD` (`03199fe`) | Acentos ahora | **Delta de este turno** |
| :--- | ---: | ---: | ---: |
| `es/pentests.json` | 28 | **62** | **+34** |
| `es/prReviews.json` | 10 | 17 | +7 |
| `es/repositories.json` | 41 | 46 | +5 |
| `es/supplyChain.json` | 12 | 14 | +2 |
| `en/pentests.json` | 0 | 0 | 0 |

Tres hechos que hay que tener delante antes de tocar nada:

- **La regla ya estaba incumplida antes.** En `HEAD`, `es/pentests.json` ya tenía 28 caracteres
  acentuados. **Todos** los `es/*.json` del árbol tienen acentos: `es/admin.json` 139,
  `es/agents.json` 139, `es/billing.json` 67, `es/support.json` 66… No es un fichero raro, es la
  norma de facto del árbol.
- **El trabajo de este turno la amplía en 48 caracteres**, casi todos en los bloques nuevos de
  diagnóstico: `"Por qué falló"`, `"Diagnóstico"`, `"Código"`, `"El worker no pudo conectarse al
  demonio Docker del servidor…"`, `"El escaneo superó el tiempo máximo configurado."`. Es decir:
  **los valores nuevos de esta sesión sí llevan tildes**, en contra de lo que decía la
  instrucción de turno.
- **Los `en/*.json` no tienen acentos**, pero tampoco están limpios: sus caracteres no-ASCII son
  puntuación tipográfica (comillas curvas `U+201C/201D`, puntos suspensivos `U+2026`, raya `U+2014`,
  guillemets `U+00AB/BB`). Si la regla es «sin caracteres especiales en los JSON», también está
  incumplida en inglés y por otra razón.

**Lo que hay que decidir y lo que no se ha decidido:** si la regla es «español con tildes» (y lo
que hay que arreglar es la cabecera de este fichero), o si es «ASCII puro en los valores» (y lo
que hay que arreglar son 48 caracteres de este turno más los ~800 que ya había). Es una decisión
del humano y **aquí no se toca**. Lo único que se ha hecho hoy es dejar la contradicción escrita
con las dos cifras, para que no se pierda.

---

## Verificación

### El comprobador global

```powershell
$env:DB_CONNECT_TIMEOUT_SECONDS = "60"
uv run --project backend python scripts/ci_check.py     # 10 gates, ~22 min. Debe dar 10/10.
```

Los diez gates, por clave (`GATES` en `scripts/ci_check.py:120`):

| Clave | Qué cubre |
| :--- | :--- |
| `pytest` | Backend entero |
| `ruff` | Lint del backend |
| `pyright` | Tipos del backend. **`--project backend/pyproject.toml` es obligatorio**: sin él da «0 errores» sin aplicar las reglas estrictas |
| `alembic` | Deriva de migraciones (`alembic check`) |
| `typecheck` | `tsc` del frontend |
| `lint` | ESLint del frontend |
| `build` | Build de producción del frontend |
| `vitest` | Tests del frontend |
| `i18n` | Paridad de literales es/en |
| `paridad` | Paridad de paridad de motor Strix |

`ci_check.py` acepta `--only` y `--skip` con las claves de arriba, y avisa si se le pide una clave
que no existe o si la selección se queda vacía (un resumen de «todo en verde» con **cero** filas es
peor que un error).

### Lo que **NO** está en `ci_check.py`

- **`frontend/_i18n_dinamico_check.py`** — **sale en rojo, y no es un gate.** Escribes con
  `_i18n_dinamico_check.py`, no `scripts/_i18n_dinamico_check.py`: vive en `frontend/`. Estado
  medido: exit 1, con **30 prefijos** construidos en runtime y **5 ausentes en es y en** —
  `create.types`, `infrastructure.destinations`, `issues.destinations`,
  `notifications.destinations`, `scopes.actions`. Preexistente, confirmado con `stash`. Una clave
  armada con `${}` no la ve ningún verificador de literales, que es justo lo que este script
  comprueba. **Mientras no esté en `ci_check.py`, esto no puede volver a romperse sin que nadie se
  entere.**

### Comprobadores rápidos del frontend (desde `frontend/`)

```
python _css_brace_check.py        # llaves CSS cuadradas
python _css_comment_check.py      # comentarios CSS sin abrir
python _css_class_check.py        # toda clase usada existe en el CSS
python _i18n_check.py             # claves literales (solo las 9 pantallas de su lista)
python _que_claves_faltan.py <rutas.tsx>   # claves de cualquier fichero, por namespace real
python _det_bloque.py             # cabeceras de comentario "## Por qué" sin párrafo -> debe dar 0
```

### Scripts de medición

En `scripts/`, con prefijo `_`. **Los que miden algo reutilizable se quedan ahí; los que solo
servían para una medición de un turno van a `%TEMP%` y no se dejan en el repositorio.**

| Script | Qué mide |
| :--- | :--- |
| `scripts/_capturas_console.py` | 40 capturas; recorre pestañas. **MÍRALAS**, no basta con que corra. |
| `scripts/_auditar_pegados.py` | 30 pantallas + 9 pestañas. Debe dar cero pegados. |
| `scripts/_capturar_escaneo.py` | Entra por la lista a un detalle y mide la cronología |
| `scripts/_informe_tabla.py <ruta> <selector>` | Celdas, desbordes, fondos |
| **`scripts/_medir_indice_agent_jobs.py`** | Si el índice compuesto de `agent_jobs` **cambia el plan** y baja los buffers |
| **`scripts/_medir_indice_endpoints.py`** | Lo mismo para el índice de orden de `webhook_endpoints` (**el que se aplicó y se retiró**) |
| **`scripts/_medir_indice_desempate.py`** | Coste de los desempates por `id` sobre los listados reales |
| `frontend/_alinear_filtros.py <ruta> ".filter-bar"` | Compara **CENTROS**, no bordes superiores |
| `frontend/_medir_ancho.py <ruta> <selector> "<texto>"` | Texto partido o recortado |

### Pruebas nuevas de este turno, y qué cubre cada una

Once ficheros de pruebas nuevos. Los números de la tabla son **funciones de prueba**, no casos: dos
de ellas van parametrizadas y rinden más de un caso. Un test que se añadió hoy y no se ha mutado no
demuestra nada: **qué se cambió a propósito se lee en el `git diff`**, porque los ficheros son
nuevos y no hay historial que consultar.

| Fichero | Qué fija |
| :--- | :--- |
| `backend/tests/test_runner_diagnostico.py` (12) | Que el motivo real llega a través del `SandboxExecutionError` del runner, que `PermissionError` del socket tiene código propio, que la cadena de causas circular no cuelga, y que el motivo **más profundo** gana al envoltorio |
| `backend/tests/test_pentest_readiness_api.py` (9) | Que readiness **no consulta al demonio Docker**, que una comprobación no bloqueante no impide escanear, y que **ningún texto de `readiness.reasons.*` afirma causalidad** |
| `backend/tests/test_reembolso_por_fallo_de_despliegue.py` (12) | Que se devuelve solo por motivo de despliegue, que devolver dos veces no mueve el saldo dos veces, y que el filtro de organización impide devolver el saldo de otro tenant |
| `backend/tests/test_pr_review_cobro_y_comentario.py` (9) | Que un reintento **no** cobra un segundo escaneo, que un relanzamiento del panel **sí** cobra, y que un comentario que GitHub rechaza **no** tumba el escaneo |
| `backend/tests/test_orden_paginado.py` (12) | Que las listas paginadas no solapan páginas, con dos peticiones seguidas |
| `backend/tests/test_orden_nulos.py` (4) | El caso `NULLS FIRST` sobre una columna entera de nulos, **afirmando el contrato** y no la estabilidad |
| `backend/tests/test_revisiones_colgadas.py` (6) | Que el vigilante cierra en `ERROR` la revisión con `run_id IS NULL` y **no** la reencola |
| `backend/tests/test_pr_review_launch_api.py` (7) | Que si la cola falla la revisión no queda en `QUEUED`, y que un tenant ajeno no puede lanzarla |
| `backend/tests/test_pr_pipeline_estado_y_diagnostico.py` (3) | Que un badge que el proveedor rechaza no impide escanear, y que el pipeline **no** escribe un literal de motivo |
| `backend/tests/test_riesgo_vivo.py` (3) | Que el criterio de recuento es el del enum y no una lista nueva, y que la ficha y el anillo dicen lo mismo |
| `backend/tests/test_audit_marca_clasificador.py` (11) | Que `_i18n_dinamico_check.py` **sigue falling**: el propio clasificador no está eximido de sus propias reglas |

---

## Entorno

- **Login demo**: `demo.admin@acmesecurity.io` / `DemoFenix2026!Empa`. Org
  `8aef9544-3fe6-5fd9-890a-4c7e948a25e3`.
- Docker 29.8.0 **funciona** por el contexto `default` (`npipe:////./pipe/docker_engine`).
  Antes fallaba por `desktop-linux`; ya no.
- Para levantar: backend `$env:DB_CONNECT_TIMEOUT_SECONDS="60"; uv run --project backend python
  -m uvicorn backend.main:app --host 127.0.0.1 --port 8000`, y `npm run dev` en `frontend/`.
  **Los procesos en background mueren al cerrar el turno**: hay que relanzarlos y comprobar.
- **`celery` no tiene `--reload`** (no es un servidor de desarrollo). Cualquier cambio en
  `backend/` —también dentro de una tarea— obliga a **reiniciar el uvicorn**, o el panel sirve
  código viejo y parece que el arreglo no funciona.
- La base de demostración está **sucia**: le quedan ~7.700 organizaciones de pruebas antiguas
  (`victima-*`, `Stripe E2E *`, `UI *`…). No es un defecto del código, pero ensucia cualquier
  métrica o listing que se mire «a pelo» sobre la base.
- Respaldo del trabajo en `%TEMP%\fenix_respaldo_20261002_135715`.

### El worker de Celery no corre en local, y eso cambia cómo se depura

**El contenedor de `celery_worker` vive en el VPS de Dokploy.** El `.env` local y el Docker local
**no tienen nada que ver** con lo que el worker ve, y esta es la confusión que costó horas:

- El traceback del VPS llega con números de línea que **coinciden con `git show HEAD`**
  (`03199fe`), no con el árbol de trabajo. `tasks.py` 977 en el contenedor frente a **1328** en el
  árbol; `sandbox.py` 81 frente a **479**. Lo que se lee en el contenedor es el código de hace
  varios commits.
- **`backend/workers/runner/diagnostico.py` y `backend/workers/runner/disponibilidad.py` están
  sin seguimiento de git**, así que el contenedor **no puede tenerlos**. Cualquier cosa que se
  lea del worker y dependa de esos ficheros es, por construcción, código viejo.
- Por eso un pentest puede mostrar `STRIX_EXECUTION_FAILED` **sin que el clasificador esté
  desplegado**: el código que etiqueta el motivo no está en el contenedor. El código genérico es
  el síntoma de «no hay diagnóstico», no el de «el diagnóstico no supo qué pasó».

**Antes de sacar conclusiones de un error del worker, compara los números de línea del traceback
con `git show HEAD:<fichero>`.**

---

# HECHO — aplicado y verificado

## Diagnóstico de fallos del sandbox

**Qué problema resuelve**: un `PermissionError` del socket de Docker quedaba enterrado dentro de
un `SandboxExecutionError` genérico. El panel veía `STRIX_EXECUTION_FAILED` y el operador no
tenía ni una línea que buscar.

| Fichero | Qué es |
| :--- | :--- |
| `backend/workers/runner/diagnostico.py` (257 líneas, **sin seguimiento de git**) | Clasificador de fallos. Siete códigos estables más el genérico |
| `backend/workers/runner/disponibilidad.py` (363 líneas, **sin seguimiento de git**) | Las cuatro comprobaciones previas al despliegue |
| `GET /api/v1/pentests/readiness` (`pentests/router.py:337`) | El endpoint que las expone |
| `frontend/src/features/pentests/SandboxReadinessCard.tsx` | La tarjeta del panel |
| `backend/tests/test_runner_diagnostico.py`, `test_pentest_readiness_api.py` | 21 pruebas |

**Los siete códigos**, en el orden en que se prueban (el orden decide cuál gana, porque las
excepciones del runner heredan unas de otras y se comparan por `isinstance`):

| Código | Comprobación a mirar |
| :--- | :--- |
| `STRIX_EGRESS_FENCE_MISSING` | `egress_fence.exigir_cerco_de_salida` |
| `STRIX_LLM_KEY_ACK_MISSING` | `llm_key_exposure.exigir_reconocimiento_de_exposicion` |
| `STRIX_IMAGE_UNAVAILABLE` | La imagen del sandbox no está en el host |
| `STRIX_WORKSPACE_UNAVAILABLE` | El host no deja preparar el workspace |
| `STRIX_OUTPUT_UNUSABLE` | Strix no dejó un `results.json` legible |
| `STRIX_TIMEOUT` | El motor superó el timeout |
| `STRIX_DOCKER_UNAVAILABLE` | El demonio no aceptó la petición |
| `STRIX_EXECUTION_FAILED` | Genérico. Lo que **no** se pudo clasificar |

**Por qué recorre la cadena de `__cause__`**: el runner **envuelve por diseño**, así que la
excepción que sale hacia arriba casi nunca es la interesante. Recorre del envoltorio a la causa más
profunda y **gana la más profunda**: si `SandboxError` y `ImageNotFound` están ambos en la cadena,
manda `ImageNotFound`. El tope de 16 es lo que garantiza que la función termina **aun con** una
cadena de causas construida a mano que se repita: la comparación de identidad solo cortaría el
ciclo si el objeto se repitiera exactamente.

**Por qué se quitaron las comprobaciones del demonio Docker de readiness**: el endpoint lo
responde el **proceso de la API**, que en Dokploy **no es** el del worker. Preguntar ahí si el
demonio responde daría **un verde falso**, que es la peor respuesta posible de un diagnóstico: te
dice que el escaneo va a funcionar cuando va a fallar por permisos. Para el demonio y para la
imagen, **la autoridad es el código del escaneo ya fallido**. El gate de superficie
`test_runner_docker_surface.py` fue lo que impidió colar la comprobación.

## La tarjeta de readiness: separar bloqueante de no bloqueante

`readiness` hace **cuatro** comprobaciones, y la distinción que importa es si **bloquean**:

| Comprobación | Motivo si falla | ¿Bloquea? |
| :--- | :--- | :--- |
| Workspace | `STRIX_WORKSPACE_UNAVAILABLE` | sí |
| Cerco instalado | `STRIX_EGRESS_FENCE_MISSING` | sí |
| Cerco activo | `STRIX_EGRESS_FENCE_DISABLED` / `STRIX_EGRESS_FENCE_MISSING` | **depende** |
| Reconocimiento de la clave | `STRIX_LLM_KEY_ACK_MISSING` | sí |

**El caso raro, y el que hay que entender**: con `STRIX_REQUIRE_EGRESS_FENCE=false`,
`exigir_cerco_de_salida` **avisa y devuelve**, o sea que el contenedor **se levanta igual y el
escaneo se completa gastando créditos y sin red**. Al revés que con el cerco ausente, que lo niega
y no los consume. Por eso no puede ir en rojo al lado de las que sí bloquean: quien lo viera marcado
igual que «no hay `iptables`» entendería que no puede escanear, cuando lo que no puede es **escanear
de forma segura**. Un despliegue que no trabaja y un despliegue que trabaja sin red son fallos
distintos y el panel tiene que separarlos.

**Por qué la tarjeta ya no afirma una causa que no es la causa.** Decía «los escaneos se encolan y
fallan antes de levantar el contenedor / esto es lo que hay que arreglar en el servidor», siendo
que el endpoint corre en la API y el escaneo en el worker: mandaba a arreglar un `iptables` que no
era el problema, **en un host que no era el del worker**. Misma clase de error que el diagnóstico vino
a arreglar, pero en dirección contraria. Ahora:

- El título de arranque es **«Lo que falta en el servidor que responde»**, y el texto dice que estas
  comprobaciones se ejecutan en el servidor que **está mostrando el panel**, que no siempre es el
  que lanza los escaneos.
- Cada motivo tiene **texto propio** en `readiness.reasons.*`, en es y en.
- Hay una prueba, `test_todo_motivo_del_diagnostico_tiene_texto_propio_y_sin_causalidad`, que
  comprueba que **ninguno** de esos textos contiene una fórmula de causalidad (la lista incluye
  «esto es lo que hay que arreglar»). Sin la segunda mitad, la prueba pasaría con el texto viejo.
- El reparto bloqueante / no bloqueante lo decide el backend (`bloquea_escaneo` y `bloquea` llegan
  en la respuesta), no el componente. Que el panel tenga que deducir qué comprobación bloquea es la
  misma clase de error que acabamos de corregir.

**La causa de fondo, que es la que hay que recordar**: la tarjeta reutilizaba
`run.fallos.<código>`, que está escrito **desde el punto de vista de quien ya sabe que el escaneo
falló**. Copiarlo a readiness devolvía la causalidad por la puerta de atrás.

## Análisis de seguridad de una revisión de pull request

`backend/apps/repositories/reviews.py` (397 líneas, **sin seguimiento de git**) y
`POST /api/v1/pr-reviews/{review_id}/analyze` (`repositories/router.py:1017`).

| Decisión | Por qué |
| :--- | :--- |
| **`202`, no `201`** | El recurso que se crea es el trabajo encolado, no la revisión: la revisión **ya existía** y solo cambia de estado. Un `201` haría que un cliente insertara la respuesta en su lista como una fila más |
| **El filtro de `organization_id` va dentro del servicio**, no solo en la ruta | La ruta y el servicio son dos puertas distintas al mismo dato. Filtrar solo en la ruta deja el servicio abierto a cualquier otro llamador, y el llamador que va a aparecer es el siguiente que se escriba |
| **`SELECT ... FOR UPDATE` antes de encolar** | Para que un doble clic no produzca dos contenedores. Un `asyncio.Lock` no serviría: la unidad de ejecución no es el proceso |
| **Si la cola falla, la revisión queda en `ERROR` con `503`, no en `QUEUED`** | `QUEUED` sin nada en la cola es una promesa que el sistema no puede cumplir y que el panel pintaría como «en curso» indefinidamente. Es un `503` con `Retry-After`, el mismo trato que recibe un pentest que no se pudo encolar |
| **`AdminRequired`, no permiso de API token** | El pipeline abre un cliente Git con la **credencial del workspace** y consume tokens de la plataforma. Quien lo dispara es una persona sentada en el panel, no una integración |
| **`404` y no `403`** cuando no se puede lanzar | Hay tres motivos distintos —no existe, está en curso, el repositorio está desconectado— y `403` confirmaría que el `review_id` existe en algún sitio, que es lo contrario de lo que R3 permite |

**Nunca se ha ejecutado de extremo a extremo.** Ver «El bloqueo del despliegue».

## La ruta del dinero

**Es lo más delicado que se ha tocado.** Tres funciones, todas en `backend/workers/tasks.py`:

| Función | Qué hace |
| :--- | :--- |
| `_saldo_retenido` (`:661`) | Cuánto se ha quedado la plataforma de este run. Filtra **por organización en el `WHERE`** |
| `_devolver_lo_retenido` (`:709`) | Asienta la devolución, **como mucho una vez** |
| `_devolver_si_el_fallo_fue_de_despliegue` (`:844`) | Decide si este motivo concreto reembolsa |

**El defecto que había, y lo que costaba**: la llamada al reembolso estaba **después** del bucle de
modelos, así que cuando `manager.run` lanzaba, no se ejecutaba. **Se cobraban escaneos que nunca
corrieron.** Medido en la base: tres cobros de **−10, −3 y −3** créditos y **cero** asientos de
devolución.

**El arreglo, y por qué cada parte es como es:**

| Parte | Por qué |
| :--- | :--- |
| **La política sale de `diagnostico.py`**, no de una lista local | `MOTIVOS_DE_DESPLIEGUE` es la autoridad sobre qué motivos dependen de la máquina del worker. Reimplementar la lista aquí serían **dos** verdades que divergen: alguien añade un motivo al diagnóstico, olvida esta función, y un fallo recién clasificado como «arreglarlo en el host» vuelve a costar créditos |
| **El importe sale del `SUM(amount_delta)` del ledger**, no de `scan_credit_cost()` | Lo que hay que devolver es **lo que se cobró**, no lo que la tabla de precios dice hoy. Con precios editables, un cambio entre el encolado y el fallo **regalaba** la diferencia si el precio había subido, y **cobraba** al tenant la parte que él no pagó si había bajado. El ledger es el único sitio donde el importe cobrado está sellado |
| **La suma cubre las tres referencias del run**, no solo la reserva | El mismo identificador puede tener el asiento de consumo (`{id}`), una devolución anterior (`{id}:refund`) y el ajuste contra el consumo real (`{id}:usage`). Sumar solo la reserva daría un número que deja de ser verdad en cuanto el worker toca cualquiera de las otras dos |
| **Idempotente por aritmética, no por un flag** | El propio asiento de devolución entra en la suma del pendiente, así que **la segunda llamada lee cero y no escribe**. Un flag en memoria no sería idempotente entre procesos, y un `UPDATE` sobre el asiento anterior está prohibido: `credit_ledger` es *append-only* (R4) |
| **`FOR UPDATE` sobre la fila del run**, antes de leer el saldo | Hace que dos workers concurrentes se vean: el segundo espera a que el primero confirme y entonces lee el asiento nuevo |
| **Se llama DESPUÉS de `_mark_failed`** | `_devolver_lo_retenido` bloquea la fila del run igual que `_mark_failed`. Marcando primero, la guarda de `COMPLETED` ve el estado **final** del run; si el reembolso fuera antes, confirmaría con el run todavía en `RUNNING` |
| **Nunca propaga el fallo** | El run ya está en estado terminal. Se registra la excepción y se sigue: un reembolso **perdido** es un saldo que alguien tiene que reconciliar a mano; un reembolso **duplicado** es un saldo inflado que además es un regalo. El orden no es simétrico, así que el segundo caso tiene que ser **imposible por construcción** y el primero solo probable y registrado |
| **`organization_id` va por parámetro y `None` no se toca el saldo de nadie** | Sin organización no hay a quién devolverle el dinero, y adivinarlo sería acreditar al tenant equivocado. Es R3 aplicado al dinero: la duda se registra, no se resuelve suponiendo |
| **`rollback` va sobre la sesión y no sobre el motor** | `AsyncEngine` no tiene ese método, y llamarlo deja el error real del reembolso enterrado bajo un `AttributeError`. Ya se cometió una vez aquí |

### Reintentos de revisión de PR: cuatro escaneos cobrados por una revisión

`MAX_INTENTOS_GIT = 3` con `max_retries=3` significa **cuatro ejecuciones**, y
`_claim_review(retry_failed=True)` construía un `PentestRun` **nuevo** con su propio cobro. Como
el camino real del reintento es un **`429` de GitHub**, que es *más* probable cuando la plataforma
acaba de lanzar varios escaneos a la vez, el fallo de infraestructura más frecuente se convertía en
la fuente de ingresos menos discreta del producto.

| Arreglo | Por qué |
| :--- | :--- |
| **Un reintento crea su run pero no vuelve a cobrar** | Un reintento de Celery **no es un encargo nuevo**: es la misma tarea con el mismo argumento, que vuelve a ejecutarse porque el proveedor devolvió un `429` o un `5xx`. Lo que cambió entre intento e intento es el humor de GitHub, no lo que pidió el cliente |
| **La condición es `es_reintento and debe`, no `if debe`** | Con `if debe`, el arreglo sería «no cobrar nunca dos veces» y **el botón de reanalizar del panel pasaría a ser gratis** sin que nadie lo hubiera decidido |
| **Se pregunta al ledger, no al flag `retry_failed`** | El flag dice «esto viene de un reintento»; el ledger dice «esto ya se pagó», y lo segundo es lo que decide. Si mañana alguien añadiera un camino reintentable **antes** del cobro, con el flag el reintento saldría gratis. Con el ledger ese caso se cobra porque no debe nada, y el defecto no puede depender de un orden de llamadas que nadie escribió para eso |
| **`reference_id` = `{run_id}:pr:{review_id}`, con el `run_id` DELANTE** | Con `run_id = None` desaparecía **el único enlace con el intento anterior**, y sin ese enlace la pregunta al ledger no tiene respuesta. Y el `run_id` va primero porque `_saldo_retenido` busca con `LIKE '{run_id}%'`: si el `run_id` no encabeza la referencia, deja de encontrar el cobro |
| **El relanzamiento del panel SÍ cobra**, aunque la revisión ya estuviera pagada | Es un encargo **nuevo**. Eso no estaba escrito en ninguna parte y ahora sí |

**Pendiente sin resolver**: `scan_credit_cost(ScanModeEnum.QUICK)` en
`pipeline.py:506` se llama **sin `organization_id`**, así que **los precios negociados por tenant no
se aplican**. `pentests/service.py:321` (`queue_pentest`) hace **exactamente lo mismo**, así que
arreglarlo solo en el pipeline de PR crearía dos precios para la misma operación. Está en
«PENDIENTE».

## Publicar en GitHub no puede tumbar un escaneo

**El defecto**: un `404` o un `403` al poner el badge o el comentario **abortaba el pipeline entero
con el run ya en `COMPLETED`**. El escaneo funcionaba y se perdía igual.

| Arreglo | Por qué |
| :--- | :--- |
| **La publicación es un resultado, no una excepción** | `PublicacionDeComentario` (`pipeline.py:107`) es un `NamedTuple` con tres respuestas y no una excepción. Hacen falta **dos** preguntas distintas —«¿se publicó?» y «¿qué se escribió?»— y se responden con datos distintos |
| **`logger.warning` con revisión, repositorio, commit, estado, TIPO de excepción y CÓDIGO HTTP** | **El mensaje de la excepción no**, que puede no estar saneado y puede traer lo que el proveedor quiera. El tipo y el código bastan para buscar |
| **El reintento del comentario es una tarea propia**: `repositories.publish_pr_review_comment` | Y **no puede repetir el escaneo**, porque en esa tarea no existe código que pueda escanear. Separar el reintento del trabajo que lo Soudió es lo que hace imposible el peor resultado |
| **Entrega al menos una vez** | Si GitHub guarda el comentario y se pierde la respuesta, hay **duplicado**. Es la contrapartida aceptada de no perder nunca un escaneo |
| **El panel no distingue «no llevaba comentario» de «no se pudo publicar»** | Limitación conocida y **no resuelta**: el estado es el mismo en los dos casos |

## La revisión atrapada desde el 27 de septiembre

Una revisión de PR con `run_id IS NULL` desde el 27 de septiembre. Alcanzable porque la FK es
`ON DELETE SET NULL`, y el `INNER JOIN` de la segunda rama del watchdog hacía que la fila
**desapareciera del resultado**: no salía como `ERROR`, ni como cero, **no salía**. Una revisión en
`SCANNING` que nadie reclama y que ningún vigilante ve es la peor clase de fallo: no se nota
porque no aparece.

**El arreglo**: `reconciliar_revisiones_huerfanas` (`reviews.py:108`), en
`repositories.watchdog_orphaned_reviews`, registrado en `backend/workers/celery_app.py:58` con **el
mismo periodo del Beat que el watchdog de runs**.

| Decisión | Por qué |
| :--- | :--- |
| **Mira solo `run_id IS NULL`** | Es la definición exacta de «hubo un `SCANNING` y el run ya no existe». Incluir más cosas sería volver a adivinar |
| **Va a `ERROR`, NO reencola** | Reencolar **vuelve a pasar por `_claim_review`, que cobra**. Una revisión atrapada hace semanas no puede costar un escaneo nuevo por la vía de la reparación |
| **`ERROR` además es relanzable** | Y eso cierra el agujero: el operador la ve, y el botón cobra, que es lo correcto porque **sí** es un encargo nuevo |

## Criterio de recuento de hallazgos: riesgo vivo, un solo sitio

**El criterio ya estaba decidido y dos pantallas no lo leían.** `ROADMAP.md:395` dice que
`REMEDIATION_PROPOSED` **sigue contando como abierto** («un PR sin fusionar no arregla nada»), y el
enum lo declara con `IssueStatusEnum.is_open_for_closure`. La herramienta MCP ya lo leía.

| Antes | Ahora |
| :--- | :--- |
| `dashboard/service.py` tenía su propia lista | Exporta `ESTADOS_RIESGO_VIVO`, derivado del enum (`dashboard/service.py:71`) |
| `pentests/router.py` tenía la suya | Lo **importa** (`pentests/router.py:15`) |

**Un criterio, un sitio**: si sale del enum, añadir un estado nuevo no puede hacer que tres
pantañas discrepen.

**Lo que cambió al arreglarlo, y hay que decirlo porque es el dato**: la ficha pasó de **3 a 2**
hallazgos y el `security_score` de **96 a 92**. Las cifras **antes** eran las equivocadas.

**El hallazgo de fondo**: `dashboard.json` decía «hallazgos abiertos» y el filtro **excluía
`REMEDIATION_PROPOSED`**. **La etiqueta mentía, no la cifra.** Y `test_vulnerability_breakdowns.py`
**afirmaba el defecto**: `assert por_target["total"] == 3` con un hallazgo `FIXED` dentro
(`test_vulnerability_breakdowns.py:289`), y su comentario solo mencionaba el `IGNORED`. Una prueba
que fija el número equivocado no es una prueba que vigila: es una que impide ver el defecto.

**La tabla de Issues NO se tocó**, y está bien: ya declara su alcance («con el filtro actual»). El
problema nunca fue que hubiera dos cifras, es que **dos de ellas no decían la suya**.

## Ordenaciones sin desempate único

Una ordenación por una columna `now()` **sin** un segundo criterio único hace que dos consultas
iguales puedan devolver las páginas en distinto orden, y las páginas se solapan.

### El principio, y por qué el desempate va donde va

- **El desempate va sobre la tabla que se pagina**, no sobre la `aliased` de un `JOIN`. Los tres
  `JOIN` con `organizations` que aparecen en estas consultas son de uno a uno, así que la clave
  primaria de la tabla `aliased` también sería única — pero pedir la unicidad de un `JOIN` es
  depender de algo que nadie escribió. La clave primaria de la tabla principal es una garantía.
- **`id` desempata porque es único**, y no cambia **qué** filas se devuelven: solo el orden entre
  las que ya se devolvían. No toca el `WHERE`, así que **R3 sigue intacto**.
- **`id` también arregla un `NULLS FIRST` sobre una columna entera de nulos** (medido). `NULLS
  FIRST` no es un empate: es una regla que coloca **todos** los nulos delante. Dentro de ese grupo,
  `id` desempata.

### Catálogo verificado — los catorce sitios corregidos en este turno

Todas las líneas están comprobadas contra el árbol de trabajo de este momento.

| Fichero:línea | Función | Desempate añadido | ¿Pagina? |
| :--- | :--- | :--- | :--- |
| `backend/apps/agents/router.py:214` | `list_agents` (tenant) | `ScannerAgent.id.desc()` | **sí** |
| `backend/apps/admin/router.py:127` | `list_admin_agents` (consola) | `ScannerAgent.id.desc()` | **sí** |
| `backend/apps/admin/operations_router.py:357` | `list_admin_scans` | `PentestRun.id.asc()` sobre `started_at ASC NULLS FIRST` | **sí** |
| `backend/apps/admin/operations_router.py:675` | `list_admin_containers` | `PentestRun.id.asc()` sobre `started_at ASC NULLS FIRST` | **sí** |
| `backend/apps/admin/operations_router.py:766` | `list_admin_reviews` | `PullRequestReview.id.desc()` | **sí** |
| `backend/apps/admin/operations_router.py:850` | `list_admin_jobs` | `AgentJob.id.desc()` | **sí** |
| `backend/apps/agents/service.py:896` | `listar_trabajos` | `AgentJob.id.desc()` | **sí** |
| `backend/apps/agents/service.py:1036` | `resumen_agente` | `AgentJob.id.desc()` | no (`limit` sin `offset`) |
| `backend/apps/api_access/service.py:116` | `list_api_tokens` | `ApiToken.id.desc()` | no |
| `backend/apps/webhooks/router.py:216` | `list_webhooks` | `WebhookEndpoint.id.desc()` | no |
| `backend/apps/webhooks/router.py:434` | `list_deliveries` | `WebhookDelivery.id.desc()` detrás de `created_at, attempt` | **sí** |
| `backend/apps/knowledge/retrieval.py:253-254` | `recuperar_documentos` | **DOS** desempates, ver abajo | corte `top_k * 3` |
| `backend/apps/repositories/webhook_events.py:160` | `_upsert_review` | `PullRequestReview.id.desc()` | `limit(1)` |
| `backend/apps/vulnerabilities/remediation.py:304` | `_review_id_del_hallazgo` | `PullRequestReview.id.desc()` | `limit(1)` |

**De las once que el catálogo anterior daba por sin desempate, solo cinco tenían el defecto de
solapamiento de páginas**, y son estas: `agents/router.py:214` (`list_agents`),
`admin/router.py:127` (`list_admin_agents`), `operations_router.py:675` (`list_admin_containers`),
`operations_router.py:766` (`list_admin_reviews`) y `agents/service.py:896` (`listar_trabajos`).

Las otras cuatro no se paginan y su daño es otro —recargar y ver filas intercambiadas, o leer el
mismo contexto distinto—: `api_access/service.py:116`, `webhooks/router.py:216`,
`knowledge/retrieval.py:253` y `agents/service.py:1036`. Y dos más (`webhook_events.py:160`,
`remediation.py:304`) son búsquedas de `limit(1)`, donde lo que importa es elegir **la** fila y no
otra.

**Y tres sitios del catálogo corregido no estaban en ninguna de las dos listas**:
`operations_router.py:357` (`list_admin_scans`), `operations_router.py:850` (`list_admin_jobs`) y
`webhooks/router.py:434` (`list_deliveries`). Los tres se corrigieron y no figureaban como
pendientes en el catálogo anterior.

### `knowledge/retrieval.py` es el caso raro: hacen falta dos desempates

**La ordenación que ve el usuario no es la de SQL.** La puntuación se calcula en Python, con
`_puntuar`. Poner `id` al final del `ORDER BY` de la consulta **no arregla nada de lo que el usuario
nota**, porque ese `ORDER BY` solo decide qué `top_k * 3` documentos entran como candidatos; el orden
final lo pone el `sort` de más abajo. **Un solo desempate en SQL deja el problema entero donde se
ve.**

- **SQL** (`retrieval.py:253-254`): para el corte de candidatos estable. Sin él, la misma pregunta
  recibe un contexto distinto y el usuario lo lee como que el modelo ha cambiado de opinión entre
  un mensaje y el siguiente.
- **Python** (`retrieval.py:279`): `key=lambda trio: (-trio[1], trio[0].title, trio[0].id)`. **`title`
  primero** porque es lo que lee una persona —dos documentos con la misma puntuación se ordenan por
  su nombre, que es lo esperable— y **`id` detrás** porque es lo único que cierra el orden y decide
  el `[:top_k]`.

**El comentario del código afirmaba que el desempate por título hacía la consulta estable, y es
falso.** El título no es único: nada impide que dos documentos se llamen igual, y mientras ese caso
exista la lista sigue sin estar del todo ordenada. El comentario se ha corregido.

### Una prueba genérica de estabilidad NO cubre el `NULLS FIRST`

`test_orden_paginado.py` comprueba que la página 1 no cambia al reescribir una fila. Para el caso de
una columna entera de nulos eso **pasa igual con el defecto puesto**: dos lecturas seguidas de la
misma consulta dan el mismo orden aunque ese orden sea arbitrario, porque el planificador no cambia
de plan entre una y otra.

Por eso `test_orden_nulos.py` está en un fichero aparte y **afirma el contrato**: que el orden sea
`(started_at ASC NULLS FIRST, id ASC)`. Para eso los `id` se fuerzan a `uuid.UUID(int=n)` y las filas
se insertan en orden creciente: con el defecto, la base devuelve las filas en orden de inserción y
la comprobación se rompe. Eso no puede fallar por casualidad.

### El índice de `webhook_endpoints`: se escribió, se aplicó, se midió y **se retiró**

Con 29.505 endpoints en 3.000 tenants, mediana de 9 ejecuciones: el plan es un **`Sort`** en los seis
casos. Con el índice compuesto el coste sale **mayor** y los buffers **suben de 18 a 21**.

**Motivo estructural**: `list_webhooks` **no pagina**, y sin `LIMIT` hay que leer todas las filas del
tenant; ordenar cientos en memoria sale más barato que leerlas en orden de índice.

> **Un índice de orden solo compensa cuando el `LIMIT` deja de leer.**

**La primera medición dio `Index Scan` con el índice y `Sort` sin él, y casi se escribe así.** No se
reprodujo al cambiar la distribución. **Un tenant de 100.000 filas con el resto en otro no es una
plataforma, es un laboratorio.** La migración se retiró; **el desempate se queda, porque medido
cuesta cero** (solo un `ORDER BY`, sin coste de I/O).

### Lo que sí merecía índice: `agent_jobs`

`ix_agent_jobs_org_status` empieza por organización pero **termina en `status`**, así que **no puede
dar el orden** `(organization_id, created_at)`. No había nada que lo cubriera.

Con 52.000 jobs: **1,305 ms → 0,079 ms**. Migración
`backend/migrations/versions/d9e0f1a2b3c4_indice_cola_de_trabajos.py`, con **`downgrade` probado de
verdad**.

**Criterio para no poner índices de más**: no «¿el índice devuelve el orden?», sino **«¿cambia el
plan y bajan los buffers?»**. `Incremental Sort` sobre un índice ya ordenado por la primera columna
es barato **a propósito**: solo ordena dentro de cada grupo de empate, y esos grupos son de una fila.

## El buscador del inventario de repositorios: dos bugs

`frontend/src/features/repositories/` y `frontend/src/lib/api.ts:331`. Diez pruebas en
`buscador.test.ts`.

| Defecto | Por qué costaba verlo |
| :--- | :--- |
| **El campo de búsqueda desaparecía al no encontrar nada**, porque estaba dentro de la rama de «hay repositorios» | El usuario perdía **el control con el que corregir lo que había escrito**. Un buscador que se borra al fallar obliga a reescribir la consulta entera |
| **El tope no eran 100 sino 10** (`VENTANA_INVENTARIO_POR_DEFECTO`) | Con diez filas, buscar el repositorio en la posición **300 de 500** no lo encontraba. El servidor filtraba bien y devolvía `total: 1`, pero la fila no cabía en la ventana. El `total` correcto y la lista vacía juntos son el peor par posible |

La prueba es estructural: mira el **texto del código**, no el comportamiento.

## El desplegable de tokens en MCP

`frontend/src/features/api_access/McpTab.tsx`. Respeta una regla que **no se podía romper**: un
token se muestra **una sola vez**, al crearse. `api_tokens` guarda `token_hash`; el valor en claro
**no existe después** — verificado en vivo: el listado devuelve 9 campos y ninguno es el valor—.

| Decisión | Por qué |
| :--- | :--- |
| El desplegable lista **nombre, prefijo, caducidad, último uso y permisos** | El problema que resuelve no es «ver el token», es **«no saber cuál pegar»**. Antes había que abrir otra pestaña, buscar el token por su nombre en una lista y copiarlo de ahí |
| La configuración referencia un token existente **por `id`** | Elegir el token es una afirmación sobre la identidad, no sobre el secreto. El marcador `<API_TOKEN>` se genera cuando el usuario no ha pegado nada |
| **El valor pegado se descarta al elegir otro token** | Si no, cambiar de token deja la configuración montada con un token que el desplegable dice que no es, y el error no se ve hasta que el cliente lo rechaza |
| **No se inventó ninguna pantalla que prometa recuperarlo** | No hay a dónde mirar |

---

# PENDIENTE

## 1. El bloqueo del despliegue — MEDIDO, SIN ARREGLAR

**Este es el punto abierto más grande del turno y sigue exactamente como estaba.** El humano dijo
que sí a arreglarlo y en este turno se ha medido otra vez. El diagnóstico y el arreglo están
escritos; **lo que falta es ejecutar el arreglo en el VPS**.

| Lo medido | Detalle |
| :--- | :--- |
| Dónde corre el worker | En un **contenedor del VPS de Dokploy**, no en local. El `.env` local y el Docker local **no tienen nada que ver** |
| Dónde falla | En **`docker.from_env()`**, con **`PermissionError(13)`** sobre `/var/run/docker.sock`, **antes de crear la red y antes de crear el contenedor** |
| Qué se ve además | **`DockerException`** al leer la versión de la API del demonio |
| Causa más probable | `DOCKER_GID` no coincide con el GID real del socket. Comprobación: `stat -c '%g' /var/run/docker.sock` **dentro del contenedor del worker**, comparado con `DOCKER_GID` |
| Dónde está escrito | `docs/deployment/dokploy.md:70-78` |

**Este es el siguiente fallo que verá quien siga**: el análisis de PR **nunca se ha ejecutado de
extremo a extremo**. El primer fallo real fue un `404` de GitHub al publicar el badge, ya
arreglado; el siguiente es el socket de Docker.

## Decisiones del humano, con sus opciones

### 2. ¿El pipeline de PR reembolsa por fallo de despliegue?

| Opción | Qué significa | A favor | En contra |
| :--- | :--- | :--- | :--- |
| **A** | Reembolsa, igual que el resto de la plataforma | Coherente con `pentests/abort_reason.py` y con `_execute_pentest_run`. Un fallo del despliegue no es culpa del cliente | Multiplica los caminos de devolución, y cada uno es un sitio donde se puede perder el dinero |
| **B** | **No reembolsa**: «se cobra una vez, se entregue o no» | Un escaneo que se lanza es un recurso que se consume; el precio es del intento, no del resultado | **Esa política no está escrita en ninguna parte.** Y con el despliegue roto, es «cobrar por escaneos que no corrieron», que es exactamente el defecto que se acaba de corregir en el worker |

**Ninguna de las dos está decidida.** El worker ya está escrito con la opción A. Si se elige B,
hay que tocarlo y hay que **documentarlo**, porque es una política de producto y no un detalle.

### 3. `SNOOZED` en el recuento de hallazgos

`SNOOZED` está **excluido** de `ESTADOS_RIESGO_VIVO` (hereda de `is_open_for_closure`). Incluirlo
**cambia el significado de `open_issues` y de `security_score`**, no solo un número. No decidido.

### 4. 16 rótulos en español redactados por una sesión anterior

`AdminTenantPricingPage` (9) y el histórico de precios. **El tono es de una IA, no del humano**:
hay que revisarlos antes de vender.

### 5. `GET /api/v1/assets/domains` rompe a un cliente viejo

Un cliente que use la respuesta sin `limit`/`offset` ve `NaN–NaN`. **No se puso `Number.isFinite` en
`Pagination` a propósito**: convertiría un bug real del backend en «no se pinta nada», que es peor.
Decisión pendiente.

## Técnico

### `scan_credit_cost()` sin `organization_id`: los precios por tenant no se aplican

Dos sitios, **los dos** con el mismo defecto:

| Sitio | Llamada |
| :--- | :--- |
| `backend/apps/repositories/pipeline.py:506` | `scan_credit_cost(ScanModeEnum.QUICK)` |
| `backend/apps/pentests/service.py:321` | `scan_credit_cost(payload.scan_mode)` |

La función **acepta** `organization_id` (`billing/pricing.py`) y sin él devuelve el precio de
plataforma, así que **los precios negociados por tenant no se aplican** en ninguno de los dos
caminos de cobro.

**No se ha arreglado**, y no se puede arreglar solo en uno: hacerlo solo en el pipeline de PR
**crearía dos precios para la misma operación**, y el que se paga dependería de por dónde entró el
trabajo. Hay que hacerlo en los dos a la vez, con las pruebas de los dos.

### Consolidar `escape_like`

Siguen **cuatro** copias locales de `_escape_like` en el árbol (`assets/service.py:111`,
`support/service.py:314`, `repositories/router.py:748`, `cve_database/service.py:200`) más el
módulo compartido `backend/core/filtros_texto.py`. **No ha crecido**: los nueve arreglos de este
proyecto usan el módulo compartido. Consolidarlas sigue pendiente y es un cambio propio: toca
módulos que funcionan y sus pruebas.

### Nomenclatura inconsistente del filtro de búsqueda

Se llama `query` en endpoints de cliente y `search` en los de consola. No se unificó porque
renombrar un parámetro de consulta rompe al llamador sin avisar. Documentado en ambos ficheros.

### 404 del favicon

Única respuesta ≥400 en el barrido de 40 capturas, sin localizar en qué pantalla.

## Contradicciones de CSS documentadas y sin arreglar

Decisiones de diseño, no técnicas:

- `.filter-field label` tiene `margin-bottom: 0` anulado por otra regla más abajo con la misma
  especificidad: el hueco real es 12 px, no 6.
- `.priority-badge` nunca se ve como círculo: `.margin-field input` le gana en 7 propiedades. Su
  comentario describe un círculo de 24 px que no se renderiza.
- `.margin-field select` y `.margin-field input` son la misma regla escrita dos veces.
- Casilla «Solo superusuarios» de Admin Users desalineada −29 px (preexistente, comprobado con
  `git stash`).

---

# HISTÓRICO — hecho y verificado en turnos anteriores

> Se conserva porque nadie tiene que volver a buscarlo. Si algo de aquí está desactualizado,
> **corregirlo aquí es mejor que acumular encima**.

## Refresco del token OAuth de Git

Ficheros: `backend/apps/repositories/token_refresh.py` (505 líneas), `oauth.py`, `credentials.py`,
`pipeline.py`, `tasks.py`, `services.py`, y la migración
`b3e7a1c5d9f4_indice_parcial_credenciales_por_vencer.py`.

| Decisión | Por qué |
| :--- | :--- |
| **`SELECT ... FOR UPDATE` con doble comprobación**, no un mutex de Python | El proceso **no** es la unidad de ejecución: hay varios workers de Celery, el uvicorn y el Beat. Un `asyncio.Lock` solo protege un proceso |
| **`populate_existing=True` en la segunda lectura** | Sin él, la segunda lectura devuelve la fila **del mapa de identidad** con la caducidad de antes, el worker cree que ya está renovada y no llama al proveedor. El `FOR UPDATE` solo no basta |
| **`refresh_token` se reescribe siempre** | **GitLab rota el `refresh_token` en cada refresco.** Guardar el viejo deja al siguiente refresco con un token que ya no existe |
| **`credentials.py` extraído** | Rompe un ciclo de imports: `token_refresh.py` depende de él, y `services.py` de los dos. Un solo sentido en el grafo |
| **Índice parcial `ix_git_credentials_por_vencer`** | El barrido solo mira las que vencen; un índice parcial sobre `token_expires_at` mantiene la consulta acotada sin penalizar al resto |

**`asegurar_credencial_vigente_en_sesion_ajena`** (`token_refresh.py:452`) es el punto de entrada
para los workers y los webhooks. Usa **sesión propia**, deliberadamente distinta de la del worker: si
la renovación escribiera en la sesión del worker, un `rollback()` posterior —que es lo normal cuando
el escaneo falla— **desharía la renovación**.

El **barrido por adelantado en Beat** (`renovar_credenciales_por_vencer`) es un **complemento, no una
garantía**: si `beat` está caído no pasa nada. El camino que sí garantiza es el bajo demanda.

## Gráficas (Apache ECharts)

Todo con `echarts/core` + `React.lazy` en su propio chunk. Los esquemas viven **en un solo sitio**,
`frontend/src/charts/opciones.ts`, y se prueban sin montar un panel (`opciones.test.ts`).

| Pantalla | Qué se añadió |
| :--- | :--- |
| **Dashboard** | Gauge de *Security Score*, anillo de severidad y **línea apilada de evolución por día** (30 días × 5 severidades) |
| **Issues** | Barras de severidad **con el filtro activo** + estado de remediación. Viven en `GraficosIssues.tsx`, no en `IssuesPage` |
| **Detalle de escaneo** (`PentestRunPage`) | Reparto por severidad y por estado del escaneo |
| **Redes / Contenedores** | Anillos y barras de ocupación |

Backend: `GET /api/v1/dashboard/summary` devolvió `severity_distribution`, `status_distribution` y
`findings_trend`; `GET /api/v1/pentests/{id}/findings` devolvió `severity_distribution` y
`status_distribution`.

**Qué se descartó y por qué** (los comentarios de `opciones.ts` lo explican entero):

| Descartado | Por qué |
| :--- | :--- |
| `echarts-for-react` (lo que pedía la especificación de fase) | 153 kB de envoltura para lo que son cuatro registros. Se usa `echarts/core` con `CanvasRenderer` y nada más. Sigue siendo Apache ECharts, que es lo que manda `AGENTS.md` |
| `grid.containLabel` | ECharts 6 lo marca **obsoleto** y avisa por consola en cada pintado. Se escribió su equivalente: `outerBoundsMode: 'same'` + `outerBoundsContain: 'axisLabel'` |
| Barras **verticales** | Las etiquetas son palabras: «Remediación propuesta» son 22 caracteres y no cabe girada bajo 260 px. Con las categorías en el eje vertical el texto va en horizontal y se lee entero |
| **Cinco líneas sueltas** en la evolución por día | Con cinco pendientes encima de las otras hay que compararlas todas; apiladas, la silueta de la banda superior **es** la producción total y su pendiente responde a «¿está mejorando o empeorando?» |
| **Degradados** en el relleno de área | `design-dark.md` los prohíbe, y además un relleno suave mezclaría el amarillo de `MEDIUM` con el naranja de `HIGH` justo donde ambos se tocan |
| Etiquetar los **150 puntos** (30 días × 5 severidades) | Eso no es un gráfico, es ruido. Solo se etiqueta el último punto de cada serie |
| **Un color por estado** de remediación | El color dejaría de significar severidad. Los estados van monocromos salvo `FIXED`, en el acento del sistema |
| Dibujar **gráficos de ceros** | Cinco barras de ancho cero ocupan una tarjeta entera para decir «no hay nada». Sale el estado vacío con su texto |
| **`--color-muted`** para los ejes de texto | Está en 3,04:1, por debajo del 4,5:1 de AA. Los ejes usan `caption`, que está en 8,0:1 |

## Filtros completos

`frontend/src/features/shared/Pagination.tsx` es el componente compartido. Props
`{total, limit, offset, onOffsetChange, namespace?}`. **No** reutilizar
`features/admin/PaginationBar`: recibe un tipo de la consola, e importarlo desde el panel rompe el
aislamiento de navegación.

| Pantalla | Filtros | Endpoint |
| :--- | :--- | :--- |
| **CVE** | buscador, severidad, año, KEV, paginación | `/api/v1/cve/search` |
| **PR Reviews** | buscador + rango de fechas + estado + repositorio | **`/api/v1/pr-reviews/` — fue el endpoint ampliado**: recibió `query`, `created_from`, `created_to` |
| **Support Tickets** | buscador + rango + estado + paginación | `/api/v1/support/tickets` |
| **Domains** | buscador + rango + verificados + paginación | `/api/v1/assets/domains` — ahora **paginado** (`limit`/`offset` en la respuesta, que antes no existían) |
| **Asset Discovery** | buscador + rango + filtros de inventario | `/api/v1/assets/discovery` |
| **Knowledge Documents** | buscador + rango + limpiar | `GET /api/v1/knowledge/documents` |
| **Pentests** | buscador + estado + tipo de objetivo + modo | `/api/v1/pentests/` |
| **Admin Sales** | buscador + rango + limpiar | `/api/v1/admin/sales` |
| **Admin Users** | botón de limpiar (el buscador ya existía) | `/api/v1/admin/users` |
| **Admin Audit** | buscador + rango | `/api/v1/admin/audit` |
| **Remote Repositories** | buscador **en el servidor** | `/api/v1/repositories/remote` |

En `useCveCatalog.ts`, `offset` es estado **propio** del hook, no un campo del filtro.

## Defectos de UI corregidos

Todos diagnosticados midiendo en el navegador, no leyendo el código:

- Etiqueta pegada al texto: clase nueva `.cell-inline` (flex, `gap: 8px`, `nowrap`).
- **Cronología del escaneo**: etiqueta y hora en columnas. Era un `<span>` sin estilo.
- **Icono de KEV** que caía a la línea siguiente conservando su `margin-left`.
- **Casilla «Solo KEV»** desalineada: `.filter-field-check` con `padding-top: 28px` (la altura real
  de la fila de etiqueta) y `min-height: 64px` para centrar en la fila de control.
- **Buscador de CVE** que cortaba el placeholder a media palabra: `min-width: 330px` **medido**
  (con 300 el input solo recibía 254 px y el placeholder necesita 274).
- **Pestaña Contenedores** de Operaciones: era un muñón que pintaba un vacío fijo con un contador
  que no le correspondía. Cableada a `GET /admin/operations/containers`.
- **Carrera de `useAsyncResource`**: la rama de carga debe ser `if (isLoading)` **a secas**, nunca
  `if (isLoading && data === null)`. Con la segunda forma se pintan las filas de la sección
  anterior.
- **10 iconos de estado vacío** unificados a `.empty-card-mark`.
- **14 textos rotos** del catálogo técnico (`catalog.*` no existía en ningún idioma).
- **2 etiquetas `aria`** de coste que `LlmModelsPage` pedía en el namespace `llm` y solo estaban
  en `admin`.
- **`LineChart` registrada** en `EChart.tsx` (no estaba importada; por eso no había línea apilada).
- **273 cabeceras «## Por qué» vacías** en `index.css` reescritas con explicación verificada.

## Los nueve `LIKE` sin escapar: TODOS corregidos (verificado por mutación)

Los nueve tenían el defecto. Ninguno venía ya escapado de otra capa. Todos usan ahora `coincide` o
`patron_contains` de `core.filtros_texto`, y **no se escribió ninguna quinta copia** de
`_escape_like`.

| Endpoint | Dónde | Prueba que lo cubre |
| :--- | :--- | :--- |
| `GET /api/v1/pentests/?search=` | `pentests/router.py` | `test_pentest_listing_api.py::test_pentest_list_no_trata_los_comodines_como_comodines` |
| `GET /api/v1/vulnerabilities/?search=` | `vulnerabilities/router.py` | `test_vulnerability_breakdowns.py::test_el_buscador_de_hallazgos_trata_los_comodines_como_literales` |
| `GET /api/v1/knowledge/?search=` | `knowledge/router.py` | `test_knowledge_api.py::test_el_buscador_del_catalogo_trata_los_comodines_como_literales` |
| `GET /api/v1/repositories/{id}/reviews?source_branch=` | `repositories/router.py` | `test_repository_reviews_api.py::test_el_filtro_de_rama_trata_los_comodines_como_literales` |
| `GET /api/v1/admin/operations/scans?busqueda=` | `admin/operations_router.py` | `test_admin_console.py::test_el_buscador_de_escaneos_no_trata_los_comodines_como_comodines` |
| `GET /api/v1/admin/tenants?search=` | `admin/queries.py` | `test_admin_console.py::test_el_buscador_de_tenants_no_trata_los_comodines_como_comodines` |
| `listar_paquetes(busqueda=)` | `supply_chain/service.py` | `test_supply_chain_api.py::test_el_buscador_trata_los_comodines_como_literales` |
| Recuperación de documentos del chat | `knowledge/retrieval.py` | `test_knowledge_retrieval.py::test_el_escape_del_patron_de_la_recuperacion_lleva_escape` — **mira el SQL, no el resultado** |
| Herramienta MCP `list_repositories` | `api_access/mcp_tools.py` | `test_mcp_server.py::test_list_repositories_trata_los_comodines_del_search_como_literales` |

Dos cosas que conviene no volver a buscar:

- **`knowledge/retrieval.py` es el caso raro**: el escape **no se puede demostrar por el
  resultado**, porque `recuperar_documentos` filtra dos veces —el `ILIKE` trae candidatos y luego
  `_puntuar` los repasa con un `in` de Python, que es subcadena literal—. Se comprobó revirtiendo
  el `ILIKE` a `f"%{termino}%"` y la prueba de comportamiento **seguía en verde**. Por eso la
  condición se extrajo a `condicion_de_terminos` y hay una prueba que mira el SQL emitido
  (`ESCAPE` declarado y barra invertida en el parámetro).
- **El `_` no es comprobable en `retrieval.py`**: `extraer_terminos` trocea `web_app` en `web_app`,
  `web` y `app`, y `web` y `app` ya son subcadena de `webXapp`. El escape se pone igual porque es lo
  correcto, no porque ese caso se pueda demostrar ahí.

## Defectos REALES encontrados (no volver a buscarlos)

| # | Defecto | Por qué costaba verlo |
| :--- | :--- | :--- |
| 1 | **`LIKE` sin escapar**: `?search=%` devolvía la tabla entera y `?search=web_app` traía `webXapp` | La consulta funciona; lo que falla es que devuelve filas que nadie pidió. La pantalla «funciona» |
| 2 | **Tickets a partir del 26 invisibles** | El cliente no mandaba `limit`, el backend topeaba a 25, y no había ni barra ni aviso |
| 3 | **`getAdminAuditLog` ignoraba `limit` y `offset`** | El botón «Siguiente» pintaba **las mismas 50 filas** con el resumen «51–100 de 210». Invisible porque `total` venía bien |
| 4 | **`total` inflado por producto cartesiano** en `list_assets` (faltaba el `JOIN` en el recuento) | 13 en vez de 8. El número mayor no se ve mal: se ve «hay más» |
| 5 | **La vista de documentos reventaba con un solo documento** | Leía `content` del listado, que no lo devuelve, y hacía `.replace()` sobre `undefined`. Pantalla en blanco. Nadie lo había visto porque el workspace de demostración no tenía ninguno |
| 6 | **Un test inestable** en `test_vulnerability_breakdowns.py` | `discovered_at` es `server_default=func.now()` y en PostgreSQL `now()` es el timestamp **de transacción**: todas las filas que siembra un test nacen con la misma marca. El orden se va al desempate por `id DESC` sobre UUID aleatorios. Medido: **9 fallos de 20**. Arreglado fijando `discovered_at` explícitamente al sembrar |
| 7 | **Un test inestable** en `test_vulnerability_triage_api.py`, **arreglado** | Comparaba el reloj del servidor de PostgreSQL con el reloj local. Ver abajo |
| 8 | **El buscador del inventario se borraba al no encontrar nada** | Estaba dentro de la rama de «hay repositorios». El usuario perdía el control con el que corregir lo que había escrito |
| 9 | **El tope del inventario era 10, no 100** | Con diez filas, la fila en la posición 300 de 500 no aparecía, aunque el servidor devolviera `total: 1` |

### El flake (7): ARREGLADO comparando contra el reloj que selló la fila

```python
# antes, en test_vulnerability_triage_api.py
assert entry.created_at <= datetime.now(UTC)
# ahora
assert entry.created_at <= await _reloj_de_la_base(integration_session)
```

`AuditLogEntry.created_at` es `server_default=func.now()`, o sea **el reloj del servidor de base de
datos**. El `datetime.now(UTC)` de la derecha era **el reloj de la máquina local**. La aserción solo
pasaba si el test duraba más que la deriva entre los dos relojes.

**Medido: `clock_timestamp()` de la BD − reloj local = +1,98 s a +2,08 s**, estable. Aquí no se pudo
reproducir el fallo: 0 de 20 antes y 0 de 20 después. Con una sonda que fuerza el caso límite —cero
segundos de trabajo tras sellar la fila— la comparación antigua **falla** y la nueva **pasa**.

**El arreglo no es «sembrar la fila», aunque esa era la instrucción inicial**: el patrón de
`test_dashboard_api.py:343` —fijar la columna `now()` al sembrar— **no se puede aplicar aquí**,
porque la fila de auditoría **no la siembra la prueba**: la escribe el `PATCH` que se está probando.
Sembrarla sería dejar de probar lo que se prueba.

Dos detalles que parecen detalles y no lo son:

- **`clock_timestamp()` y no `now()`.** La fixture `integration_session` comparte conexión y
  transacción con la aplicación, así que `now()` devolvería **exactamente** el valor almacenado y la
  comparación sería tautológica.
- **Nada de margen escrito.** `datetime.now(UTC) + timedelta(seconds=5)` sigue siendo una carrera,
  solo que con el margen movido, y además esconde la deriva en vez de quitarla.

## Flakes: cómo se comprueba uno

Un «ahora pasa» de una sola ejecución **no demuestra nada**. El proceder que sí sirve:

1. Ejecutar el test **20 veces seguidas** antes del arreglo y contar.
2. Aplicar el arreglo.
3. Ejecutar **20 veces seguidas** después.
4. **Verificar la mutación**: devolver el código al defecto y comprobar que la prueba cae.

En `test_vulnerability_breakdowns.py::test_desgloses_cuentan_el_conjunto_y_no_la_pagina`:
**9 fallos de 20 antes, 0 de 20 después**.

El paso 4 es el que faltaba en el flake 7, y la mutación dio **20 de 20 PASS**: la prueba con el
reloj local **no cae nunca en esta máquina**, porque la fase `call` tarda 2,1–2,4 s contra una
deriva de 2,0 s. Por eso el 0 de 20 antes del arreglo **no demuestra que el defecto estuviera
arreglado**.

Reglas generales:

- **Si una prueba siembra filas y luego afirma sobre el orden o sobre qué filas caen en una página,
  hay que fijar explícitamente la columna `now()`** (`discovered_at`, `created_at`, `updated_at`). Es
  el patrón que convierte un gate en una ruleta.
- **Nunca compares un `now()` del servidor con un `now()` del cliente.** La deriva entre el VPS y la
  máquina local es de ~2,0 s. Si la fila la siembra la prueba, se fija la columna; si la escribe el
  código que se está probando —un `PATCH`, un worker, un endpoint— no se puede sembrar, y entonces se
  compara contra `select(func.clock_timestamp())`.
- **Si la función filtra dos veces, el segundo filtro puede tapar al primero**, y entonces una
  prueba de comportamiento **no detecta el defecto**. Es lo que pasa en `knowledge/retrieval.py`.
  Cuando sospeches de eso, la prueba tiene que mirar el **SQL emitido**.

---

## Andamiaje en la raíz — NO BORRAR

Hay seis ficheros de andamiaje commiteados en la raíz. El humano tiene una regla explícita de no
borrar nada sin su permiso y ya se le ha preguntado, así que **solo se documentan**.

| Fichero | Qué es | Aviso |
| :--- | :--- | :--- |
| **`_parche.py`** | Reescribe `backend/apps/repositories/router.py` desde una plantilla incrustada | ⚠️ **DESTRUCTIVO.** La plantilla está desfasada. Si alguien lo ejecuta, **rompe el router**. Es **el primero que hay que borrar** |
| `_parche_router.py.txt` | La plantilla del parche, en texto plano | Inofensivo por sí solo; es la fuente del peligro de `_parche.py` |
| `_det.py` | Sonda de diagnóstico de `_parche.py` | Inofensivo |
| `_inp.py` | Lectura de entrada para el diagnóstico | Inofensivo |
| `_scan.py` | Escaneo rápido durante la depuración del parche | Inofensivo |
| `_mutaciones_refresco_token.py` | Mutaciones para demostrar no-vacuidad del refresco de token | Inofensivo; el germen de `backend/tests/test_refresh_token_credencial.py` |

## Estado de la base de demostración

Las credenciales de **GitHub de las dos organizaciones demo quedaron sobrescritas con tokens de
prueba**. Hay que **reconectar GitHub en las dos** antes de dar por bueno cualquier flujo de
repositorios, PRs o webhooks.

| Organización | `slug` | `token_expires_at` (a 4 oct) |
| :--- | :--- | :--- |
| `Acme RedTeam Security` | `demo-acme` | 2026-10-04 14:23 UTC |
| `MindGuard Fenix HQ` | `mindguard-hq` | 2026-10-04 14:23 UTC |

Solo hay **dos** filas en `git_credentials`, las dos `GITHUB`. Se reescribieron el 4 de octubre a la
misma hora y con una vida de 8 horas, que es la firma del barrido de Beat pasando por encima. Si el
valor guardado es un token real o de prueba **no se puede saber sin llamar a GitHub**: asumirlos de
prueba y reconectar.

## Docstrings que mienten — y eso hay que dejarlo escrito

Un docstring que afirma algo falso no es un problema de estilo: es una instrucción que la siguiente
sesión va a seguir.

| Docstring | Lo que afirma | Lo que es cierto |
| :--- | :--- | :--- |
| `backend/apps/billing/models.py:184` | Que `pentest_runs.precio_creditos_unitario` sella el precio por trabajo | **Esa columna no existe.** Un grep de `precio_creditos_unitario` en todo el árbol devuelve **una sola línea: la del docstring**. Lo que sella el precio es el asiento del `credit_ledger` |
| `backend/apps/knowledge/retrieval.py:268` | Que el desempate por título hace la consulta **estable** | Es falso. El título no es único. Faltaba el `id` detrás, y se ha añadido |

## Estructura de tareas: por qué existe `repositories/tasks.py`

`celery_app.py` dice que «la tarea vive con su dominio». `repositories/tasks.py` existe para que
`reviews.py` y `pipeline.py` **no importen Celery arriba**: hacerlo arrastraría el registro de
tareas entero al servidor de la API, que no ejecuta ninguna. Por eso el watchdog de revisiones se
**declara en `celery_app.py`** con un `import` local dentro del cuerpo de la función, en vez de
importar el módulo de tareas.

Las dos tareas nuevas de este turno:

| Tarea | Qué hace |
| :--- | :--- |
| `repositories.watchdog_orphaned_reviews` | Cierra en `ERROR` las revisiones con `run_id IS NULL`. Mismo periodo del Beat que el watchdog de runs |
| `repositories.publish_pr_review_comment` | Reintenta **solo** la publicación del comentario. No puede repetir el escaneo |

---

## Advertencias para la sesión siguiente

- **Los subagentes trabajan en el mismo árbol**: sepáralos **por ficheros**, no por puertos, y
  **diles que no arranquen servidores**. Un conflicto de puertos produce errores de red que parecen
  fallos de código.
- **Los 10/10 de cada subagente son del árbol en ese momento**, no del estado final combinado.
  Relanza `ci_check.py` al final.
- **`MEMORY.md` lo escribe una sola persona.** Los subagentes lo han dejado intacto tres veces
  seguidas precisamente porque no era suyo. Si lo necesitas cambiar, dilo; si no, déjalo.
- **No escribas ficheros de andamiaje en la raíz.** Si algo hay que dejar como script, va en
  `scripts/` con prefijo `_` o directamente no se deja.
- **No arranques, reinicies ni pares servidores que no sean tuyos.** El uvicorn **no** lleva
  `--reload`.
- **Todo test nuevo se demuestra no vacío** (se muta el defecto y se ve caer). Un test verde que
  nunca ha fallado no demuestra nada. Y todo test que afirme sobre **el orden** se demuestra
  **20 veces seguidas**, no una.
- **Un «0 de 20» no es una demostración.** Si el defecto es una carrera, la máquina que ejecuta
  puede ganarla siempre hoy y aun así el gate ser una ruleta mañana. Lo que separa «arreglado» de
  «pasó hoy» es poder **volver al código viejo y ver caer la prueba**.
- **Cuidado con `Get-Content`/`Set-Content` de PowerShell sobre UTF-8 sin BOM**: lee como cp1252 y
  deja mojibake al escribir, y `-Encoding UTF8` mete además un BOM. Para escribir texto con tildes,
  usar la herramienta de edición, o `[System.IO.File]::WriteAllText` con
  `New-Object System.Text.UTF8Encoding($false)`.
- **Las capturas son la única prueba** de que una pantalla está bien. Los gates pasan con el texto
  cortado.
- No ejecutar `checkout`, `restore` ni `reset` sobre ficheros modificados sin preguntar.