"""El resumen de la cabecera: los números de `/containers` y de `/networks`.

## Por qué esto necesita pruebas propias

Porque es la clase de código donde un número **miente sin error**: nada falla, la pantalla se
pinta, y lo que se pinta es un agregado mal contado. Un KPI que dice cuatro paquetes donde hay
cuarenta no rompe nada —no hay excepción, no hay status de error— y un cliente lo lee como
verdad. La única defensa es comprobar los números contra resultados conocidos.

## Qué se comprueba

- Que los dos resúmenes son **independientes**: el de redes no cuenta imágenes y el de
  contenedores no cuenta redes. Es el motivo de que existan dos rutas.
- Que el recuento sale del `JSONB` que **escribió otro despliegue**, cuya forma no está
  garantizada. Un resultado con la forma inesperada tiene que contar como «no se ha podido
  leer», no como cero.
- Que la serie diaria no tiene huecos: un día sin escaneo es un cero, no un día que falta.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import (
    AgentJob,
    AgentJobKindEnum,
    AgentJobStatusEnum,
    ScannerAgent,
)
from backend.apps.agents.service import huella_resultado
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(
    session: AsyncSession, *, nombre: str = "Resumen"
) -> tuple[Organization, User, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{nombre} {suffix}",
        slug=f"resumen-{suffix}",
        plan_tier=PlanTierEnum.PRO,
        credit_balance=0,
    )
    user = User(
        email=f"resumen-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Resumen User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    return organization, user, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _trabajo(
    organization: Organization,
    kind: AgentJobKindEnum,
    objetivo: str,
    resultado: object | None,
    *,
    status: AgentJobStatusEnum = AgentJobStatusEnum.COMPLETED,
) -> AgentJob:
    return AgentJob(
        organization_id=organization.id,
        kind=kind,
        target=objetivo,
        status=status,
        result=resultado,
        result_digest=_huella(resultado),
    )


def _huella(resultado: object) -> str | None:
    """La huella que el servidor comprueba al leer.

    Se calcula con la misma funcion que usa el servicio, y no con una cadena fija, porque
    `evidence_intact` se recalcula **al servir**: un resultado con una huella que no corresponde
    se devuelve como alterado. Con una huella inventada, estas pruebas estaria midiendo la
    firma y no el resumen, que es justo el error que un test debe evitar.
    """

    if resultado is None:
        return None
    if not isinstance(resultado, dict):
        return None
    return huella_resultado(resultado)


RESULTADO_CONTENEDOR = {
    "referencia": "alpine:3.20",
    "sistema_operativo": "alpine",
    "total_capas": 5,
    "paquetes": [
        {"name": "musl", "version": "1.2.4", "ecosystem": "apk"},
        {"name": "busybox", "version": "1.36", "ecosystem": "apk"},
        {"name": "zlib", "version": "1.3", "ecosystem": "apk"},
    ],
    "total_paquetes": 3,
}

RESULTADO_RED = {
    "cidr": "10.10.0.0/24",
    "direcciones_analizadas": 254,
    "hosts_con_puertos": 2,
    "hosts": [
        {"ip": "10.10.0.1", "puertos": [{"puerto": 22, "servicio": "ssh", "banner": None}]},
        {
            "ip": "10.10.0.7",
            "puertos": [
                {"puerto": 5432, "servicio": "postgresql", "banner": "PostgreSQL 13.4"},
                {"puerto": 22, "servicio": "ssh", "banner": None},
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_los_dos_resumenes_no_se_contaminan(integration_session: AsyncSession) -> None:
    """Una imagen no aparece en el resumen de redes y un segmento no aparece en el de
    contenedores.

    Es la razón de que haya dos rutas. Con un solo resumen, cada pantalla tendría que filtrar en
    el navegador lo que el servidor ya sabe, y además se traería el inventario entero de la otra
    mitad.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session)
    integration_session.add_all(
        [
            _trabajo(
                organizacion, AgentJobKindEnum.CONTAINER_SCAN, "alpine:3.20", RESULTADO_CONTENEDOR
            ),
            _trabajo(organizacion, AgentJobKindEnum.NETWORK_SCAN, "10.10.0.0/24", RESULTADO_RED),
        ]
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        contenedores = (await client.get("/api/v1/agents/summary", headers=cabeceras)).json()
        redes = (await client.get("/api/v1/agents/summary/networks", headers=cabeceras)).json()

    assert contenedores["total_imagenes"] == 1
    assert contenedores["total_paquetes"] == 3
    assert contenedores["total_redes"] == 0
    assert contenedores["total_hosts"] == 0

    assert redes["total_redes"] == 1
    assert redes["total_hosts"] == 2
    assert redes["total_puertos"] == 3
    assert redes["total_imagenes"] == 0
    assert redes["total_paquetes"] == 0


@pytest.mark.asyncio
async def test_los_puertos_se_cuentan_por_numero(integration_session: AsyncSession) -> None:
    """El puerto 22 aparece dos veces en el resultado y se cuenta dos veces.

    Es el dato que alimenta el gráfico de barras de `/networks`: un recuento de **hosts** en vez
    de un recuento de **puertos** daría un 22 donde hay dos, y el operador leería un servidor
    con dos servicios SSH donde hay dos máquinas con uno cada una.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session)
    integration_session.add(
        _trabajo(organizacion, AgentJobKindEnum.NETWORK_SCAN, "10.10.0.0/24", RESULTADO_RED)
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        redes = (await client.get("/api/v1/agents/summary/networks", headers=cabeceras)).json()

    assert redes["puertos_por_numero"] == {"22": 2, "5432": 1}
    assert redes["direcciones_analizadas"] == 254


@pytest.mark.asyncio
async def test_un_resultado_con_otra_forma_no_inventa_una_imagen_limpia(
    integration_session: AsyncSession,
) -> None:
    """Un `result` que no tiene la forma esperada cuenta como «sin inventario», no como cero
    paquetes.

    La diferencia es la que separa un inventario vacío de un inventario que no se ha podido
    leer. Un `0` en la columna de paquetes se lee como «esta imagen no tiene nada», y eso es una
    afirmación sobre la imagen del cliente que no se sostiene.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session)
    integration_session.add_all(
        [
            # Un resultado escrito por un agente más nuevo, con una clave que este backend no
            # conoce. Es el caso real: el `JSONB` lo escribe otro despliegue.
            _trabajo(
                organizacion,
                AgentJobKindEnum.CONTAINER_SCAN,
                "imagen-del-futuro:latest",
                {"referencia": "imagen-del-futuro:latest", "contenido": {"capas": 12}},
            ),
            # Y un `result` que no es un objeto, que es lo que devuelve un `null` serializado
            # con un molde raro.
            _trabajo(
                organizacion, AgentJobKindEnum.CONTAINER_SCAN, "raro:latest", "no soy un objeto"
            ),
        ]
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        contenedores = (await client.get("/api/v1/agents/summary", headers=cabeceras)).json()

    assert contenedores["total_imagenes"] == 2
    assert contenedores["total_paquetes"] == 0
    assert contenedores["imagenes_sin_inventario"] == 2
    assert contenedores["imagenes_inventariadas"] == 0


@pytest.mark.asyncio
async def test_los_cinco_estados_estan_siempre(integration_session: AsyncSession) -> None:
    """Aunque valgan cero.

    Una torta con un solo trozo no dice nada, y un estado ausente no es un estado que no ocurre:
    es un estado que no se ha dibujado. Un gráfico de estados con una sola barra verde parece
    «todo va bien» cuando en realidad solo hay un trabajo.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session)
    integration_session.add(
        _trabajo(
            organizacion,
            AgentJobKindEnum.CONTAINER_SCAN,
            "alpine:3.20",
            RESULTADO_CONTENEDOR,
            status=AgentJobStatusEnum.COMPLETED,
        )
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        contenedores = (await client.get("/api/v1/agents/summary", headers=cabeceras)).json()

    assert set(contenedores["por_estado"]) == {
        "QUEUED",
        "CLAIMED",
        "RUNNING",
        "COMPLETED",
        "FAILED",
    }
    assert contenedores["por_estado"]["COMPLETED"] == 1
    assert contenedores["por_estado"]["FAILED"] == 0


@pytest.mark.asyncio
async def test_la_serie_diaria_no_tiene_huecos(integration_session: AsyncSession) -> None:
    """Quince días, los quince, con los vacíos a cero.

    Un gráfico con huecos **lee** como si faltaran datos: el cliente ve «solo escaneé el lunes»
    en vez de «no escaneé el martes». Los huecos los rellena el servidor porque el navegador no
    sabe qué días son los que existen, y porque los días que no hay que rellenar no se
    pueden distinguir de los que se han perdido.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session)
    integration_session.add(
        _trabajo(
            organizacion, AgentJobKindEnum.CONTAINER_SCAN, "alpine:3.20", RESULTADO_CONTENEDOR
        )
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        contenedores = (await client.get("/api/v1/agents/summary", headers=cabeceras)).json()

    serie = contenedores["por_dia"]
    assert len(serie) == 15
    # Los días llegan en orden y son días consecutivos: un `sorted` sobre fechas ISO ordena
    # también, así que el orden se comprueba contra la serie completa, no contra un `<`.
    assert [d["dia"] for d in serie] == sorted(d["dia"] for d in serie)
    assert sum(d["escaneos"] for d in serie) == 1
    assert sum(1 for d in serie if d["escaneos"] == 0) == 14


@pytest.mark.asyncio
async def test_el_resumen_no_enseña_nada_de_otro_tenant(integration_session: AsyncSession) -> None:
    """R3, comprobado en la ruta que alimenta los números.

    Una fuga aquí no se ve como una fuga: se ve como un KPI con un número más alto. Y el cliente
    no tiene forma de saber que ese número no es suyo, así que la fuga no la detecta nadie.
    """

    assert integration_session is not None
    mio, _u1, cabeceras = await _tenant(integration_session, nombre="Mio")
    ajeno, _u2, _cab2 = await _tenant(integration_session, nombre="Ajeno")
    integration_session.add_all(
        [
            _trabajo(mio, AgentJobKindEnum.CONTAINER_SCAN, "mia:1", RESULTADO_CONTENEDOR),
            _trabajo(
                ajeno,
                AgentJobKindEnum.CONTAINER_SCAN,
                "ajena:1",
                {
                    **RESULTADO_CONTENEDOR,
                    "paquetes": [{"name": "x", "version": "1", "ecosystem": "apk"}] * 99,
                },
            ),
        ]
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mio_resumen = (await client.get("/api/v1/agents/summary", headers=cabeceras)).json()

    assert mio_resumen["total_imagenes"] == 1
    assert mio_resumen["total_paquetes"] == 3


@pytest.mark.asyncio
async def test_los_agentes_conectados_se_cuentan_con_la_ventana_del_servidor(
    integration_session: AsyncSession,
) -> None:
    """Tres agentes: uno recién conectado, uno que se cayó, uno que nunca conectó.

    La cuenta va separada de `total_agentes` a propósito. Un cliente con tres agentes dados de
    alta y ninguno conectado tiene «agentes» y no tiene nada, y la pantalla tiene que poder
    decirlo. Si los dos números fueran uno, el KPI mentiría hacia el lado cómodo.
    """

    import datetime as dt

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session)
    ahora = dt.datetime.now(dt.UTC)
    integration_session.add_all(
        [
            ScannerAgent(
                organization_id=organizacion.id,
                name="reciente",
                token_prefix="fx_aaaa",
                token_hash="h1",
                status="ACTIVE",
                last_seen_at=ahora - dt.timedelta(seconds=10),
            ),
            ScannerAgent(
                organization_id=organizacion.id,
                name="caido",
                token_prefix="fx_bbbb",
                token_hash="h2",
                status="ACTIVE",
                last_seen_at=ahora - dt.timedelta(hours=2),
            ),
            ScannerAgent(
                organization_id=organizacion.id,
                name="nunca",
                token_prefix="fx_cccc",
                token_hash="h3",
                status="ACTIVE",
                last_seen_at=None,
            ),
            ScannerAgent(
                organization_id=organizacion.id,
                name="dado-de-baja",
                token_prefix="fx_dddd",
                token_hash="h4",
                status="REVOKED",
                last_seen_at=ahora,
            ),
        ]
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resumen = (await client.get("/api/v1/agents/summary", headers=cabeceras)).json()

    # El dado de baja no cuenta ni como dado de alta: no puede volver a conexión sin un token
    # nuevo, y contarlo haría que el KPI de "agentes" prometiera algo que ya no va a pasar.
    assert resumen["total_agentes"] == 3
    assert resumen["vivos"] == 1
