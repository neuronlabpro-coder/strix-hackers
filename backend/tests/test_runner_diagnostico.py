"""El fallo del runner se explica con un código, no con un `STRIX_EXECUTION_FAILED` genérico.

## Qué se está probando y por qué estas pruebas existen

El 4 de octubre un pentest falló en 0,2 s sin levantar contenedor y el panel escribió
`STRIX_EXECUTION_FAILED`. El motivo real —el worker no podía abrir el socket Docker del host,
`PermissionError(13)`— estaba a un `redis-cli` de distancia, en el resultado de la tarea de
Celery, y no se veía desde ningún sitio de la aplicación. Este fichero es la red de seguridad
de ese caso: comprueba que cada motivo conocido sale con **su** código.

## Por qué una prueba de cada motivo y no una de la tabla entera

Porque la tabla se puede leer y la clasificación se puede equivocar. El fallo que más cuesta
es `DockerException` dentro de `SandboxExecutionError`: si el recorrido de la cadena de causas
mira al revés, el motivo real se pierde y sale `STRIX_EXECUTION_FAILED`, que es exactamente el
defecto que este módulo arregla. Eso es lo que se prueba, y lo que la mutación del final
vuelve a romper.

## Por qué se prueba también el camino del panel

Porque un backend que clasifica bien y un panel que sigue mostrando el código desnudo dejan al
usuario exactamente donde estaba. `test_pentest_lifecycle_api.py` cubre la persistencia; aquí se
cubre el texto, que es la mitad que el usuario lee.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from docker.client import DockerClient
from docker.errors import DockerException, ImageNotFound

from backend.workers.runner.diagnostico import (
    CODIGO_DESCONOCIDO,
    MOTIVOS_DE_DESPLIEGUE,
    diagnosticar_fallo,
    es_fallo_de_despliegue,
)
from backend.workers.runner.egress_fence import EgressFenceMissingError
from backend.workers.runner.exceptions import (
    ContainerExecutionError,
    SandboxError,
    SandboxExecutionError,
    SandboxOutputError,
    SandboxTimeoutError,
    SandboxWorkspaceError,
)
from backend.workers.runner.llm_key_exposure import LlmKeyExposureNotAcknowledgedError
from backend.workers.runner.sandbox import StrixSandboxManager

# El error que el worker del VPS lanzo de verdad el 4 de octubre de 2026, con el codigo de la
# tarea de Celery `5404f570-e8e1-4646-909e-3c8d8dcd140c`. Va literal y no inventado porque el
# diagnostico se escribio **despues** de leerlo, y una prueba con un error de ejemplo puede
# pasar con un clasificador que no habria detectado el real.
ERROR_REAL_DEL_VPS = DockerException(
    "Error while fetching server API version: "
    "('Connection aborted.', PermissionError(13, 'Permission denied'))"
)


def _envuelto(error: BaseException) -> BaseException:
    """Reproduce el envoltorio que pone `sandbox.run`, para probar la cadena de causas."""

    envuelto = SandboxExecutionError("Falló la ejecución del sandbox Strix")
    envuelto.__cause__ = error
    return envuelto


def test_el_motivo_real_llega_a_traves_del_run_real(tmp_path: Path) -> None:
    """El camino completo: `StrixSandboxManager.run` envolviendo, y el panel viendo el motivo.

    ## Por qué esta prueba y no una que lance excepciones a mano

    Porque las dos anteriores construyen la cadena con las manos, y una prueba así no depende
    de `sandbox.run`. Si alguien quita el `from error` de los `except` de `run()` —que es
    exactamente el cambio que hace que el motivo se quede en el registro del worker y no
    llegue al panel— las pruebas de arriba seguirían en verde, y el defecto volvería sin que
    nada se cayera. Esta pasa por el `run()` de verdad, con un cliente que falla al crear la
    red como falló el worker del VPS, y por eso esa mutación sí la tumba.

    ## Por qué la red y no el contenedor

    Porque es donde falla el caso real. `PermissionError(13)` al abrir el socket sale en la
    **primera** llamada al daemon, que es `networks.create`; para cuando se llegara a
    `containers.run` el despliegue ya estaría roto de otra manera. La prueba pone el fallo
    donde lo puso el host.
    """

    cliente = MagicMock()
    cliente.networks.create.side_effect = ERROR_REAL_DEL_VPS
    gestor = StrixSandboxManager(
        run_id="11111111-1111-4111-8111-111111111111",
        target="ejemplo.test",
        scan_mode="QUICK",
        client=cast(DockerClient, cliente),
        workspace_root=tmp_path,
        image="ejemplo/strix:test",
    )

    with pytest.raises(SandboxExecutionError) as capturado:
        gestor.run(timeout_seconds=5, soft_timeout_seconds=0)

    assert diagnosticar_fallo(capturado.value).codigo == "STRIX_DOCKER_UNAVAILABLE"


def test_el_permiso_denegado_del_socket_docker_da_su_propio_codigo() -> None:
    diagnostico = diagnosticar_fallo(ERROR_REAL_DEL_VPS)

    assert diagnostico.codigo == "STRIX_DOCKER_UNAVAILABLE"


def test_el_motivo_real_gana_a_traves_del_envoltorio_del_runner() -> None:
    """El envoltorio de `run()` no puede tapar el motivo real.

    Es el caso que hacía inútil el diagnóstico: `sandbox.run` envuelve casi todo en
    `SandboxExecutionError`, así que si la cadena no se recorre el panel recibe siempre el
    mismo código y el defecto sigue igual de invisible.
    """

    assert diagnosticar_fallo(_envuelto(ERROR_REAL_DEL_VPS)).codigo == "STRIX_DOCKER_UNAVAILABLE"
    assert (
        diagnosticar_fallo(_envuelto(ContainerExecutionError("No se pudo ejecutar"))).codigo
        == CODIGO_DESCONOCIDO
    ), "sin causa underneath no hay motivo que clasificar"


def test_el_cerco_y_el_reconocimiento_de_la_clave_no_se_confunden() -> None:
    """Los dos motivos de despliegue más frecuentes necesitan códigos distintos.

    Son los dos que un despliegue se salta por descuido, y son arranglables en sitios
    opuestos —uno en el host con `iptables`, otro en la variable de entorno del Dokploy—.
    Con un solo código el operador no sabe cuál de los dos miró.
    """

    assert (
        diagnosticar_fallo(EgressFenceMissingError("sin cerco")).codigo
        == "STRIX_EGRESS_FENCE_MISSING"
    )
    assert (
        diagnosticar_fallo(LlmKeyExposureNotAcknowledgedError("sin ack")).codigo
        == "STRIX_LLM_KEY_ACK_MISSING"
    )


def test_cada_motivo_conocido_sale_con_un_codigo_propio() -> None:
    esperados = {
        ImageNotFound("no such image"): "STRIX_IMAGE_UNAVAILABLE",
        SandboxWorkspaceError("sin permisos"): "STRIX_WORKSPACE_UNAVAILABLE",
        SandboxOutputError("sin results.json"): "STRIX_OUTPUT_UNUSABLE",
        SandboxTimeoutError("tarde"): "STRIX_TIMEOUT",
    }

    for error, codigo in esperados.items():
        assert diagnosticar_fallo(error).codigo == codigo, type(error).__name__


def test_lo_desconocido_se_queda_en_el_codigo_generico() -> None:
    """Sin motivo clasificado se dice que no se sabe, en vez de inventar uno.

    Un `RuntimeError` sin clasificar es exactamente el caso en el que hay que mirar el
    registro del worker. Marcarlo con el código de otro fallo sería peor que no marcarlo:
    el panel mentiría sobre lo que sabe.
    """

    diagnostico = diagnosticar_fallo(RuntimeError("boom"))

    assert diagnostico.codigo == CODIGO_DESCONOCIDO
    assert "RuntimeError" in diagnostico.comprobacion


def test_una_cadena_de_causas_circular_no_cuelga_el_clasificador() -> None:
    """El recorrido tiene que terminar sea cual sea la excepción.

    Una cadena construida a mano puede volver al principio. Sin tope ni comprobación de
    identidad esto sería un bucle infinito en el worker, y un worker colgado es peor que un
    worker que falla: se queda el run en `RUNNING` hasta que el watchdog lo mata.
    """

    primero = SandboxExecutionError("primero")
    segundo = SandboxExecutionError("segundo")
    primero.__cause__ = segundo
    segundo.__cause__ = primero

    assert diagnosticar_fallo(primero).codigo == CODIGO_DESCONOCIDO


def test_el_motivo_mas_profundo_gana_al_envoltorio_que_tambien_se_sabe_clasificar() -> None:
    """Cuando los dos extremos de la cadena se clasifican, gana el de dentro.

    ## Por qué esta prueba existe, y qué cubre que las otras no

    Las seis pruebas anteriores nunca tienen dos elementos clasificables en la misma cadena: el
    envoltorio de `sandbox.run` es `SandboxExecutionError`, que **no** está en `MOTIVOS_CONOCIDOS`
    a propósito. Por eso el `reversed(...)` de `diagnosticar_fallo` no lo cubría ninguna: quitarlo
    dejaba los 17 tests en verde, que es la forma más comfortable de tener código muerto.

    El caso que lo distingue necesita un envoltorio que la lista **sí** sepa clasificar. El más
    real es `ImageNotFound` —de Docker— dentro de `EgressFenceMissingError`, que es como se ve
    cuando el cerco se comprueba y lo que falla por debajo es una llamada al demonio: el
    operador tiene dos motivos distintos en la mano y solo puede escribir uno.

    Por qué gana el de dentro: el envoltorio es el síntoma y el motivo es la causa, y el motivo es
    el que se arregla. Al revés, el código sería el del socket de Docker y mandaría a mirar el
    demonio cuando lo declarado es la regla de `iptables` que falta.

    ## Y por qué esto sigue siendo protección del futuro

    Porque hoy `SandboxExecutionError` no está en la lista. Cuando alguien añada un tipo base a
    `MOTIVOS_CONOCIDOS` —que es tentador, porque parece una simplificación— esta prueba es la que
    dice que el recorrido al revés sigue siendo lo que impide que ese tipo absorba los motivos
    concretos. Si alguien añade el tipo base **y** quita el `reversed`, esta prueba cae.
    """

    causa = ImageNotFound("no such image")
    envuelto = EgressFenceMissingError("sin cerco")
    envuelto.__cause__ = causa

    assert diagnosticar_fallo(envuelto).codigo == "STRIX_IMAGE_UNAVAILABLE"


def test_el_recorrido_sigue_protegiendo_contra_ciclos_sin_importar_nada_nuevo() -> None:
    """El tope y la identidad siguen siendo la garantía de que la función termina.

    ## Por qué está junto a la prueba del orden y no en un fichero aparte

    Porque las dos defienden la misma línea de código —`_cadena_de_causas`— y separarlas haría
    que quien lea una tuviera que mirar la otra para saber qué garantiza el conjunto. Se puede
    quitar el `reversed` sin tocar esta, y se puede quitar el tope sin tocar la otra; quitando
    las dos cosas el clasificador vuelve al defecto y las dos pruebas caen.
    """

    primero = SandboxExecutionError("primero")
    segundo = SandboxOutputError("segundo")
    primero.__cause__ = segundo
    segundo.__cause__ = primero

    # Con la cadena circular el `SandboxOutputError` es clasificable, así que la prueba **sí**
    # depende del tope: sin él, el recorrido no terminaría nunca.
    assert diagnosticar_fallo(primero).codigo == "STRIX_OUTPUT_UNUSABLE"


def test_los_motivos_de_despliegue_no_se_reintentan_con_otro_modelo() -> None:
    """Un host sin cerco falla igual con los cinco modelos del catálogo.

    Es la razón de que estos códigos existan: `_FALLBACK_ELIGIBLE_ERRORS` se decide por el
    código, así que un motivo de despliegue sale del conjunto por construcción y no se
    gasta la cola reintentando lo que no puede funcionar.
    """

    for error in (
        EgressFenceMissingError("sin cerco"),
        LlmKeyExposureNotAcknowledgedError("sin ack"),
        ImageNotFound("no such image"),
        SandboxWorkspaceError("sin permisos"),
        ERROR_REAL_DEL_VPS,
    ):
        assert es_fallo_de_despliegue(error), type(error).__name__
        assert diagnosticar_fallo(error).codigo in MOTIVOS_DE_DESPLIEGUE

    assert not es_fallo_de_despliegue(SandboxOutputError("sin results.json")), (
        "un results.json ilegible puede mejorar con otro modelo, asi que no es de despliegue"
    )


def test_sandbox_error_a_secas_no_se_confunde_con_sus_derivados() -> None:
    """La base de la jerarquía se clasifica como desconocida, no como la primera derivada.

    `SandboxError` es la base de cinco fallos con causas distintas. Si estuviera en la
    tabla, `isinstance` la encontraría primero para todos ellos —el orden de la tupla lo
    pone por delante— y volveríamos al defecto original con más donde elegir.
    """

    diagnostico = diagnosticar_fallo(SandboxError("sin clasificar"))

    assert diagnostico.codigo == CODIGO_DESCONOCIDO


@pytest.mark.parametrize(
    "codigo",
    sorted(MOTIVOS_DE_DESPLIEGUE | {CODIGO_DESCONOCIDO, "STRIX_OUTPUT_UNUSABLE", "STRIX_TIMEOUT"}),
)
def test_todo_codigo_producido_tiene_explicacion_en_el_panel(codigo: str) -> None:
    """Cada código que el backend puede escribir tiene su texto en los dos idiomas.

    Sin esta comprobación, un motivo nuevo sale en el panel como la cadena cruda —que es
    justo lo que se quería arreglar— y nadie se entera hasta que alguien lo ve en una
    pantalla. Se lee el JSON de traducciones del panel, no una constante del frontend: es el
    contrato entre backend y frontend, y lo que se rompe es ese contrato.
    """

    import json
    from pathlib import Path

    raices = Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"
    for idioma in ("es", "en"):
        ruta = raices / idioma / "pentests.json"
        if not ruta.exists():  # pragma: no cover - solo si el arbol esta incompleto
            pytest.skip(f"no esta el arbol de traducciones: {ruta}")
        claves = json.loads(ruta.read_text(encoding="utf-8"))["run"]["fallos"]
        assert codigo in claves, f"{idioma}: falta la explicacion de {codigo}"
