"""Hashea el token de invitación y añade la unicidad por organización al inventario de repos.

## Por qué el token de invitación cambia de forma

Porque era una credencial de portador en **texto plano** en la tabla `invitations`, y su hermano
`User.email_verification_token_hash`, en la misma base, ya estaba hasheado. Leer la tabla —un
backup, un `SELECT` de un operador, una réplica— era incorporarse a cualquier workspace con el
rol que le asignó el invitador, y el token no caduca hasta que se acepta.

La migración **no borra nada**: renombra la columna a `token_hash`, la pasa a `String(64)`, y
rellena las filas existentes con la huella de lo que ya había. El token de una invitación viva
sigue funcionando, porque el hash es determinista y es el mismo valor que se comparaba antes.

## Por qué el hash y no una función lenta

Porque el token tiene 256 bits de entropía: quien tiene la fila tiene la clave. Una función lenta
solo añadiría latencia a aceptar una invitación. Es el mismo criterio que el token de API, y por
eso se reutiliza `hash_email_verification_token` en vez de inventar un segundo hash para lo
mismo.

## Por qué el índice único sobrevive

`token` era `unique=True, index=True`. `token_hash` es un SHA-256 de un valor único, así que
también es único, y perder el índice sería una regresión de rendimiento en la aceptación. Se
declara explícitamente en vez de confiar en el `unique` implícito.

## Por qué `repositories` pierde la unicidad global

Porque `UNIQUE (provider, remote_repo_id)` **sin `organization_id`** es una restricción global
que produce dos defectos a la vez:

1. **Denegación de servicio entre tenants**: la organización A reclama un repositorio popular y
   la organización B no puede conectarlo nunca, porque la fila ya existe. La restricción de
   integridad se estaba usando como mecanismo deLan Claims.
2. **Oráculo de existencia entre tenants**: `connect_repository` busca por
   `(provider, remote_repo_id)` sin filtrar por organización y devuelve un `409` con el texto de
   que ya está vinculada a otra organización.

Que dos organizaciones conecten el mismo repositorio no es un problema: cada una tiene su
propio PAT, su propio espacio de revisión y su propio cargo. El `repositories` de la A no es el
de la B. Convertir la restricción en `(organization_id, provider, remote_repo_id)` deja el
índice útil para las consultas del panel —que siempre filtran por organización— y hace las dos
cosas correctas a la vez.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: La columna vieja y la nueva. Se declaran aquí para que el par no pueda desincronizarse.
#:
#: Y son también la **razón** de que la consulta de relleno lleve su excepción de `S608`: no son
#: entrada de nadie, son constantes de este fichero. El aviso se desactiva porque la alternativa
#: —`quote_ident` sobre un nombre que no puede variar— no añade nada y hace la migración más larga
#: de leer. Es una excepción con motivo, no un silenciado.
COLUMNA_VIEJA = "token"
COLUMNA_NUEVA = "token_hash"
INVITACIONES = "invitations"
REPOSITORIES = "repositories"
#: El índice único que `token` tenía. Se crea de nuevo sobre la columna nueva.
INDICE_VIEJO = "ix_invitations_token"
INDICE_NUEVO = "ix_invitations_token_hash"
#: La restricción global que se sustituye.
RESTRICCION_VIEJA = "uq_repositories_provider_remote"
RESTRICCION_NUEVA = "uq_repositories_org_provider_remote"


def _huella(token: str) -> str:
    """El mismo SHA-256 que usa `hash_email_verification_token` en el servicio.

    Se recalcula aquí en vez de llamarse porque una migración no puede importar código de
    aplicación: la función podría cambiar y las filas ya hasheadas dejarían de coincidir. Es la
    misma razón por la que una migración no debe depender de una constante que otro día se
    mueva.
    """

    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def upgrade() -> None:
    # 1) La columna. Se ensancha a 64 antes de escribir, porque un SHA-256 en hex son 64
    #    caracteres y los tokens originales eran mas cortos; hacerlo al reves daria error de
    #    truncamiento en vez de un fallo de datos.
    op.alter_column(INVITACIONES, COLUMNA_VIEJA, new_column_name=COLUMNA_NUEVA)
    op.alter_column(
        INVITACIONES,
        COLUMNA_NUEVA,
        existing_type=sqlalchemy.String(length=128),
        type_=sqlalchemy.String(length=64),
        existing_nullable=False,
    )
    op.create_index(INDICE_NUEVO, INVITACIONES, [COLUMNA_NUEVA], unique=True)
    op.drop_index(INDICE_VIEJO, table_name=INVITACIONES)

    # 2) Las filas que ya existen. Sin esto, las invitaciones vivas dejarian de aceptarse: el
    #    servicio buscaria por huella y la columna guardaria el token en claro.
    #
    #    Se hace en Python y no en SQL porque la huella es un SHA-256 y la extension `digest` de
    #    PostgreSQL no esta garantizada en la imagen del despliegue. Sin `pgcrypto` la expresion
    #    seria un error de arranque de la migracion, y una migracion que depende de una extension
    #    opcional es una migracion que fallara en el servidor de un cliente y no en el tuyo.
    connection = op.get_bind()
    # `text()` y no una cadena suelta: `execute` de SQLAlchemy 2 espera un `Executable`, y una
    # cadena cruda es el error que de aqui salio. Los parametros van con dos puntos y en un
    # diccionario aparte, que es lo que los hace seguros.
    #
    # El nombre de tabla y de columna son constantes de este fichero, no entrada de nadie, y por
    # eso se interpolan: la alternativa —construir el identificador con `quote_ident`— seria
    # mas correcta en teoria y mas fragil en la practica, porque aqui no hay nada que inyectar.
    leer = sqlalchemy.text(
        f"SELECT id, {COLUMNA_NUEVA} FROM {INVITACIONES} WHERE {COLUMNA_NUEVA} IS NOT NULL"  # noqa: S608
    )
    escribir = sqlalchemy.text(
        f"UPDATE {INVITACIONES} SET {COLUMNA_NUEVA} = :huella WHERE id = :identificador"  # noqa: S608
    )
    for identificador, token in connection.execute(leer).fetchall():
        connection.execute(
            escribir,
            {"huella": _huella(str(token)), "identificador": identificador},
        )

    # 3) La unicidad de `repositories`, de global a por organizacion.
    op.drop_constraint(RESTRICCION_VIEJA, REPOSITORIES, type_="unique")
    op.create_unique_constraint(
        RESTRICCION_NUEVA, REPOSITORIES, ["organization_id", "provider", "remote_repo_id"]
    )


def downgrade() -> None:
    """Se niega a bajar, y lo dice antes de tocar nada.

    ## Por qué falla en vez de deshacer

    Porque **no se puede recuperar el token en claro**: solo se conservó su huella, y una huella
    no se deshace. Una baja que renombrase la columna a `token` dejaría una tabla que *parece*
    la de antes y no lo es: el servicio compararía el token recibido contra un SHA-256, ninguna
    invitación se aceptaría, y nadie vería un error porque el `UPDATE` de la baja «funciona».

    Un `downgrade` que rompe datos sin decirlo es peor que uno que se niega, porque el segundo
    se ve y el primero se descubre cuando un cliente no puede entrar a su workspace. Volver a
    esta versión de la migración es una decisión de despliegue, no un accident: hay que emitir
    invitaciones nuevas.
    """

    raise RuntimeError(
        "Bajar esta migración es irreversible: invitations.token_hash es un SHA-256 y no se "
        "puede deshacer a un token utilizable. Hay que volver a la revision b1c2d3e4f5a6 "
        "manualmente y reemitir las invitaciones vivas."
    )
