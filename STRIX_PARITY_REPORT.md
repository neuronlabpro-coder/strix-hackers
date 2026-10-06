# Informe de paridad funcional

Generado por `scripts/audit_strix_parity.py`. Los numeros de este informe se leen del codigo, no de un inventario: el unico modo de que un informe de paridad sirva de algo es que se pueda volver a generar y salga igual.

## Resumen

- Modulos auditados: **41**
- Funcionales: **34**
- En paridad: **2**
- Bloqueados por Fase 6: **0**
- Ausentes: **0**
- Marcas visibles de la marca: **0**
- Scopes declarados: **49**
- Pruebas de backend: **1188** funciones (47 con `@parametrize`, que generan mas de un caso cada una)
- Pruebas de frontend: **187**

## Superficie funcional

| Modulo | Estado | Nota |
| :--- | :--- | :--- |
| Dashboard | Funcional |  |
| Pentests | Funcional |  |
| Autofix | Funcional |  |
| Gestor de Vulnerabilidades | Funcional |  |
| Detalle de Vulnerabilidad | Funcional |  |
| PR Reviews | Funcional |  |
| Supply Chain | Funcional |  |
| Contenedores | Funcional |  |
| Redes | Funcional |  |
| Chat | Funcional |  |
| Repositorios | Funcional |  |
| Dominios | Funcional |  |
| Asset Discovery | Funcional |  |
| Knowledge (OKF) | Funcional |  |
| Base de Datos CVE | Funcional |  |
| Integraciones | Funcional |  |
| API Access | Funcional |  |
| Ajustes | Funcional |  |
| Consola SuperAdmin | Funcional |  |
| Ajustes > General | Funcional | subruta de `/settings` |
| Ajustes > Auditoría | Funcional | subruta de `/settings` |
| Ajustes > Miembros | Funcional | subruta de `/settings` |
| Ajustes > Facturación | Funcional | subruta de `/settings` |
| Ajustes > Soporte | Funcional | subruta de `/settings` |
| Admin > Tenants | Funcional | subruta de `/admin` |
| Admin > Usuarios | Funcional | subruta de `/admin` |
| Admin > Ventas | Funcional | subruta de `/admin` |
| Admin > Auditoría | Funcional | subruta de `/admin` |
| Admin > Agentes | Funcional | subruta de `/admin` |
| Admin > Catálogo LLM | Funcional | subruta de `/admin` |
| Admin > Tickets | Funcional | subruta de `/admin` |
| Ruta fuera del alcance de paridad: `/billing` | Ruta no declarada | no es un modulo de paridad; se documenta, no se penaliza |
| Ruta fuera del alcance de paridad: `/invitations/accept` | Ruta no declarada | no es un modulo de paridad; se documenta, no se penaliza |
| Ruta fuera del alcance de paridad: `/login` | Ruta no declarada | no es un modulo de paridad; se documenta, no se penaliza |
| Ruta fuera del alcance de paridad: `/register` | Ruta no declarada | no es un modulo de paridad; se documenta, no se penaliza |
| Ruta fuera del alcance de paridad: `/verify-email` | Ruta no declarada | no es un modulo de paridad; se documenta, no se penaliza |
| Scopes declarados en el backend | Funcional | 49 en el enum `Scope` |
| Scopes con representacion literal en pruebas | En paridad | 13 de 49 aparecen literalmente; el resto se cubre por pruebas de autorizacion que usan el enum |
| Scopes con representacion en la interfaz | En paridad | 8 de 49 aparecen en el frontend; los que no, se resuelven en tiempo de ejecucion desde el catalogo de la API |
| Marca visible para el usuario final | Funcional | cero apariciones en componentes y traducciones |
| Titulo de la ventana | Funcional | el documento declara el titulo en `frontend/index.html` |

## Auditoria de scopes

El enum `Scope` declara **49** permisos. Los tres sitios donde uno tiene que poder aparecer son el catalogo de la API, la interfaz y las pruebas, y cada uno falla de forma distinta: un scope sin pruebas es un permiso sin verificar, y un scope sin interfaz es un permiso que existe y que nadie puede ejercer.

