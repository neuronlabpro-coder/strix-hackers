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
    """Strix no produjo un artefacto JSON utilizable."""


class SandboxCleanupError(SandboxError):
    """No se pudo completar la limpieza de un recurso efímero."""
