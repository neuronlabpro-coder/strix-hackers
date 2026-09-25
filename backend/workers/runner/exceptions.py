"""Errores tipados del runner Docker de Strix."""


class SandboxError(RuntimeError):
    """Error base del ciclo de vida del sandbox."""


class SandboxTimeoutError(SandboxError):
    """El sandbox superó el timeout de ejecución configurado."""


class SandboxExecutionError(SandboxError):
    """La ejecución del sandbox no pudo completarse."""


class ContainerExecutionError(SandboxExecutionError):
    """Docker no pudo crear o ejecutar el contenedor de Strix."""


class SandboxOutputError(SandboxError):
    """Strix no produjo un artefacto JSON utilizable."""


class SandboxCleanupError(SandboxError):
    """No se pudo completar la limpieza de un recurso efímero."""