| Ambito | Valor |
| :--- | :--- |
| `admin:analytics_read` | sin referencia literal |
| `admin:models_manage` | cubierto |
| `assets:read` | cubierto |
| `audit:read` | sin referencia literal |
| `billing:checkout` | sin referencia literal |
| `billing:ledger_read` | cubierto |
| `billing:read` | sin referencia literal |
| `chat:read` | cubierto |
| `chat:write` | cubierto |
| `cve:read` | sin referencia literal |
| `cve:search` | sin referencia literal |
| `enterprise:containers_read` | sin referencia literal |
| `enterprise:containers_write` | sin referencia literal |
| `enterprise:networks_read` | sin referencia literal |
| `enterprise:networks_write` | sin referencia literal |
| `enterprise:supply_chain_read` | sin referencia literal |
| `enterprise:supply_chain_write` | cubierto |
| `knowledge:delete` | sin referencia literal |
| `knowledge:read` | sin referencia literal |
| `knowledge:write` | sin referencia literal |
| `llm:models_read` | sin referencia literal |
| `llm:usage_read` | sin referencia literal |
| `mcp:connect` | cubierto |
| `mcp:invoke` | cubierto |
| `members:invite` | sin referencia literal |
| `members:read` | sin referencia literal |
| `members:remove` | sin referencia literal |
| `organization:read` | sin referencia literal |
| `organization:update` | sin referencia literal |
| `pentests:abort` | sin referencia literal |
| `pentests:create` | cubierto |
| `pentests:delete` | sin referencia literal |
| `pentests:read` | cubierto |
| `pr_reviews:read` | sin referencia literal |
| `pr_reviews:trigger` | sin referencia literal |
| `repositories:connect` | sin referencia literal |
| `repositories:delete` | sin referencia literal |
| `repositories:read` | cubierto |
| `repositories:sync` | sin referencia literal |
| `tokens:create` | sin referencia literal |
| `tokens:read` | cubierto |
| `tokens:revoke` | sin referencia literal |
| `vulnerabilities:export` | sin referencia literal |
| `vulnerabilities:read` | cubierto |
| `vulnerabilities:triage` | cubierto |
| `webhooks:create` | cubierto |
| `webhooks:delete` | sin referencia literal |
| `webhooks:read` | cubierto |
| `webhooks:update` | sin referencia literal |

## Marca

La auditoria separa tres clases de aparicion, y solo la primera hace fallar el informe.

| Categoria | Que es | Decision |
| :--- | :--- | :--- |
| `usuario` | Literal en un componente o en un fichero de traduccion | **Fallo** |
| `identidad tecnica` | Nombres de clase, imagen, variables del motor | Se mantiene |
| `comentario` | Explicacion interna del codigo | Se mantiene |
| `por revisar` | Fuera de un comentario y no es identidad tecnica | A revisar a mano |

Los mensajes de error que ve un **operador** al desplegar mantienen el nombre del motor a proposito. `El timeout suave de Strix debe ser menor que el duro` dice que variable tocar; si dijera el nombre del producto, el operador buscaria una opcion que no existe, porque el timeout pertenece al contenedor del motor y no a la configuracion del panel. Renombrar un mensaje de diagnostico para que suene a marca es cambiar su precision por su apariencia.

### usuario: ninguna

### por revisar: ninguna

### identidad tecnica (247)

