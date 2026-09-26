# Evidencia TDD — Fase 5 · Bloque 5.2

**Alcance:** catálogo oficial de OpenRouter con `markup_pct`, enlace del orquestador al
runner de Strix con fallback encadenado, telemetría de consumo con ajuste contra la
reserva, y base de datos CVE de referencia con `/cve` en el panel y sincronización
periódica de los feeds oficiales.

**Gates al cierre:** backend `295 passed, 2 skipped`; `ruff check` sin hallazgos;
`pyright --project backend/pyproject.toml` con 0 errores; `alembic check` sin drift y
`alembic current` = `d5e6f7a8b9c0 (head)`. Frontend `typecheck`, `lint` y `build`
limpios (488.06 kB principal, 37.89 kB CSS, 453.53 kB EChart). i18n: 15 namespaces,
551 claves, paridad es/en, cero literales visibles en JSX. `.env.example` en paridad
con los 116 campos de `Settings`.

---

## 1. RED · Especificación antes que implementación

Dos ficheros de pruebas, escritos antes de tocar el código de aplicación:

* `backend/tests/test_cve_database_api.py` — 14 pruebas.
* `backend/tests/test_llm_catalog_and_runner.py` — 19 pruebas.

El RED inicial no fue un fallo de aserción sino de módulo inexistente, que es la
señal correcta: la especificación nombraba `backend/apps/pentests/tasks.py` y
`backend/apps/cve_database/sync.py`, dos rutas que no existen y que no se iba a crear
por obedecer al enunciado (ver §5).

```
ModuleNotFoundError: No module named 'backend.workers.runner.telemetry'
```

---

## 2. Diez hallazgos reales durante la implementación

Ninguno de estos lo detecta una prueba que solo compruebe que el código corre.

### 2.1 Ciclo de importación al mover `scan_credit_cost`

El enunciado pedía resolver el modelo desde `apps/pentests/`, y el worker ya
importaba el router de pentests. Importar `scan_credit_cost` desde ahí cerraba el
ciclo:

```
ImportError: cannot import name 'scan_credit_cost' from partially initialized module
'backend.apps.pentests.router' (most likely due to a circular import)
```

**Corrección:** el precio de un escaneo vive ahora en `backend/apps/billing/pricing.py`.
Es una función de precio, no de routing, y el router la reexporta. El ciclo desaparece
porque `billing` no importa a nadie del worker.

### 2.2 `_container_environment` era privado y las pruebas lo necesitan

El sandbox construía su entorno en un método privado. La prueba que verifica que el
contenedor recibe el modelo resuelto —que es exactamente el contrato del bloque— tenía
que alcanzarlo por `manager._container_environment()`. Se renombró a
`container_environment()` y se documentó como parte del contrato: el modelo que
inyecta el orquestador y la clave que consume Strix se deciden ahí.

### 2.3 El comodín `_` de LIKE se colaba en la búsqueda de CVE

`cve_id ILIKE '%<query>%'` trata `_` como comodín de un carácter. Buscando
`CVE-2026-900001` devolvía ocho registros, no uno. No es inyección —la consulta está
parametrizada— pero es semántica incorrecta: el usuario recibe coincidencias que no
pidió.

