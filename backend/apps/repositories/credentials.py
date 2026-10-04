"""Primitivas de credencial Git que no dependen de nadie.

## Por qué este módulo existe

Porque hay dos cosas que necesitan los dos caminos —la fachada que construye el cliente y la
renovación que lo precede— ymeterlas en uno de los dos crea un ciclo que **falla al importar**,
no al ejecutar: en un ciclo de módulos, `from paquete import nombre` busca el atributo en el
módulo que todavía se está ejecutando y no lo encuentra.

Las dos piezas son:

- `GitCredentialNotFoundError`, que `get_organization_credential` lanza y que
  `asegurar_credentialo_vigente` lanza también —con el mismo texto, porque es el mismo hecho—.
- `encrypt_organization_credential`, que cifra el token de acceso al conectar una credencial y
  que cifra el token nuevo al renovarla. Es la misma operación con distinto `field`: por eso es
  una función y no dos.

## Por qué la dirección de la dependencia es esta

Porque estas dos primitivas no necesitan saber nada de renovar nada. `token_refresh.py` depende de
este módulo; `services.py` depende de este módulo **y** de `token_refresh.py`. Un solo sentido en
cada arista, y los dos'import` se resuelven siempre.

## Por qué `services.py` los sigue importando

Porque hay cuatro sitios que lossacan de ahí —`router.py`, `router_auth.py`, `pipeline.py` y
`test_git_webhooks.py`— y mover el módulo de un nombre no debería obligar a mover a sus
consumidores. `services.py` los importa para usarlos y, de paso, quedan reexportados: la ruta de
importación anterior sigue viva sin cambiar una línea de quien la usa.
"""

from __future__ import annotations

from uuid import UUID

from backend.apps.repositories.models import GitProviderEnum
from backend.core.crypto import encrypt_secret


class GitCredentialNotFoundError(LookupError):
    """No existe una credencial activa para la organización y proveedor."""


def encrypt_organization_credential(
    plaintext: str,
    organization_id: UUID,
    provider: GitProviderEnum,
    field: str = "access",
) -> str:
    """Cifra un token con AAD ligado al tenant, proveedor y tipo de campo.

    ## Por qué el `field` es parte del dato autenticado y no una etiqueta

    Porque es lo que hace que un `refresh_token` no se pueda descifrar como si fuera un token de
    acceso. Sin él, una fila rotada mal seguiría pareciendo válida —texto no vacío, formato
    correcto— y el fallo saldría mucho después, cuando alguien intentara renovar. Está escrito en
    `test_refresh_token_credencial.py`, que monta exactamente esa fila.
    """

    return encrypt_secret(
        plaintext,
        organization_id=str(organization_id),
        provider=provider.value,
        field=field,
    )