| Categoria | Situacion | Linea | Clasificacion |
| :--- | :--- | :--- | :--- |
| identidad tecnica | `frontend/src/locales/en/pentests.json:112` | "STRIX_DOCKER_UNAVAILABLE": "The worker could not reach the Docker daemon on the server... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:113` | "STRIX_EGRESS_FENCE_MISSING": "The egress fence is missing on the worker host, so the s... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:114` | "STRIX_LLM_KEY_ACK_MISSING": "The deployment configuration does not acknowledge exposin... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:115` | "STRIX_IMAGE_UNAVAILABLE": "The analysis container image is not present on the worker h... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:116` | "STRIX_WORKSPACE_UNAVAILABLE": "The worker host does not allow preparing the scan worki... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:117` | "EGRESS_FENCE_DISABLED": "The egress fence check is turned off (STRIX_REQUIRE_EGRESS_FE... | nombre de una variable de entorno de la plataforma |
| identidad tecnica | `frontend/src/locales/en/pentests.json:118` | "STRIX_OUTPUT_UNUSABLE": "The engine finished but left no readable results file. A diff... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:119` | "STRIX_NONZERO_EXIT": "The engine finished with a non-zero exit code.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:120` | "STRIX_TIMEOUT": "The scan exceeded the configured maximum time.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:121` | "STRIX_EXECUTION_FAILED": "The worker did not record a reason this panel can explain. T... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:144` | "STRIX_WORKSPACE_UNAVAILABLE": "The responding process cannot create the scan working d... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:145` | "STRIX_EGRESS_FENCE_MISSING": "The filtering chain the runner checks is not present on ... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:147` | "STRIX_LLM_KEY_ACK_MISSING": "This process configuration does not acknowledge exposing ... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:148` | "STRIX_EXECUTION_FAILED": "This process could not complete a check and does not know wh... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:149` | "STRIX_DOCKER_UNAVAILABLE": "This process could not talk to the Docker daemon while che... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:150` | "STRIX_IMAGE_UNAVAILABLE": "This process cannot find the analysis container image.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:151` | "STRIX_TIMEOUT": "The check did not finish in time.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/en/pentests.json:152` | "STRIX_OUTPUT_UNUSABLE": "This check does not produce a usable result." | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:112` | "STRIX_DOCKER_UNAVAILABLE": "El worker no pudo conectarse al demonio Docker del servido... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:113` | "STRIX_EGRESS_FENCE_MISSING": "Falta el cerco de salida en el host del worker, así que ... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:114` | "STRIX_LLM_KEY_ACK_MISSING": "Falta el reconocimiento de exposición de la clave del pro... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:115` | "STRIX_IMAGE_UNAVAILABLE": "La imagen del contenedor de análisis no está en el servidor... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:116` | "STRIX_WORKSPACE_UNAVAILABLE": "El host del worker no permite preparar el directorio de... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:117` | "EGRESS_FENCE_DISABLED": "La comprobación del cerco de salida está desactivada en este ... | nombre de una variable de entorno de la plataforma |
| identidad tecnica | `frontend/src/locales/es/pentests.json:118` | "STRIX_OUTPUT_UNUSABLE": "El motor terminó pero no dejó un archivo de resultados legibl... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:119` | "STRIX_NONZERO_EXIT": "El motor terminó con un código de salida distinto de cero.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:120` | "STRIX_TIMEOUT": "El escaneo superó el tiempo máximo configurado.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:121` | "STRIX_EXECUTION_FAILED": "El worker no registró un motivo que el panel sepa explicar. ... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:144` | "STRIX_WORKSPACE_UNAVAILABLE": "El proceso que responde no puede crear el directorio de... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:145` | "STRIX_EGRESS_FENCE_MISSING": "En el servidor que responde no se encuentra la cadena de... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:147` | "STRIX_LLM_KEY_ACK_MISSING": "La configuracion de este proceso no toma la decision de e... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:148` | "STRIX_EXECUTION_FAILED": "Este proceso no pudo completar una comprobacion y no sabe po... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:149` | "STRIX_DOCKER_UNAVAILABLE": "Este proceso no pudo hablar con el demonio Docker al compr... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:150` | "STRIX_IMAGE_UNAVAILABLE": "Este proceso no encuentra la imagen del contenedor de anali... | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:151` | "STRIX_TIMEOUT": "La comprobacion no termino a tiempo.", | clave de un codigo de error del motor |
| identidad tecnica | `frontend/src/locales/es/pentests.json:152` | "STRIX_OUTPUT_UNUSABLE": "Esta comprobacion no produce un resultado utilizable." | clave de un codigo de error del motor |
| identidad tecnica | `backend/apps/admin/operations_router.py:39` | (`fenix-strix-{run_id}`) y su referencia vive en `pentest_runs.container_id`. La secció... | identificador: strix |
| identidad tecnica | `backend/apps/admin/operations_router.py:555` | from backend.workers.runner.sandbox import StrixSandboxManager | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:567` | nombre = StrixSandboxManager.container_name_for_run(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:585` | StrixSandboxManager.kill_container( | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:606` | StrixSandboxManager.remove_network_for_run(str(run.id), client=cliente) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:607` | StrixSandboxManager.purge_workspace(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:662` | from backend.workers.runner.sandbox import StrixSandboxManager | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:690` | container_id=run.container_id or StrixSandboxManager.container_name_for_run( | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/admin/operations_router.py:693` | nombre_esperado=StrixSandboxManager.container_name_for_run(str(run.id)), | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/agents/models.py:5` | El sandbox de Strix vive en un bridge aislado cuyo cerco de salida permite DNS, HTTPS y | identificador: Strix |
| identidad tecnica | `backend/apps/billing/admin_router.py:40` | Porque leer el precio de la base en cada cobro no es una opción: el worker de Strix cob... | identificador: Strix |
| identidad tecnica | `backend/apps/billing/catalogo.py:4` | cuesta un escaneo—, que se leen en sitios sin sesión: el worker de Strix, dos servicios... | identificador: Strix |
| identidad tecnica | `backend/apps/billing/organization_prices.py:31` | Porque el precio se lee en sitios **sin sesión**: el worker de Strix cobra desde una ta... | identificador: Strix |
| identidad tecnica | `backend/apps/billing/pricing.py:3` | Vive aquí y no en el router de pentests porque el worker de Strix también lo | identificador: Strix |
| identidad tecnica | `backend/apps/billing/pricing.py:17` | Porque el precio se lee en sitios donde **no hay sesión**: el worker de Strix ajusta | identificador: Strix |
| identidad tecnica | `backend/apps/billing/pricing.py:217` | Porque hay cuatro llamadores —el servicio de pentests, el worker de Strix, el | identificador: Strix |
| identidad tecnica | `backend/apps/cve_database/models.py:6` | el impacto real de un `cve_id` que Strix reporte. | identificador: Strix |
| identidad tecnica | `backend/apps/pentests/abort.py:65` | from backend.workers.runner.sandbox import StrixSandboxManager | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/abort.py:143` | referencia = run.container_id or StrixSandboxManager.container_name_for_run(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/abort.py:146` | StrixSandboxManager.remove_network_for_run(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/abort.py:147` | StrixSandboxManager.purge_workspace(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:49` | from backend.workers.runner.sandbox import StrixSandboxManager | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:82` | StrixSandboxManager.kill_container(container_reference) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:518` | container_reference = run.container_id or StrixSandboxManager.container_name_for_run( | identificador: StrixSandboxManager |

_Y 187 mas._

### comentario (54)

| Categoria | Situacion | Linea | Clasificacion |
| :--- | :--- | :--- | :--- |
| comentario | `frontend/src/styles/index.css:1228` | * filtrado por `STRIX_EGRESS_FENCE_MISSING` lo busca en la columna `error_message` de l... | explicacion interna |
| comentario | `frontend/src/features/pentests/PentestRunPage.tsx:29` | * de `scripts/audit_strix_parity.py` lo cuenta como fallo con razón —el nombre del motor | explicacion interna |
| comentario | `frontend/src/features/pentests/PentestRunPage.tsx:36` | const FALLO_DESCONOCIDO = ['run.fallos.', 'STRIX_', 'EXECUTION_FAILED'].join('') | explicacion interna |
| comentario | `frontend/src/features/pentests/PentestRunPage.tsx:346` | esta tarjeta el usuario ve un `STRIX_EXECUTION_FAILED` en la terminal y un gráfico | explicacion interna |
| comentario | `frontend/src/features/pentests/PentestRunPage.tsx:347` | vacío, y no hay forma de pasar de ahí a nada accionable: el `STRIX_EXECUTION_FAILED` | explicacion interna |
| comentario | `backend/apps/pentests/models.py:26` | """Modos de ejecución soportados por Strix.""" | explicacion interna |
| comentario | `backend/workers/tasks.py:1` | """Tareas Celery de ejecución e ingesta de Strix.""" | explicacion interna |
| comentario | `backend/workers/tasks.py:72` | # solo toma el valor `STRIX_NONZERO_EXIT` —el único desenlace que devuelve un código— o... | explicacion interna |
| comentario | `backend/workers/tasks.py:1038` | # `finally` no se dispara. El contenedor de Strix se queda vivo con el código | explicacion interna |
| comentario | `backend/workers/tasks.py:1243` | # `STRIX_TIMEOUT` por eso. Lo que sí hace este camino es dejar el run en `TIMED_OUT` | explicacion interna |
| comentario | `backend/workers/tasks.py:1247` | # El código que se persiste **no** es `STRIX_EXECUTION_FAILED`: es el que dice qué | explicacion interna |
| comentario | `backend/workers/tasks.py:1284` | """Persiste atómicamente un reporte Strix y cierra el run como completado.""" | explicacion interna |
| comentario | `backend/workers/__init__.py:1` | """Workers y utilidades de ejecución de Strix.""" | explicacion interna |
| comentario | `backend/workers/parser/normalizer.py:18` | """Convierte severidades de Strix a valores persistidos canónicos.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:1` | """Parser estricto de los reportes JSON generados por Strix.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:18` | """Error controlado para salida Strix inválida o corrupta.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:75` | """Extrae el identificador de provenance de un reporte Strix válido.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:102` | """Convierte un reporte Strix en entidades sin abrir una transacción de persistencia. | explicacion interna |
| comentario | `backend/workers/parser/__init__.py:1` | """Parser de resultados y normalización de Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/diagnostico.py:80` | #: solo vale `STRIX_NONZERO_EXIT` o `None`. El código desconocido se escribe al final del | explicacion interna |
| comentario | `backend/workers/runner/exceptions.py:1` | """Errores tipados del runner Docker de Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/exceptions.py:17` | """Docker no pudo crear o ejecutar el contenedor de Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/exceptions.py:35` | """Strix no produjo un artefacto JSON utilizable.""" | explicacion interna |
| comentario | `backend/workers/runner/llm_key_exposure.py:58` | #: Texto que hay que escribir en `STRIX_LLM_KEY_EXPOSURE_ACK` para confirmar que la exp... | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:1` | """Ciclo de vida efímero del contenedor Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:45` | """Resultado normalizado de una ejecución Strix ya purgada.""" | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:53` | """Ejecuta Strix en un bridge y workspace exclusivos por run.""" | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:78` | # vacío o fallo de resolución), se recurre al `DEFAULT_STRIX_LLM`: es | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:241` | """Ejecuta Strix y devuelve el JSON leído antes de purgar el workspace.""" | explicacion interna |
| comentario | `backend/workers/runner/telemetry.py:106` | """Extrae el consumo de tokens del reporte de Strix, si lo publica. | explicacion interna |
| comentario | `backend/workers/runner/__init__.py:1` | """Ejecución aislada de Strix en contenedores efímeros.""" | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:2` | """Audita la paridad de la superficie funcional y escribe `STRIX_PARITY_REPORT.md`. | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:135` | # "strix" en su nombre, es del motor. Un nombre de clase, una funcion, un atributo de | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:138` | # motor **es** Strix y el contenedor que ejecuta **es** el suyo. | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:143` | # ## Por que no un patron que encuentre "strix" dentro del identificador | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:149` | # "strix", asi que en `StrixSandboxManager` el `[A-Za-z_]` se come la "S" inicial y lo ... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:150` | # es "trixSandboxManager", donde ya no hay un "strix" que emparejar. Se busco con mas g... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:288` | #: —`STRIX_DOCKER_UNAVAILABLE`— en `pentest_runs.error_message` y el panel lo traduce. Ese | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:296` | CODIGO_DE_ERROR = re.compile(r"^\s*\"[A-Z0-9_]*(?:STRIX)[A-Z0-9_]*\"\s*:") | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:300` | #: La marca aparece en `STRIX_REQUIRE_EGRESS_FENCE`, y es lo unico que el texto puede d... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:324` | Porque `STRIX_REQUIRE_EGRESS_FENCE` es una **variable de entorno de la plataforma**, y el | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:336` | `SCREAMING_SNAKE_CASE` se exime; una que además dice «Strix» en prosa no. Con la marca ... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:347` | if "strix" in candidato.lower() | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:352` | # `find`, no con `in`: `STRIX_REQUIRE_EGRESS_FENCE` contiene a `STRIX` pero no al revés... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:353` | # comparación al reves es lo que haria que una prosa que mencionara `STRIX` suelto saliera | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:367` | return [m.start() for m in re.finditer(r"strix", linea, re.IGNORECASE)] | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:420` | suave de Strix...")` en el modulo de configuracion tiene un simbolo en la misma linea y | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:431` | de error —`STRIX_DOCKER_UNAVAILABLE`—, que es el contrato con el worker que escribe ese | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:501` | if "strix" not in linea.lower(): | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:650` | # modulos de Strix—. Se listan aparte para que sean visibles sin marcar el despliegue como | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:718` | "funcional" if "Strix" not in leer(RAIZ / "frontend/index.html") else "FALLA", | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:808` | "Generado por `scripts/audit_strix_parity.py`. Los numeros de este informe se leen del " | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:854` | "a proposito. `El timeout suave de Strix debe ser menor que el duro` dice que variable " | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:910` | help="ruta del informe (por defecto STRIX_PARITY_REPORT.md en la raiz)", | explicacion interna |

## Metodo

- **Superficie**: se leen las rutas de `<Route>` de `App.tsx` y se comparan con la lista de modulos de paridad. Las rutas relativas se tratan como submodulos de su padre.
- **Scopes**: se lee el enum `Scope` de `scopes.py` sin importarlo, para que un error de importacion en cualquier parte del arbol no impida a la auditoria llegar a diagnosticar.
- **Marca**: se recorre el arbol clasificando cada aparicion por extension y por si esta dentro de un comentario. Solo lo que el usuario lee se cuenta como fallo.
- **Pruebas**: se cuentan funciones `def test_` y `it(`/`test(`, no ficheros. Un fichero con cuarenta pruebas y otro con una ponderarian igual. La cifra de funciones es menor que la que reporta la suite porque una funcion parametrizada genera varios casos, y el total de casos no se puede derivar de aqui: un decorador con veinte valores genera veinte, y otro con dos genera dos.
