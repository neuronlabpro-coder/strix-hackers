"""Pruebas del parser OKF.

## Qué se comprueba y por qué

El parser decide si un documento se guarda o no, así que cada rama tiene que estar probada por
el motivo que la hace existir:

- El frontmatter **es** obligatorio, y el error nombra el campo que falta.
- Las dos grafías de `type` —la del formato y la del enum de la base— producen la misma fila.
- Las dos grafías de lista de `tags` se aceptan, porque la especificación y la gente escriben
  distinto.
- El cuerpo se conserva **sin** el frontmatter, porque es lo que se indexa y lo que se cita.
- Los enlaces internos se recogen, y solo los internos.
"""

from __future__ import annotations

import pytest

from backend.apps.knowledge.documents import KnowledgeDocTypeEnum
from backend.apps.knowledge.okf import OkfError, parse_okf

pytestmark = pytest.mark.asyncio

COMPLETO = '''---
type: business_rule
title: "El campo legacy_id es un marcador fijo"
description: "No es un identificador de usuario y no debe indexarse como tal."
tags: [auth, payments, internal]
---

El campo `legacy_id` se lleno con el mismo valor en toda la tabla y se mantiene por
compatibilidad con una migracion de 2019.

Se lee pero no se usa para autorizar nada. Ver [Politica de acceso](/documents/politica-acceso).
'''


async def test_parsea_un_documento_completo() -> None:
    documento = parse_okf(COMPLETO)
    assert documento.doc_type is KnowledgeDocTypeEnum.BUSINESS_RULE
    assert documento.title == "El campo legacy_id es un marcador fijo"
    assert "No es un identificador" in documento.description
    assert documento.tags == ("auth", "payments", "internal")


async def test_el_cuerpo_no_lleva_el_frontmatter() -> None:
    """Lo que se indexa y lo que se cita es el cuerpo, sin la cabecera.

    Si el frontmatter se colara en el cuerpo, cada documento indexado llevaria su propio titulo
    repetido al principio, y el modelo lo veria como parte del contenido en lugar de como
    metadato.
    """

    documento = parse_okf(COMPLETO)
    assert not documento.body.startswith("---")
    assert "type: business_rule" not in documento.body
    assert documento.body.startswith("El campo `legacy_id`")


async def test_recoge_los_enlaces_internos_y_solo_ellos() -> None:
    """Los enlaces a otros documentos se anotan; los externos no.

    Un enlace a una pagina web no es navegacion dentro de la base de conocimiento: seguirlo
    exigiria salir a Internet desde el servidor, y el documento al que apunta no esta en el
    prompt de ningun modo.
    """

    documento = parse_okf(
        COMPLETO
        + "\n\nTambien aplica a [la documentacion](https://ejemplo.com/docs) externa.\n"
    )
    assert documento.links == ("politica-acceso",)


# --------------------------------------------------------------------------- #
# Los tipos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("escrito", "esperado"),
    [
        ("concept", KnowledgeDocTypeEnum.DOCUMENTATION),
        ("business_rule", KnowledgeDocTypeEnum.BUSINESS_RULE),
        ("api_spec", KnowledgeDocTypeEnum.API_SPEC),
        ("architecture", KnowledgeDocTypeEnum.ARCHITECTURE),
        # La grafia del enum de la base, que es lo que escribe el selector del panel.
        ("BUSINESS_RULE", KnowledgeDocTypeEnum.BUSINESS_RULE),
        ("API_SPEC", KnowledgeDocTypeEnum.API_SPEC),
        ("ARCHITECTURE", KnowledgeDocTypeEnum.ARCHITECTURE),
        # La grafia de la columna, que aparece en el listado de documentos.
        ("DOCUMENTATION", KnowledgeDocTypeEnum.DOCUMENTATION),
    ],
)
async def test_todas_las_grafias_de_type_dan_la_misma_fila(
    escrito: str, esperado: KnowledgeDocTypeEnum
) -> None:
    """Las dos fuentes de verdad producen el mismo enum de columna.

    Un documento pegado desde la especificacion del formato y uno escrito desde el panel tienen
    que acabar en la misma fila. Si no, el filtro por tipo deja de encontrar uno de los dos.
    """

    documento = parse_okf(
        f'---\ntype: {escrito}\ntitle: "T"\ndescription: "D"\n---\n\nCuerpo.\n'
    )
    assert documento.doc_type is esperado


# --------------------------------------------------------------------------- #
# Las etiquetas
# --------------------------------------------------------------------------- #