**Corrección:** `_escape_like` neutraliza `\`, `%` y `_` antes de interpolar, y la
consulta declara `escape="\\"`. Prueba nueva: `test_search_escapes_like_wildcards`
comprueba que `%` devuelve cero resultados y que `9000_1` no encuentra `CVE-2026-900011`.

### 2.4 `total` de trending KEV mentía con el límite aplicado

`CVEPage.total` devolvía `len(records)` ya truncado por el `limit`. El panel muestra
«N explotadas» y ese N cambiaba según cuántas filas se pintaran.

**Corrección:** `count_kev` cuenta el catálogo entero con una consulta aparte. La
prueba compara `trending_limited.total == trending.total`.

### 2.5 La resincronización reportaba `updated` sin cambiar nada

`sync_cve_catalog` reescribía todas las filas existentes y contaba cada una como
actualizada. El informe de la tarea periódica habría dicho «1500 actualizados» cada
noche sin que el feed trajese nada nuevo.

**Corrección:** solo se cuenta como `updated` lo que realmente difiere. Es la
diferencia entre un informe útil y uno que nadie creería.

### 2.6 Un bloque de consumo corrupto se tarificaba a medias

`prompt_tokens: -5` con `completion_tokens: 10` devolvía `TokenUsage(0, 10)`: la mitad
buena de un dato corrupto. Con eso, la mitad del consumo real se cobraba y la otra
mitad se regalaba.

**Corrección:** si algún token declarado es inutilizable, el bloque entero se
descarta y se registra. Se prefiere no cobrar antes que cobrar una cifra que no se
puede defender.

### 2.7 El cargo de la telemetría se sumaba en vez de restarse

`apply_credit_delta` suma el importe. `charge()` pasaba el precio al cliente tal cual,
así que la primera prueba de tarificación pasó a `1007,50` créditos sobre una
organización que arrancaba en `1000`. El error era de signo y era silencioso: una
tarificación invertida no rompe nada, solo regala margen.

**Corrección:** el signo se invierte dentro de `charge()` con un comentario que explica
por qué. La regla es que el llamador nunca tiene que acordarse del signo. Tres
pruebas cubren los tres casos: exceso de reserva (devuelve), defecto de reserva (cobra)
y coincidencia exacta (ni asiento ni movimiento).

### 2.8 `engine.rollback()` no existe en `AsyncEngine`

En la ruta de error del reembolso se llamaba `await engine.rollback()`. `AsyncEngine`
no tiene ese método, así que el `AttributeError` habría sustituido al error real del
reembolso en el log, y el tecnico habría estado depurando la causa equivocada.

Lo detectó `pyright`, no una prueba. **Corrección:** el `rollback` va sobre la sesión,
dentro del `async with`, y la excepción se propaga al `except` exterior que sí registra.

### 2.9 Alembic no sabe comparar índices de expresión

`alembic check` reportaba `remove_index` sobre `ix_cve_records_search` en cada
ejecución. El índice GIN sobre `to_tsvector` es un índice funcional, y el generador no
lo empareja con el modelo. La puerta de calidad era inútil.

**Corrección:** `include_object` en `backend/migrations/env.py` excluye los índices
funcionales declarados en migración, con el motivo escrito en el código. Excluir no
es silenciar: la definición real sigue en la migración `c4d5e6f7a8b9`, que es donde se
revisa. Durante la depuración se descubrió además que el `CREATE INDEX` con
`gin_trgm_ops` fallaba porque el índice se creaba antes que la extensión `pg_trgm`.

### 2.10 `alembic check` comparaba el `tsvector` con y sin `::regconfig`

El modelo construye `to_tsvector('spanish'::regconfig, ...)` porque `REGCONFIG` no
tiene renderizador de literales en SQLAlchemy. La migración creaba
`to_tsvector('spanish', ...)`. Misma expresión, texto distinto, y Alembic los trata
como índices diferentes. La migración se alineó con el modelo.

---

## 3. Sobre el nombre de la tabla y de los ficheros

El enunciado nombra `llm_models_config` y `backend/apps/pentests/tasks.py`. Ninguna de
las dos rutas existe:

* La tabla real es `llm_model_configs`. Renombrarla por coherencia de singular sería una
  migración de una vista que no aporta nada y que complica el historial. Se documenta la
  diferencia.
* Las tareas de Celery viven en `backend/workers/tasks.py`. `apps/` contiene código de
  aplicación síncrono; `workers/` contiene el proceso que lanza contenedores Docker. El
  runner de Strix es lo segundo por definición: no puede vivir junto al código que
  atiende peticiones HTTP porque comparte el motor pero no el ciclo de vida.

---

## 4. Decisiones que sí siguen el enunciado

* **`markup_pct` sustituye a `profit_margin_pct`.** El campo es un recargo sobre el
  coste base. Con la definición habitual de margen —porcentaje sobre el precio de venta—
  un 150 % sería imposible, porque el precio tendría que ser negativo. El nombre anterior
  inducía a la lectura equivocada. El docstring de `LLMModelConfig` lo explica.
* **La tabla oficial.** `anthropic/claude-3.7-sonnet` sustituye al 3.5 y se le añade
  `deepseek/deepseek-r1`, `openai/o3-mini` y `openrouter/auto`. Las prioridades 1 a 6
  construyen la cadena de resolución. El modelo retirado se desactiva en lugar de
  borrarse cuando tiene historial de consumo, porque `llm_usage_events` lo referencia.
* **`1 crédito = 1,00 USD`.** `credits_per_usd` deja de ser un entero y pasa a `Decimal`.
* **`/cve` sigue la interfaz de Strix** completa: buscador central, severidades, KEV
  only, navegación por año, lista de explotación activa y card promocional a `/pentests`.

---

## 5. Lo que el bloque NO resuelve y conviene decir

* **El reporte de Strix no publica consumo de tokens.** `extract_token_usage` acepta el
  bloque si aparece (`usage`, `token_usage` o `tokens`, en objeto o en lista, con tres
  alias por campo) y devuelve `None` si no. La cadena está probada por los dos lados,
  pero en producción el camino real todavía no existe. Cuando Strix exponga el dato, no
  hay que tocar nada.
* **El worker no ve los códigos HTTP de OpenRouter.** El fallback ante `429`/`5xx` se
  implementa como reintento encadenado: se observa que la ejecución falló y se reencola
  con el siguiente modelo. Solo se insiste ante errores que otro modelo podría mejorar
  (`STRIX_NONZERO_EXIT`, `STRIX_EXECUTION_FAILED`); un contenedor que ni arrancó no
  mejora cambiando de proveedor.
* **`scan_credit_cost` sigue sin calibrar.** Con `CREDITS_PER_USD = 1`, un pentest de
  500 k tokens de entrada y 200 k de salida en Sonnet costaría unos 2,20 USD frente a
  los 10 créditos cobrados. Es intencional hasta que se sepa cuántas llamadas hace un
  pentest real, y está anotado en `ROADMAP.md`.
* **El refund cubre el fallo de encolado, no el fallo posterior a arrancar el
  contenedor.** Decisión pendiente del responsable; el código actual reembolsa cuando la
  cadena de modelos se agota sin éxito, y esa es una extensión natural del mismo
  camino.
* **Las credenciales de Stripe siguen sin existir en `.env`.** Todo el camino de 5.1 se
  probó con dobles y con el verificador de firma real del SDK. Nada de este bloque toca
  Stripe.

---

## 6. Verificación en ejecución

Comprobado contra el backend en marcha (puerto 8000), no solo en la suite:

* `/openapi.json` incluye `GET /api/v1/cve/search`, `/years`, `/trending-kev` y
  `/api/v1/cve/{cve_id}`.
* `GET /api/v1/cve/search` sin token devuelve `401`. La autenticación es obligatoria
  aunque los datos sean públicos: la ruta describe técnicas de explotación.
* `alembic upgrade head` aplicó `b3c4d5e6f7a8` (renombrado, catálogo oficial,
  `numeric(12,4)`) y `c4d5e6f7a8b9` (`cve_records`, índices GIN, 50 registros de
  semilla) contra la base remota por Tailscale.

---

## 7. Sustitución del catálogo por el listado del Owner

Ajuste posterior dentro del mismo bloque, con la migración `d5e6f7a8b9c0`.

### 7.1 Por qué es una migración nueva

`b3c4d5e6f7a8` ya estaba aplicada en la base remota cuando llegó el listado definitivo.
Editar su seed habría dejado el repositorio describiendo un catálogo y la base
teniendo otro, y `alembic check` no lo detecta porque compara el esquema, no las
filas. La corrección va hacia adelante, que es la única dirección que la historia de
la base permite deshacer.

### 7.2 `STRIX_LLM` y no `STRIX_LLM_MODEL`

El enunciado nombra `STRIX_LLM_MODEL`. El motor lee `STRIX_LLM`:

```
strix/llm/config.py:12:  self.model_name = model_name or os.getenv("STRIX_LLM", "openai/gpt-5")
strix/interface/main.py:47:  if not os.getenv("STRIX_LLM"): missing_required_vars.append(...)
```

Inyectar con el nombre del enunciado habría sido la conmutación silenciosa más cara del
proyecto: el contenedor no habría dado error, habría caído a su `openai/gpt-5` por
defecto, cada escaneo habría corrido fuera del catálogo y `llm_usage_events` no habría
recibido nunca una fila. No hay forma de que una prueba de integración lo detecte
porque el contenedor arranca bien.

La prueba `test_primary_model_reaches_the_container_as_strix_llm` ata las dos
mitades: resuelve la cadena, construye el sandbox y comprueba que `STRIX_LLM` vale
`z-ai/glm-5.3` **y** que `STRIX_LLM_MODEL` no está en el entorno. Si alguien la añade
creyendo que es un alias, el contenedor la ignorará y la aserción explícita no lo
detectará sola: por eso está escrita en negativo.

### 7.3 `use_case = ALL` para un primario transversal

El Owner pidió `z-ai/glm-5.3` como `ALL / DEEP_PENTEST`. `model_id` tiene restricción
única y `use_case` es una sola columna, así que dos filas para el mismo modelo son
imposibles. `ALL` es la única forma de expresar «primario de todas sin duplicar»,
porque `resolve_model_chain` incluye los modelos `ALL` en cada cadena. Verificado
contra la base, no solo contra el modelo:

```
cadena ALL          : z-ai/glm-5.3(1) -> openai/gpt-6-sol(6) -> moonshotai/kimi-k3(7)
cadena DEEP_PENTEST : z-ai/glm-5.3(1) -> openai/gpt-6-astra(2) -> claude-opus-5.5(3) -> deepseek-v4-pro-0813(4) -> gpt-6-sol(6) -> kimi-k3(7)
cadena QUICK_SCAN   : z-ai/glm-5.3(1) -> openai/gpt-6-sol(6) -> moonshotai/kimi-k3(7) -> deepseek-v4.1-flash(8)
cadena AUTOFIX      : z-ai/glm-5.3(1) -> claude-fable-5.1(5) -> openai/gpt-6-sol(6) -> moonshotai/kimi-k3(7)

