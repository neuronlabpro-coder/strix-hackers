# Informe de paridad funcional

Generado por `scripts/audit_strix_parity.py`. Los numeros de este informe se leen del codigo, no de un inventario: el unico modo de que un informe de paridad sirva de algo es que se pueda volver a generar y salga igual.

## Resumen

- Modulos auditados: **40**
- Funcionales: **33**
- En paridad: **2**
- Bloqueados por Fase 6: **0**
- Ausentes: **0**
- Marcas visibles de la marca: **0**
- Scopes declarados: **49**
- Pruebas de backend: **852** funciones (34 con `@parametrize`, que generan mas de un caso cada una)
- Pruebas de frontend: **43**

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

### identidad tecnica (170)

| Categoria | Situacion | Linea | Clasificacion |
| :--- | :--- | :--- | :--- |
| identidad tecnica | `backend/apps/billing/pricing.py:3` | Vive aquí y no en el router de pentests porque el worker de Strix también lo | identificador: Strix |
| identidad tecnica | `backend/apps/cve_database/models.py:6` | el impacto real de un `cve_id` que Strix reporte. | identificador: Strix |
| identidad tecnica | `backend/apps/pentests/router.py:38` | from backend.workers.runner.sandbox import StrixSandboxManager | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:51` | StrixSandboxManager.kill_container(container_reference) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:307` | container_reference = run.container_id or StrixSandboxManager.container_name_for_run( | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:312` | StrixSandboxManager.remove_network_for_run(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/pentests/router.py:313` | StrixSandboxManager.purge_workspace(str(run.id)) | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/repositories/autofix.py:60` | if len(patch.encode("utf-8")) > settings.strix_max_autofix_chars: | identificador: strix_max_autofix_chars |
| identidad tecnica | `backend/apps/repositories/pipeline.py:49` | from backend.workers.parser.strix_parser import extract_strix_scan_id, parse_strix_output | identificador: extract_strix_scan_id, parse_strix_output, strix_parser |
| identidad tecnica | `backend/apps/repositories/pipeline.py:50` | from backend.workers.runner.sandbox import SandboxRunResult, StrixSandboxManager | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/repositories/pipeline.py:64` | ManagerFactory = Callable[..., StrixSandboxManager] | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/repositories/pipeline.py:371` | await _mark_pipeline_error(session, claim, "STRIX_NONZERO_EXIT") | identificador: STRIX_NONZERO_EXIT |
| identidad tecnica | `backend/apps/repositories/pipeline.py:372` | raise PRPipelineError("Strix terminó con un código de salida no cero") | identificador: Strix |
| identidad tecnica | `backend/apps/repositories/pipeline.py:373` | expected_scan_id = extract_strix_scan_id(result.output_json) | identificador: extract_strix_scan_id |
| identidad tecnica | `backend/apps/repositories/pipeline.py:397` | findings = parse_strix_output( | identificador: parse_strix_output |
| identidad tecnica | `backend/apps/repositories/pipeline.py:503` | manager_factory: ManagerFactory = StrixSandboxManager, | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/repositories/pipeline.py:513` | manager: StrixSandboxManager \| None = None | identificador: StrixSandboxManager |
| identidad tecnica | `backend/apps/repositories/pipeline.py:537` | workspace_root=workspace_root or settings.strix_workspace_root, | identificador: strix_workspace_root |
| identidad tecnica | `backend/apps/supply_chain/service.py:13` | y la que no exige nada nuevo, pero **Strix no publica el inventario**: no hay por dónde | identificador: Strix |
| identidad tecnica | `backend/apps/vulnerabilities/autofix.py:5` | Strix **trae** el parche desde dentro del contenedor: el escaneo analiza el código y el... | identificador: Strix |
| identidad tecnica | `backend/apps/vulnerabilities/autofix.py:418` | MAX_RESPONSE_CHARS: Final[int] = settings.strix_max_autofix_chars | identificador: strix_max_autofix_chars |
| identidad tecnica | `backend/apps/vulnerabilities/remediation.py:299` | max_tokens=settings.strix_max_autofix_chars // 4, | identificador: strix_max_autofix_chars |
| identidad tecnica | `backend/core/config.py:87` | default="@fenix-team review,@strix review", | identificador: strix |
| identidad tecnica | `backend/core/config.py:224` | strix_max_output_bytes: int = Field(default=10_000_000, gt=0, le=50_000_000) | identificador: strix_max_output_bytes |
| identidad tecnica | `backend/core/config.py:225` | strix_max_findings: int = Field(default=10_000, gt=0, le=100_000) | identificador: strix_max_findings |
| identidad tecnica | `backend/core/config.py:226` | strix_max_description_chars: int = Field(default=100_000, gt=0, le=1_000_000) | identificador: strix_max_description_chars |
| identidad tecnica | `backend/core/config.py:227` | strix_max_poc_chars: int = Field(default=1_000_000, gt=0, le=5_000_000) | identificador: strix_max_poc_chars |
| identidad tecnica | `backend/core/config.py:228` | strix_max_autofix_chars: int = Field(default=2_000_000, gt=0, le=10_000_000) | identificador: strix_max_autofix_chars |
| identidad tecnica | `backend/core/config.py:229` | strix_sandbox_image: str = Field( | identificador: strix_sandbox_image |
| identidad tecnica | `backend/core/config.py:230` | default="ghcr.io/usestrix/strix-sandbox:latest", | identificador: strix, usestrix |
| identidad tecnica | `backend/core/config.py:233` | strix_workspace_root: str = Field(default="/tmp/fenix_workspaces", min_length=1)  # noq... | identificador: strix_workspace_root |
| identidad tecnica | `backend/core/config.py:234` | strix_network_prefix: str = Field(default="strix_net", min_length=1) | identificador: strix_net, strix_network_prefix |
| identidad tecnica | `backend/core/config.py:235` | strix_network_pool: str = Field( | identificador: strix_network_pool |
| identidad tecnica | `backend/core/config.py:239` | strix_require_egress_fence: bool = Field( | identificador: strix_require_egress_fence |
| identidad tecnica | `backend/core/config.py:242` | strix_require_llm_key_exposure_ack: bool = Field( | identificador: strix_require_llm_key_exposure_ack |
| identidad tecnica | `backend/core/config.py:245` | strix_llm_key_exposure_ack: str = Field(default="") | identificador: strix_llm_key_exposure_ack |
| identidad tecnica | `backend/core/config.py:246` | strix_memory_limit: str = Field(default="4g", min_length=1) | identificador: strix_memory_limit |
| identidad tecnica | `backend/core/config.py:247` | strix_cpu_limit: float = Field(default=2.0, gt=0, le=8) | identificador: strix_cpu_limit |
| identidad tecnica | `backend/core/config.py:248` | strix_pids_limit: int = Field(default=256, gt=0, le=100_000) | identificador: strix_pids_limit |
| identidad tecnica | `backend/core/config.py:249` | strix_worker_concurrency: int = Field(default=1, gt=0, le=32) | identificador: strix_worker_concurrency |
| identidad tecnica | `backend/core/config.py:250` | strix_hard_timeout_seconds: int = Field(default=1800, gt=0, le=86400) | identificador: strix_hard_timeout_seconds |
| identidad tecnica | `backend/core/config.py:251` | strix_soft_timeout_seconds: int = Field(default=1500, gt=0, le=86400) | identificador: strix_soft_timeout_seconds |
| identidad tecnica | `backend/core/config.py:252` | strix_watchdog_interval_seconds: int = Field(default=300, gt=0, le=86400) | identificador: strix_watchdog_interval_seconds |
| identidad tecnica | `backend/core/config.py:253` | strix_watchdog_stale_after_seconds: int = Field(default=1860, gt=0, le=172800) | identificador: strix_watchdog_stale_after_seconds |
| identidad tecnica | `backend/core/config.py:254` | default_strix_llm: str = Field(min_length=1) | identificador: default_strix_llm |
| identidad tecnica | `backend/core/config.py:446` | @field_validator("strix_network_pool") | identificador: strix_network_pool |
| identidad tecnica | `backend/core/config.py:448` | def validate_strix_network_pool(cls, value: str) -> str: | identificador: validate_strix_network_pool |
| identidad tecnica | `backend/core/config.py:484` | f"STRIX_NETWORK_POOL={value!r} no es un CIDR IPv4 valido. Debe tener la forma " | identificador: STRIX_NETWORK_POOL |
| identidad tecnica | `backend/core/config.py:490` | f"STRIX_NETWORK_POOL={value!r} tiene que ser IPv4: las reglas de salida del " | identificador: STRIX_NETWORK_POOL |
| identidad tecnica | `backend/core/config.py:495` | f"STRIX_NETWORK_POOL={value!r} tiene un prefijo de /{red.prefixlen}, demasiado " | identificador: STRIX_NETWORK_POOL |
| identidad tecnica | `backend/core/config.py:597` | if self.strix_soft_timeout_seconds >= self.strix_hard_timeout_seconds: | identificador: strix_hard_timeout_seconds, strix_soft_timeout_seconds |
| identidad tecnica | `backend/core/config.py:598` | raise ValueError("El timeout suave de Strix debe ser menor que el duro") | identificador: Strix |
| identidad tecnica | `backend/core/config.py:599` | if self.strix_hard_timeout_seconds >= self.celery_task_soft_time_limit_seconds: | identificador: strix_hard_timeout_seconds |
| identidad tecnica | `backend/core/config.py:600` | raise ValueError("Strix hard timeout debe ser menor que el timeout suave de Celery") | identificador: Strix |
| identidad tecnica | `backend/core/config.py:601` | if self.strix_watchdog_stale_after_seconds <= self.strix_hard_timeout_seconds: | identificador: strix_hard_timeout_seconds, strix_watchdog_stale_after_seconds |
| identidad tecnica | `backend/core/config.py:602` | raise ValueError("El watchdog debe esperar más que el timeout duro de Strix") | identificador: Strix |
| identidad tecnica | `backend/workers/celery_app.py:31` | worker_concurrency=settings.strix_worker_concurrency, | identificador: strix_worker_concurrency |
| identidad tecnica | `backend/workers/celery_app.py:40` | "schedule": timedelta(seconds=settings.strix_watchdog_interval_seconds), | identificador: strix_watchdog_interval_seconds |
| identidad tecnica | `backend/workers/tasks.py:45` | from backend.workers.parser.strix_parser import extract_strix_scan_id, parse_strix_output | identificador: extract_strix_scan_id, parse_strix_output, strix_parser |
| identidad tecnica | `backend/workers/tasks.py:47` | from backend.workers.runner.sandbox import StrixSandboxManager | identificador: StrixSandboxManager |

_Y 110 mas._

### comentario (28)

| Categoria | Situacion | Linea | Clasificacion |
| :--- | :--- | :--- | :--- |
| comentario | `backend/apps/pentests/models.py:26` | """Modos de ejecución soportados por Strix.""" | explicacion interna |
| comentario | `backend/workers/tasks.py:1` | """Tareas Celery de ejecución e ingesta de Strix.""" | explicacion interna |
| comentario | `backend/workers/tasks.py:959` | """Persiste atómicamente un reporte Strix y cierra el run como completado.""" | explicacion interna |
| comentario | `backend/workers/__init__.py:1` | """Workers y utilidades de ejecución de Strix.""" | explicacion interna |
| comentario | `backend/workers/parser/normalizer.py:18` | """Convierte severidades de Strix a valores persistidos canónicos.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:1` | """Parser estricto de los reportes JSON generados por Strix.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:18` | """Error controlado para salida Strix inválida o corrupta.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:75` | """Extrae el identificador de provenance de un reporte Strix válido.""" | explicacion interna |
| comentario | `backend/workers/parser/strix_parser.py:102` | """Convierte un reporte Strix en entidades sin abrir una transacción de persistencia. | explicacion interna |
| comentario | `backend/workers/parser/__init__.py:1` | """Parser de resultados y normalización de Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/exceptions.py:1` | """Errores tipados del runner Docker de Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/exceptions.py:17` | """Docker no pudo crear o ejecutar el contenedor de Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/exceptions.py:21` | """Strix no produjo un artefacto JSON utilizable.""" | explicacion interna |
| comentario | `backend/workers/runner/llm_key_exposure.py:58` | #: Texto que hay que escribir en `STRIX_LLM_KEY_EXPOSURE_ACK` para confirmar que la exp... | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:1` | """Ciclo de vida efímero del contenedor Strix.""" | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:44` | """Resultado normalizado de una ejecución Strix ya purgada.""" | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:52` | """Ejecuta Strix en un bridge y workspace exclusivos por run.""" | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:77` | # vacío o fallo de resolución), se recurre al `DEFAULT_STRIX_LLM`: es | explicacion interna |
| comentario | `backend/workers/runner/sandbox.py:230` | """Ejecuta Strix y devuelve el JSON leído antes de purgar el workspace.""" | explicacion interna |
| comentario | `backend/workers/runner/telemetry.py:106` | """Extrae el consumo de tokens del reporte de Strix, si lo publica. | explicacion interna |
| comentario | `backend/workers/runner/__init__.py:1` | """Ejecución aislada de Strix en contenedores efímeros.""" | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:2` | """Audita la paridad de la superficie funcional y escribe `STRIX_PARITY_REPORT.md`. | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:134` | # "strix" en su nombre, es del motor. Un nombre de clase, una funcion, un atributo de | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:137` | # motor **es** Strix y el contenedor que ejecuta **es** el suyo. | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:142` | # ## Por que no un patron que encuentre "strix" dentro del identificador | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:148` | # "strix", asi que en `StrixSandboxManager` el `[A-Za-z_]` se come la "S" inicial y lo ... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:149` | # es "trixSandboxManager", donde ya no hay un "strix" que emparejar. Se busco con mas g... | explicacion interna |
| comentario | `scripts/audit_strix_parity.py:419` | # modulos de Strix—. Se listan aparte para que sean visibles sin marcar el despliegue como | explicacion interna |

## Metodo

- **Superficie**: se leen las rutas de `<Route>` de `App.tsx` y se comparan con la lista de modulos de paridad. Las rutas relativas se tratan como submodulos de su padre.
- **Scopes**: se lee el enum `Scope` de `scopes.py` sin importarlo, para que un error de importacion en cualquier parte del arbol no impida a la auditoria llegar a diagnosticar.
- **Marca**: se recorre el arbol clasificando cada aparicion por extension y por si esta dentro de un comentario. Solo lo que el usuario lee se cuenta como fallo.
- **Pruebas**: se cuentan funciones `def test_` y `it(`/`test(`, no ficheros. Un fichero con cuarenta pruebas y otro con una ponderarian igual. La cifra de funciones es menor que la que reporta la suite porque una funcion parametrizada genera varios casos, y el total de casos no se puede derivar de aqui: un decorador con veinte valores genera veinte, y otro con dos genera dos.
