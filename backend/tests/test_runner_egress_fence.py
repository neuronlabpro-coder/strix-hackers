"""El cerco de salida se comprueba, se nega a pasar sin el, y cubre la red correcta.

## Que hace el control

El runner crea una red bridge por trabajo y el contenedor sale de ella con los permisos de
red del host: los metadatos de la nube, la red de Tailscale, Redis y PostgreSQL del despliegue.
El motor necesita salir —clonar, resolver, llamar al proveedor—, asi que la red no se puede
declarar `internal`. Lo que se quiere es que solo salga a DNS y HTTPS, y eso lo instala
`scripts/harden_runner_egress.sh` en la cadena `DOCKER-USER` del host.

El runner **comprueba** que el cerco esta puesto antes de arrancar el contenedor, y se niega a
lanzarlo si no esta. El aviso en el log no sirve: un despliegue sin el script tendria un
contenedor con salida completa y una linea en un registro que nadie lee.

## El fallo que mas importa que no pase

Dar por protegido un trabajo que **no** lo esta. Concretamente, que haya una regla de DROP en
la cadena y que la comprobacion la de por buena aunque no cubra la subred de este trabajo: un
`DROP` instalado para `172.31.0.0/16` no protege un trabajo en `172.20.0.0/16`, y aceptarlo
seria un fallo de seguridad que se announce como una proteccion. Por eso se compara **por
red** y se exige que la subred este dentro de lo que la regla cubre.

## Por que se falsea `iptables` en vez de invocarlo

Porque las reglas las instala un script del host que no se puede ejecutar en la bateria —no
hay demonio de Docker, y en Windows no hay `iptables`—, y porque lo que se decide es **que
hace el runner con el texto que lee**, que es logica pura y se puede probar entera. El
`subprocess` real se aísla en una sola funcion, y su fallo —no encontrar `iptables`— se prueba
tambien, porque "no hay iptables" tiene que significar "sin cerco", no "excepcion" ni "paso".
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from backend.core.config import settings
from backend.workers.runner import egress_fence
from backend.workers.runner.egress_fence import (
    EgressFenceMissingError,
    exigir_cerco_de_salida,
    hay_cerco_para,
    subred_de_la_red,
)

# Las reglas que deja el script cuando el cerco esta bien puesto.
# Las reglas se componen en vez de escribirse enteras, porque las lineas de `iptables -S`
# con el comentario al final se pasan de cien columnas y partir una cadena de literales a
# mano acaba en una regla mal formada que la prueba ya no representa.
COMENTARIO = f'--comment "{egress_fence.COMENTARIO_DEL_SCRIPT}"'
REGLAS_CON_CERCO = (
    f'-A DOCKER-USER -s 172.31.0.0/16 -p udp --dport 53 -j RETURN -m {COMENTARIO}',
    f'-A DOCKER-USER -s 172.31.0.0/16 -p tcp --dport 443 -j RETURN -m {COMENTARIO}',
    f'-A DOCKER-USER -s 172.31.0.0/16 -j DROP -m {COMENTARIO}',
)

# El caso peligroso: hay DROP, y no cubre esta subred.
REGLAS_DE_OTRA_RED = (
    f'-A DOCKER-USER -s 10.10.0.0/16 -j DROP -m {COMENTARIO}',
)


# El interruptor se pasa por parametro en vez de leerse de `Settings`, que es congelado. Es el
# mismo motivo por el que `llm_router.client.build_chat_url` recibe la base como parametro: mutar
# el global exigiria `object.__setattr__`, que es justo lo que la inmutabilidad evita. Por eso
# `exigir_cerco_de_salida` tiene `requerido` y `reglas` como parametros, y estas pruebas los
# usan en vez de tocar el entorno.


# --------------------------------------------------------------------------- #
# hay_cerco_para
# --------------------------------------------------------------------------- #


def test_reconoce_el_cerco_bien_puesto() -> None:
    assert hay_cerco_para("172.31.0.0/24", REGLAS_CON_CERCO)
    assert hay_cerco_para("172.31.7.0/24", REGLAS_CON_CERCO), (
        "una subred mas pequena dentro de la cubierta tambien esta protegida"
    )


def test_no_da_por_protegido_un_trabajo_que_el_cerco_no_cubre() -> None:
    """El fallo que mas importa que no pase: un DROP que no alcanza a esta red."""

    assert not hay_cerco_para("172.20.0.0/24", REGLAS_DE_OTRA_RED), (
        "un DROP para 10.10.0.0/16 no protege un trabajo en 172.20.0.0/24, y darlo por "
        "protegido seria un fallo de seguridad anunciado como una proteccion"
    )


def test_sin_reglas_no_hay_cerco() -> None:
    """Ausencia de reglas significa ausencia de proteccion, nunca excepcion."""

    assert not hay_cerco_para("172.31.0.0/24", ())
    assert not hay_cerco_para("172.31.0.0/24", ("-A DOCKER-USER -j ACCEPT",))


def test_un_drop_de_otro_script_no_cuenta() -> None:
    """El cerco tiene que ser el de este despliegue.

    Un DROP que puso otra cosa en la misma cadena no se sabe si cubre lo que este trabajo
    necesita. Se exige el comentario del script, que es lo que lo identifica.
    """

    reglas = (
        '-A DOCKER-USER -s 172.31.0.0/16 -j DROP -m comment --comment "otro-script"',
    )
    assert not hay_cerco_para("172.31.0.0/24", reglas)


def test_un_return_sin_drop_no_cuenta() -> None:
    """Solo permitir con `RETURN` no acota nada, porque `RETURN` deja pasar hacia Docker.

    Es el motivo de que la comprobacion busque el `DROP` de la cola y no un `RETURN` de las
    reglas de permitidos.
    """

    reglas = (f'-A DOCKER-USER -s 172.31.0.0/16 -p tcp --dport 443 -j RETURN -m {COMENTARIO}',)
    assert not hay_cerco_para("172.31.0.0/24", reglas)


def test_una_subred_mal_escrita_no_pasa() -> None:
    assert not hay_cerco_para("no-es-una-subred", REGLAS_CON_CERCO)


def test_se_compara_por_red_y_no_por_cadena() -> None:
    """`172.31.0.1/24` y `172.31.0.0/24` son la misma red y se comparan distinto."""

    reglas = (f'-A DOCKER-USER -s 172.31.0.0/24 -j DROP -m {COMENTARIO}',)
    assert hay_cerco_para("172.31.0.1/24", reglas)


# --------------------------------------------------------------------------- #
# exigir_cerco_de_salida
# --------------------------------------------------------------------------- #


def test_se_niega_a_lanzar_sin_cerco() -> None:
    with pytest.raises(EgressFenceMissingError) as error:
        exigir_cerco_de_salida("172.31.0.0/24", requerido=True, reglas=())

    mensaje = str(error.value)
    # El mensaje tiene que decir **que** hacer, no solo que falta algo.
    assert "harden_runner_egress.sh" in mensaje, (
        f"el mensaje no dice como se instala el cerco: {mensaje}"
    )
    assert "STRIX_REQUIRE_EGRESS_FENCE=false" in mensaje, (
        f"el mensaje no dice como se trabaja en local: {mensaje}"
    )


def test_se_niega_a_lanzar_si_no_se_conoce_la_subred() -> None:
    """Sin subred no se puede afirmar nada, y afirmar sin comprobar es peor que no comprobar."""

    with pytest.raises(EgressFenceMissingError) as error:
        exigir_cerco_de_salida(None, requerido=True, reglas=REGLAS_CON_CERCO)
    assert "subred" in str(error.value)


def test_pasa_con_el_cerco_puesto() -> None:
    exigir_cerco_de_salida("172.31.0.0/24", requerido=True, reglas=REGLAS_CON_CERCO)


def test_con_el_interruptor_apagado_no_se_niega() -> None:
    """En desarrollo no hay iptables, y sin interruptor no se podria ni probar un escaneo."""

    exigir_cerco_de_salida("172.31.0.0/24", requerido=False, reglas=())  # no debe lanzar
    exigir_cerco_de_salida(None, requerido=False, reglas=())  # tampoco


def test_el_valor_por_defecto_del_interruptor_es_exigir() -> None:
    """Si el valor por defecto fuera no exigir, la proteccion se desactivaria sola.

    Un despliegue en el que nadie ha decidido nada tendria el contenedor con salida completa y
    no habria ningun sintoma. Ese es el modo de fallo que un interruptor de seguridad no puede
    tener, asi que el valor por defecto tiene que ser exigir.
    """

    # Se lee del campo del modelo, no del objeto ya construido, que la prueba anterior pudo
    # haber modificado.
    campo = settings.model_fields["strix_require_egress_fence"]
    assert campo.default is True, (
        "STRIX_REQUIRE_EGRESS_FENCE deberia exigir el cerco por defecto; si por defecto no se "
        "exige, un despliegue recien hecho no tendria proteccion y nadie lo notaria"
    )


# --------------------------------------------------------------------------- #
# subred_de_la_red
# --------------------------------------------------------------------------- #


class _RedFalsa:
    def __init__(self, attrs: Any) -> None:
        self.attrs = attrs


@pytest.mark.parametrize(
    ("attrs", "esperado"),
    [
        ({"IPAM": {"Config": [{"Subnet": "172.31.0.0/24"}]}}, "172.31.0.0/24"),
        ({"IPAM": {"Config": []}}, None),
        ({"IPAM": {}}, None),
        ({}, None),
    ],
)
def test_la_subred_se_lee_de_la_red_o_se_declara_desconocida(
    attrs: dict[str, Any], esperado: str | None
) -> None:
    assert subred_de_la_red(_RedFalsa(attrs)) == esperado


# --------------------------------------------------------------------------- #
# La lectura de iptables
# --------------------------------------------------------------------------- #


def test_sin_iptables_no_hay_cerco_y_no_excepcion(monkeypatch: pytest.MonkeyPatch) -> None:
    """La ausencia de `iptables` tiene que leerse como "sin proteccion", no como un fallo.

    Si lanzara excepcion, un host sin `iptables` no arrancaria el worker y el sintoma seria
    "el worker no arranca", que no dice nada del cerco. Y si devolviera reglas vacias sin
    distinguirlo de "no hay reglas", que es lo mismo.
    """

    def falla(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("iptables")

    monkeypatch.setattr(subprocess, "run", falla)
    assert egress_fence.reglas_de_la_cadena() == ()


def test_una_salida_vacia_es_una_cadena_vacia(monkeypatch: pytest.MonkeyPatch) -> None:
    """`iptables -S DOCKER-USER` en una cadena vacia sale con codigo 0 y sin reglas."""

    class _Resultado:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Resultado())
    assert egress_fence.reglas_de_la_cadena() == ()


def test_un_codigo_de_salida_distinto_de_cero_no_es_cerco(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resultado:
        returncode = 1
        stdout = REGLAS_CON_CERCO[0]

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Resultado())
    assert egress_fence.reglas_de_la_cadena() == (), (
        "una cadena que iptables no puede listar no se puede leer como un cerco puesto"
    )