async def test_acepta_las_dos_grafias_de_etiquetas() -> None:
    """Con corchetes y sin corchetes.

    La especificacion OKF escribe `[auth, payments]` y la gente pega `auth, payments`. Exigir una
    concreta rechazaria documentos por un detalle de puntuacion.
    """

    con = parse_okf(
        '---\ntype: concept\ntitle: "T"\ndescription: "D"\n'
        "tags: [auth, payments]\n---\n\nCuerpo.\n"
    )
    sin = parse_okf(
        '---\ntype: concept\ntitle: "T"\ndescription: "D"\ntags: auth, payments\n---\n\nCuerpo.\n'
    )
    assert con.tags == sin.tags == ("auth", "payments")


async def test_normaliza_las_etiquetas_a_minuscula_y_sin_repetidos() -> None:
    """`Auth`, `auth` y `AUTH` son la misma etiqueta.

    Sin normalizar, un documento etiquetado `Auth` y otro `auth` son dos etiquetas distintas, y
    el filtro por etiqueta encuentra uno de los dos. Es la clase de bug que solo aparece cuando
    hay dos personas etiquetando.
    """

    documento = parse_okf(
        '---\ntype: concept\ntitle: "T"\ndescription: "D"\n'
        "tags: [Auth, auth, AUTH, payments]\n---\n\nCuerpo.\n"
    )
    assert documento.tags == ("auth", "payments")


async def test_etiquetas_entre_comillas_con_espacio() -> None:
    """Una etiqueta puede llevar espacio si va entrecomillada."""

    documento = parse_okf(
        '---\ntype: concept\ntitle: "T"\ndescription: "D"\n'
        'tags: ["datos personales", auth]\n---\n\nCuerpo.\n'
    )
    assert documento.tags == ("datos personales", "auth")


# --------------------------------------------------------------------------- #
# Los errores
# --------------------------------------------------------------------------- #


async def test_sin_frontmatter_da_error_que_dice_que_hace_falta() -> None:
    """El mensaje dice qué escribir, no solo que algo va mal.

    Un error de "frontmatter invalido" obliga a abrir la especificacion del formato; uno que
    dice "entre lineas de tres guiones" se resuelve en el sitio.
    """

    with pytest.raises(OkfError, match="tres guiones"):
        parse_okf("# Solo markdown\n\nSin cabecera.")


async def test_sin_type_da_error_que_nombra_el_campo() -> None:
    """`type` es obligatorio porque sin el el documento no se puede recuperar por tipo.

    Es el unico campo sin valor por defecto razonable: `DOCUMENTATION` seria inventar el tipo
    de un documento que el cliente no ha dicho de que es.
    """

    with pytest.raises(OkfError, match="`type`"):
        parse_okf('---\ntitle: "T"\ndescription: "D"\n---\n\nCuerpo.\n')


async def test_sin_title_da_error() -> None:
    with pytest.raises(OkfError, match="`title`"):
        parse_okf('---\ntype: concept\ndescription: "D"\n---\n\nCuerpo.\n')


async def test_sin_description_da_error() -> None:
    """La descripcion es lo que se inyecta cuando el documento se cita sin su cuerpo.

    Sin ella, un documento recuperado por similitud se citaria por su titulo y el modelo no
    sabria de que trata. Es el campo mas barato y el que mas mejora la respuesta.
    """

    with pytest.raises(OkfError, match="`description`"):
        parse_okf('---\ntype: concept\ntitle: "T"\n---\n\nCuerpo.\n')


async def test_frontmatter_sin_cierre_da_error() -> None:
    """Un frontmatter abierto y nunca cerrado es el error de pegado más frecuente."""

    with pytest.raises(OkfError, match="no se cierra"):
        parse_okf('---\ntype: concept\ntitle: "T"\ndescription: "D"\n\nCuerpo sin cerrar.\n')


async def test_cuerpo_vacio_da_error() -> None:
    """Un documento con cabecera y sin cuerpo no aporta nada.

    Se acepta en el guardado solo si hay texto: un frontmatter beautiful con el cuerpo vacío es
    un documento que el agente recuperará y no podrá usar.
    """

    with pytest.raises(OkfError, match="cuerpo"):
        parse_okf('---\ntype: concept\ntitle: "T"\ndescription: "D"\n---\n\n   \n')


async def test_type_desconocido_lista_los_admitidos() -> None:
    """El error de un tipo malo dice cuáles son válidos.

    Es la diferencia entre un `422` que obliga a mirar la especificación y uno que obliga a
    probar cuatro valores a mano.
    """

    with pytest.raises(OkfError, match="business_rule"):
        parse_okf('---\ntype: politica\ntitle: "T"\ndescription: "D"\n---\n\nCuerpo.\n')
