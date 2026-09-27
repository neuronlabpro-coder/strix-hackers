"""Parser y validador del formato documental OKF (*Open Knowledge Format*).

## Qué problema resuelve

El contexto que se inyecta en el prompt del chat sale de documentos que escribe el cliente: sus
reglas de negocio, su arquitectura, sus especificaciones de API. Ese texto se pega tal cual en
`content`, sin estructura, y un bloque de texto sin estructura no se puede recuperar por partes,
ni citar, ni saber de qué trata sin leerlo entero.

El frontmatter resuelve las tres cosas a la vez: **qué es** este documento, **de qué trata** y
**con qué etiquetas** se relaciona. El cuerpo sigue siendo Markdown legible por una persona, que
es lo que lo hace mantenible: el mismo fichero que lee el cliente en un editor es el que se
indexa.

## Por qué el parser es **estricto** en el frontmatter y **permisivo** en el cuerpo

Porque los dos fallan de forma distinta. Un frontmatter sin `type` impide clasificar el documento
y no hay forma de recuperarlo después: es un fallo al escribir. Un cuerpo con un enlace a otro
documento que no existe, o con Markdown roto, se puede recuperar igual y el modelo lo descarta
solo; rechazarlo en el guardado sería impedir documentar algo por un detalle de formato.

Por eso el guardado devuelve un error con el campo concreto cuando el frontmatter no vale, y
acepta cualquier cuerpo que sea texto no vacío.

## Por qué `type` usa los valores del enum y no los del OKF

El OKF escribe `business_rule` y `api_spec` con guion bajo. La columna es un enum de
PostgreSQL con `BUSINESS_RULE` y `API_SPEC`. El parser acepta **las dos** escrituras y devuelve
el valor del enum, de forma que un documento pegado desde la especificación del formato y uno
escrito por alguien del equipo producen exactamente la misma fila.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from backend.apps.knowledge.documents import KnowledgeDocTypeEnum

#: La valla del frontmatter. Se exige la de tres guiones porque es la que escribe la
#: especificacion OKF y la que entiende el resto del ecosistema de Markdown. Aceptar `---` de
#: cuatro o mas permitiria un separador de tabla de contenido al principio del cuerpo.
_DELIMITADOR = re.compile(r"\A---[ \t]*\r?\n", re.MULTILINE)

#: El cuerpo empieza despues del cierre del frontmatter.
_CIERRE = re.compile(r"\r?\n---[ \t]*(?:\r?\n|\Z)")

#: `clave: valor` en la cabecera. Se admite el `:` del valor con espacios, que es lo que lleva
#: un titulo entrecomillado, y solo en las lineas que no empiezan por `-` para no leer como
#: clave la primera palabra de una lista.
_CLAVE = re.compile(r"^(?P<clave>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<valor>.*?)\s*$")

#: Una etiqueta es un token sin espacios, o una cadena entre comillas.
#:
#: La alternancia y no unas comillas opcionales a los dos lados, porque eso parte la etiqueta
#: por el espacio: sobre `"datos personales"` la comillas inicial se consumia como opcional, el
#: grupo cogia `datos`, la comillas final se consumia como opcional y el resto de la etiqueta
#: salia como dos etiquetas mas. El fallo es silencioso —no da error, devuelve tres etiquetas en
#: vez de una— y por eso la alternativa es explicita.
_ETIQUETA = re.compile(r'"([^"]+)"|([A-Za-z0-9_./+-]+)')


class OkfError(ValueError):
    """El frontmatter no cumple el formato.

    El mensaje nombra el campo. Un error que dice "frontmatter invalido" obliga a abrir el
    documento y buscar; uno que dice "falta `type`" dice qué escribir.
    """


class OkfTypeEnum(StrEnum):
    """Los `type` del frontmatter, en la grafía de la especificación.

    Son un enum **distinto** del de la base de datos a propósito: son dos contratos distintos y
    no deben compartir tipo. El de la base es un enum de PostgreSQL que no puede cambiar sin
    migración; este es el vocabulario del formato, que puede crecer con la especificación sin
    tocar el esquema.
    """

    CONCEPT = "concept"
    BUSINESS_RULE = "business_rule"
    API_SPEC = "api_spec"
    ARCHITECTURE = "architecture"


#: Del `type` del OKF al enum de la columna. Sin esta tabla, el parser devolvería un `str` y la
#: translation a enum se decidiría en el servicio, que es donde se decide qué hacer con un valor
#: que no existe.
MAPA_DE_TIPOS: dict[OkfTypeEnum, KnowledgeDocTypeEnum] = {
    OkfTypeEnum.CONCEPT: KnowledgeDocTypeEnum.DOCUMENTATION,
    OkfTypeEnum.BUSINESS_RULE: KnowledgeDocTypeEnum.BUSINESS_RULE,
    OkfTypeEnum.API_SPEC: KnowledgeDocTypeEnum.API_SPEC,
    OkfTypeEnum.ARCHITECTURE: KnowledgeDocTypeEnum.ARCHITECTURE,
}

#: Se acepta la grafía de la columna ademas de la del formato, para que un documento escrito
#: desde el panel —donde el selector dice `BUSINESS_RULE`— tambien valide.
_TIPOS_ACEPTADOS: dict[str, OkfTypeEnum] = {
    **{tipo.value: tipo for tipo in OkfTypeEnum},
    "documentation": OkfTypeEnum.CONCEPT,
    **{miembro.name.lower(): miembro for miembro in OkfTypeEnum},
}


@dataclass(frozen=True)
class OkfDocument:
    """Un documento OKF ya parseado."""

    doc_type: KnowledgeDocTypeEnum
    title: str
    description: str
    tags: tuple[str, ...] = ()
    #: El cuerpo **sin** el frontmatter. Es lo que se indexa y lo que se cita.
    body: str = ""
    #: Los enlaces a otros documentos que se han encontrado en el cuerpo, por si hace falta
    #: seguir la navegacion sin volver a parsear.
    links: tuple[str, ...] = field(default_factory=tuple)


#: Enlaces de la forma `[texto](/documents/slug)`. Se recogen solo con esa ruta, que es la que
#: el chat genera al citar, y no con enlaces http: un documento que enlaza a una URL externa
#: no esta "enlazando otro documento del knowledge base".
_ENLACE_INTERNO = re.compile(r"\[[^\]]*\]\(/documents/([^)\s#]+)\)")


def parse_okf(contenido: str) -> OkfDocument:
    """Parsea un documento OKF.

    ## Por qué el frontmatter es **obligatorio** y no opcional con valor por defecto

    Porque un documento sin `type` no se puede clasificar, y un documento sin clasificar no se
    puede recuperar por tipo. La unica forma de recuperarlo seria por texto completo, que es lo
    que hace la degradacion cuando no hay embeddings — es decir, peor. Aceptarlo aqui crearia
    filas que el sistema no sabe usar, y seria mas dificilarlas despues porque el usuario ya
    las ve en su listado.
    """

    bruto = contenido.lstrip("﻿")
    cabecera = _DELIMITADOR.match(bruto)
    if cabecera is None:
        raise OkfError(
            "El documento necesita un bloque de frontmatter OKF al principio, entre lineas "
            "de tres guiones."
        )

    cuerpo_bruto = bruto[cabecera.end() :]
    cierre = _CIERRE.search(cuerpo_bruto)
    if cierre is None:
        raise OkfError("El frontmatter no se cierra: falta la linea de tres guiones del final.")

    campos = _leer_campos(cuerpo_bruto[: cierre.start()])
    cuerpo = cuerpo_bruto[cierre.end() :].strip()

    tipo = _leer_tipo(campos)
    titulo = _leer_texto(campos, "title")
    descripcion = _leer_texto(campos, "description")

    if not cuerpo:
        raise OkfError(
            "El cuerpo del documento esta vacio. Un documento con frontmatter y sin contenido "
            "no aporta nada al contexto del chat."
        )

    return OkfDocument(
        doc_type=MAPA_DE_TIPOS[tipo],
        title=titulo,
        description=descripcion,
        tags=_leer_etiquetas(campos.get("tags", "")),
        body=cuerpo,
        links=tuple(dict.fromkeys(_ENLACE_INTERNO.findall(cuerpo))),
    )


def _leer_campos(bloque: str) -> dict[str, str]:
    """Los pares `clave: valor` de la cabecera.

    Se ignoran las lineas de comentario (`#`) y los bloques de lista indentados, que son el
    valor de la clave anterior. Es un parser deliberadamente **pequeno**: `yaml.safe_load`
    haria mas de lo que hace falta y abriria la puerta a una etiqueta de Python dentro de un
    documento pegado por un cliente, que es un problema de seguridad en un parser de YAML usado
    con entrada no confiable.
    """

    campos: dict[str, str] = {}
    clave_actual: str | None = None
    for linea in bloque.splitlines():
        if not linea.strip() or linea.lstrip().startswith("#"):
            continue
        emparejada = _CLAVE.match(linea)
        if emparejada is not None:
            # `group` devuelve `str | None` en la firma de tipos de `re`, aunque el grupo exista
            # cuando `match` ha devuelto algo. Se comprueba en vez de forzar el tipo con un
            # `cast`: si el grupo no estuviera, el `if` lo diria y el documento se parsearia sin
            # esa clave en vez de reventar con un `None` como valor.
            clave = emparejada.group("clave")
            valor = emparejada.group("valor")
            if clave is None or valor is None:
                continue
            clave_actual = clave
            campos[clave] = valor
            continue
        # Una linea mas profunda continua el valor de la clave anterior: es la forma de
        # escribir un `tags:` a lo largo de varias lineas.
        if clave_actual is not None and linea[:1].isspace():
            # El `cast` no hace falta: la comprobacion de arriba ya estrecha el tipo, y el
            # aviso de pyright viene de que la variable se reasigna dentro del bucle. Se
            # copia a un nombre local para que el estrechamiento sea visible para quien
            # lea, y no una asercion invisible en medio de una cadena.
            clave = clave_actual
            campos[clave] = f"{campos[clave]} {linea.strip()}".strip()
    return campos


def _leer_tipo(campos: dict[str, str]) -> OkfTypeEnum:
    crudo = campos.get("type", "").strip().strip("\"'").lower()
    if not crudo:
        raise OkfError("El frontmatter necesita `type`.")
    tipo = _TIPOS_ACEPTADOS.get(crudo)
    if tipo is None:
        admitidos = ", ".join(sorted(t.value for t in OkfTypeEnum))
        raise OkfError(f"`type: {crudo}` no es un tipo OKF. Admitidos: {admitidos}.")
    return tipo


def _leer_texto(campos: dict[str, str], clave: str) -> str:
    valor = campos.get(clave, "").strip().strip("\"'").strip()
    if not valor:
        raise OkfError(f"El frontmatter necesita `{clave}`.")
    return valor


def _leer_etiquetas(crudo: str) -> tuple[str, ...]:
    """Las etiquetas, en las dos grafías de lista que se ven en la práctica.

    `[auth, payments]` y `auth, payments`. Se admiten ambas porque la primera es la que escribe
    la especificación y la segunda es la que pega la gente; exigir una concreta rechazaría
    documentos por un detalle de puntuacion.
    """

    limpio = crudo.strip()
    if limpio.startswith("[") and limpio.endswith("]"):
        limpio = limpio[1:-1]
    etiquetas: list[str] = []
    for emparejada in _ETIQUETA.finditer(limpio):
        # Solo uno de los dos grupos casa: el entrecomillado o el simple. Se cogen los dos y se
        # descarta el `None`, en vez de asumir cual es, para que anadir una forma nueva al
        # patron no tenga que acordarse de tocar aqui.
        valor = emparejada.group(1) or emparejada.group(2)
        if valor is not None:
            etiquetas.append(valor.lower())
    return tuple(dict.fromkeys(etiquetas))
