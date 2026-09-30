"""Autenticación de los agentes, separada de la de las personas.

## Por qué esta función no puede devolver un `TenantContext`

Porque es la única garantía de que un token de agente no llegue a ser una sesión de panel. Un
agente se autentica aquí y resuelve a un `ScannerAgent`; `get_current_tenant` resuelve a un
`TenantContext` y no tiene forma de producir un agente. Los dos caminos no comparten nada, así
que la separación no depende de que cada endpoint recuerde comprobar nada.

Que el `Depends` de esta función se monte **solo** en este router es parte del diseño, y por eso
los endpoints de panel no tienen ni una importación a este módulo. Si alguien lo importa allí,
la revisión lo enseña; si no, no hay forma de que ocurra por descuido.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.apps.agents.models import ScannerAgent
from backend.apps.agents.service import (
    AgentNotFoundError,
    AgentRevokedError,
    autenticar_agente,
)
from backend.core.middleware import SessionDependency

#: `auto_error=False` para que el `401` lo componga esta función y no el de FastAPI. Un
#: `auto_error=True` respondería con su propio mensaje, que no distingue entre un token que no
#: es de agente y uno que no existe, y que además no pasa por el log estructurado.
_bearer = HTTPBearer(auto_error=False, description="Token de agente (`mgf_agent_...`)")


def _no_autorizado(detalle: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detalle,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_agent(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SessionDependency,
) -> ScannerAgent:
    """El agente que hace la petición, o un `401`.

    El token viaja en la cabecera `Authorization: Bearer`, igual que el de una persona, y el
    prefijo los separa. No es una decisión de estilo: un `401` de aquí no dice si el token no
    existe o está revocado en la respuesta que puede ver un atacante, porque el mensaje de
    "revocado" solo se le dice a quien ya lo tenía.
    """

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _no_autorizado("Se requiere un token de agente")

    try:
        return await autenticar_agente(session, credentials.credentials)
    except AgentRevokedError as error:
        # Distinto del token desconocido, y a propósito: quien tenía el token recibe un motivo
        # accionable, y un atacante que prueba tokens no obtiene información, porque el token
        # revocado no se puede convertir en uno vigente.
        raise _no_autorizado(
            "Este agente está dado de baja. Revócalo de nuevo o emite uno nuevo desde "
            "Ajustes > Agentes de escaneo."
        ) from error
    except AgentNotFoundError as error:
        raise _no_autorizado("Token de agente desconocido") from error


#: El agente autenticado, para los endpoints del propio agente.
AgentDependency = Annotated[ScannerAgent, Depends(get_current_agent)]