STRIX_LLM inyectado al contenedor: z-ai/glm-5.3
```

### 7.4 Un test de 5.1 que hubo que corregir

`test_seed_contains_valid_models_in_fallback_order` afirmaba que *todo* modelo sembrado
está activo y que las prioridades son únicas sobre *todas* las filas. Con el catálogo
sustituido, ambas cosas dejan de ser ciertas: los seis modelos retirados se conservan
desactivados para no perder su historial de consumo, así que hay filas inactivas y sus
prioridades chocan con las nuevas.

El arreglo correcto no era borrar los datos históricos para satisfacer la prueba, sino
acotar la invariante a lo que la cadena de resolución realmente consulta: los modelos
activos. Exigir unicidad sobre todas las filas obligaría a destruir historial cada vez
que el Owner cambie de catálogo.

### 7.5 Consecuencia de negocio que conviene mirar

Con `z-ai/glm-5.3` en prioridad 1 y transversal, `deepseek/deepseek-v4.1-flash` —el
modelo de $0,15 que justifica el caso de uso `QUICK_SCAN`— queda en prioridad 8 y
nunca es primario de nada. Los escaneos rápidos empiezan por un modelo de $0,40 y solo
llegarían al flash como último recurso tras fallar con dos modelos más caros.

Se implementó exactamente lo pedido y queda anotado en `ROADMAP.md` y en la
especificación de la fase. La corrección, si se quiere, es mover el flash por encima
del 1 o declararlo `QUICK_SCAN` con prioridad 1 — pero entonces dejaría de ser
transversal y los escaneos profundos perderían su primario. Es una decisión de
producto sobre qué modelo debe servir cada tipo de escaneo.

### 7.6 Precios sin contrastar

Los costes base son los que fijó el Owner. El sistema no los verifica contra ningún
feed de OpenRouter, así que el margen se audita contra los números declarados.
Contrastarlos con la carta de precios antes de abrir el producto comercialmente.
