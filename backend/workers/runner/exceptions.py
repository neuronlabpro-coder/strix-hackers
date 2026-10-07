"""Errores tipados del runner Docker de Strix."""


class SandboxError(RuntimeError):
    """Error base del ciclo de vida del sandbox."""


class SandboxTimeoutError(SandboxError):
    """El sandbox superó el timeout de ejecución configurado."""


class SandboxExecutionError(SandboxError):
    """La ejecución del sandbox no pudo completarse."""


class ContainerExecutionError(SandboxExecutionError):
    """Docker no pudo crear o ejecutar el contenedor de Strix."""


class SandboxWorkspaceError(SandboxError):
    """El host no permitió preparar el directorio de trabajo del run.

    ## Por qué es un tipo propio y no un `OSError` cualquiera

    Porque `run()` agrupa `OSError` con los errores del daemon de Docker y los dos
    convergen en `ContainerExecutionError`, que se lee como «Docker no pudo ejecutar el
    contenedor». Un `PermissionError` al crear `/tmp/fenix_workspaces` no es eso: es el host
    sin el directorio preparado o sin permisos para el usuario del worker, que es el paso
    manual que documenta `docs/deployment/dokploy.md`. Confundir los dos mandaba al
    operador a mirar el daemon cuando el problema estaba en un `mkdir`.
    """


class SandboxOutputError(SandboxError):
    """Strix no produjo un artefacto utilizable.

    ## Por qué el nombre ya no dice "JSON"

    Porque el contrato `results.json` con `scan_id`, `status` y `findings[]` **no existe**: un
    grep de todo el repositorio del motor no devuelve ni un fichero que lo mencione. Lo que el
    motor produce son cuatro artefactos —`run.json`, `findings.sarif`, `coverage.json` y
    `penetration_test_report.md`— y ninguno es un JSON único con la lista de hallazgos. El tipo
    se conserva, y con el nombre antiguo, para no romper la cadena de diagnostico que lo
    clasifica como `STRIX_OUTPUT_UNUSABLE`; lo que cambia es lo que afirma.
    """


class StrixRunIncompleteError(SandboxError):
    """El motor terminó sin completar el run, y su artefacto no describe un escaneo.

    ## Por qué es un tipo propio y no un `SandboxOutputError`

    Porque son dos cosas que el panel tiene que explicar de dos maneras. Un artefacto ilegible
    es «el motor no dejo nada que yo pueda leer» y a menudo mejora con otro modelo. Un
    artefacto legible que dice `status: "failed"` o `status: "running"` es «el motor lo intento y
    no lo termino»: hay un motivo en el registro que hay que mirar, y reintentar con los cinco
    modelos del catalogo solo gasta cola.

    Y no se convierte en un escaneo limpio con cero hallazgos: eso seria mentir sobre el motor.
    Un run sin terminar no ha examined nada, y decirlo como si lo hubiera hecho es la clase de
    mentira que este modulo vino a corregir en la direccion contraria.
    """


class SandboxCleanupError(SandboxError):
    """No se pudo completar la limpieza de un recurso efímero."""
