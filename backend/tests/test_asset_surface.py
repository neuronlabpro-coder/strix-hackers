"""Pruebas de la superficie de ataque: dominios verificados y activos descubiertos.

## Qué se comprueba y por qué

La superficie de ataque tiene cuatro invariantes que no se deducen del código y que hay que
probar por separado, porque cada una se rompe de una forma distinta:

1. **La propiedad de un dominio es exclusiva (R3 aplicado a activos).** Un dominio es un
   activo empresarial. Si el tenant B pudiera reclamar `banco.com` después de que el A lo
   verificara, el descubrimiento del B le daría derecho a enumerar la infraestructura de
   otra empresa. Se prueba en los dos estados —verificado y pendiente— porque la regla
   distinta, y probar solo la fuerte deja sin verificar que la débil también existe.

2. **El aislamiento no se negocia por `id`.** Pedir el dominio del tenant A siendo del B
   tiene que dar `404`, no `403`: un `403` confirmaría que ese identificador existe, que es
   exactamente lo que R3 no permite revelar.

3. **No se descubre lo que no se ha verificado.** El `400` no es un formality: es la
  traducción de `is_verified == True` que se comprueba en el endpoint. Y la tarea Celery lo
   vuelve a comprobar, porque entre el encolado y la ejecución la fila puede haber cambiado.

4. **El descubrimiento es idempotente y no destructive.** Reescanear no puede duplicar filas,
   no puede borrar las que ya no aparecen y no puede perder la fecha de primera aparición.
   Eso se mide por conteo **con filtro de organización**, nunca contando filas globales de la
   base compartida: el antipatrón de las pruebas anteriores.

## Por qué el DNS se sustituye en vez de consultarse

Porque una prueba que depende de `1.1.1.1` no es una prueba: falla cuando el runner no tiene
salida, y cuando la tiene verifica que Cloudflare responde, no que el código funciona. El
`monkeypatch` sobre `_query_sync` sustituye la **consulta** y deja intacta la lógica de
comparación, la normalización y los seis resultados, que es donde están los errores
posibles.

## Por qué hay una prueba de normalización de dominio y no solo del alta

`Empresa.COM` y `empresa.com` tienen que ser la misma fila. Si no lo fueran, el mismo dominio
aparecería dos veces con dos tokens distintos y "verificar" no tendría respuesta clara: el
usuario vería un verificado y otro pendiente para el mismo nombre, sin forma de saber cuál
es el bueno. La prueba compara por conteo **filtrado por organización**, no por longitud de
la lista global.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.assets import discovery, service
from backend.apps.assets.models import (
    AssetTypeEnum,
    DiscoveredAsset,
    VerifiedDomain,
)
from backend.apps.assets.schemas import (
    _ETIQUETA_DNS as ETIQUETA_DNS,
)
from backend.apps.assets.schemas import (
    DomainCreate,
    normalize_domain,
)
from backend.apps.assets.verifier import (
    SERVICIO_LABEL_PATTERN,
    TXT_PREFIX,
    TXT_RECORD_LABEL,
    DnsLookupOutcome,
    build_txt_record,
    build_txt_record_name,
    is_publishable_record_name,
    verify_domain_txt,
)
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.config import settings
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def _headers(user: User, organization: Organization | None = None) -> dict[str, str]:
    cabeceras = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    if organization is not None:
        cabeceras["X-Organization-Id"] = str(organization.id)
    return cabeceras


async def _tenant(
    session: AsyncSession,
    *,
    role: RoleEnum = RoleEnum.ADMIN,
    prefijo: str = "assets",
) -> tuple[User, Organization, dict[str, str]]:
    """Un workspace con un usuario del rol pedido y sus cabeceras."""

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{prefijo} {suffix}", slug=f"{prefijo}-{suffix}"
    )
    user = User(
        email=f"{prefijo}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"Cliente de {prefijo}",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=role)
    )
    await session.commit()
    return user, organization, _headers(user, organization)


async def _dominio(
    session: AsyncSession,
    organization_id: uuid.UUID,
    nombre: str,
    *,
    verificado: bool = False,
    created_at: datetime | None = None,
) -> VerifiedDomain:
    """Crea un dominio directamente en la base, saltándose la API y el DNS.

    `created_at` se puede fijar a mano porque la columna solo declara `server_default`: es lo
    único que permite medir el rango de fechas sin depender del reloj.
    """

    dominio = VerifiedDomain(
        organization_id=organization_id,
        domain_name=nombre,
        verification_token=uuid.uuid4().hex,
        is_verified=verificado,
        verified_at=datetime.now(UTC) if verificado else None,
        **({} if created_at is None else {"created_at": created_at}),
    )
    session.add(dominio)
    await session.commit()
    return dominio


async def _activos_de(session: AsyncSession, organization_id: uuid.UUID) -> int:
    """Activos de **un** workspace. Nunca cuenta filas globales."""

    total = (
        await session.execute(
            select(func.count(DiscoveredAsset.id)).where(
                DiscoveredAsset.organization_id == organization_id
            )
        )
    ).scalar_one()
    return int(total)


async def _dominios_de(session: AsyncSession, organization_id: uuid.UUID) -> int:
    total = (
        await session.execute(
            select(func.count(VerifiedDomain.id)).where(
                VerifiedDomain.organization_id == organization_id
            )
        )
    ).scalar_one()
    return int(total)


async def _limpiar(session: AsyncSession, organization_id: uuid.UUID) -> None:
    await session.execute(
        delete(DiscoveredAsset).where(DiscoveredAsset.organization_id == organization_id)
    )
    await session.execute(
        delete(VerifiedDomain).where(
            VerifiedDomain.organization_id == organization_id
        )
    )
    await session.commit()


# --------------------------------------------------------------------------- #
# Normalización y validación del nombre
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("empresa.com", "empresa.com"),
        ("Empresa.COM", "empresa.com"),
        ("  empresa.com  ", "empresa.com"),
        ("https://empresa.com", "empresa.com"),
        ("http://www.empresa.com/path", "www.empresa.com"),
        ("empresa.com.", "empresa.com"),
        ("https://empresa.com:8443/x", "empresa.com"),
    ],
)
def test_normalizar_dominio(entrada: str, esperado: str) -> None:
    """El nombre se guarda siempre en su forma canónica."""

    assert normalize_domain(entrada) == esperado


def test_normalizar_no_quita_www() -> None:
    """`www` es un subdominio real y se conserva.

    Quitarlo fusionaría dos hosts distintos en uno, y el descubrimiento nunca vería ninguno
    de los dos. Es el caso donde "normalizar de más" destruye información.
    """

    assert normalize_domain("www.empresa.com") == "www.empresa.com"
    assert normalize_domain("empresa.com") == "empresa.com"


@pytest.mark.parametrize(
    "invalido",
    [
        "",
        "   ",
        "localhost",
        "empresa",
        "empresa..com",
        "-empresa.com",
        "empresa-.com",
        "empresa.123",
        "empresa_foo.com",
        "a" * 64 + ".com",
    ],
)
def test_dominio_invalido_no_se_puede_registrar(invalido: str) -> None:
    """Lo que no es un FQDN publicable se rechaza en el esquema, no en la base."""

    with pytest.raises(ValueError):
        DomainCreate(domain_name=invalido)


# --------------------------------------------------------------------------- #
# Verificación por DNS: los seis resultados
# --------------------------------------------------------------------------- #


async def test_txt_coincidente_verifica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con el token publicado, el resultado es `MATCH` y verificado."""

    token = uuid.uuid4().hex

    def _falso(domain: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        return (DnsLookupOutcome.NO_TXT, (build_txt_record(token),))

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)
    resultado = await verify_domain_txt("empresa.com", token)
    assert resultado.outcome is DnsLookupOutcome.MATCH
    assert resultado.verified is True
    assert resultado.expected_value == f"{TXT_PREFIX}{token}"


async def test_txt_que_no_coincide_devuelve_lo_encontrado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un token equivocado se reporta con su valor, para que el panel lo muestre.

    Sin `found_values` el usuario sabe que "no coincide" y no puede ver que tiene un token de
    diciembre equivocado, que es el error más frecuente y el más fácil de arreglar.
    """

    publicado = build_txt_record("token-del-otro-cliente")

    def _falso(domain: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        return (DnsLookupOutcome.NO_TXT, (publicado,))

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)
    resultado = await verify_domain_txt("empresa.com", uuid.uuid4().hex)
    assert resultado.outcome is DnsLookupOutcome.MISMATCH
    assert resultado.verified is False
    assert resultado.found_values == (publicado,)


async def test_sin_txt_es_estado_normal_y_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un nombre que existe sin TXT es `NO_TXT`, no `NXDOMAIN` ni excepción.

    Es la diferencia entre "todavía no lo he publicado" —esperar— y "te has equivocado al
    escribir el nombre" —corregir—. Confundirlas era el fallo que motivó el enum.
    """

    def _falso(domain: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        return (DnsLookupOutcome.NO_TXT, ())

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)
    resultado = await verify_domain_txt("empresa.com", uuid.uuid4().hex)
    assert resultado.outcome is DnsLookupOutcome.NO_TXT
    assert resultado.found_values == ()


async def test_nxdomain_no_se_confunde_con_sin_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NXDOMAIN` es su propio estado: el nombre no existe."""

    def _falso(domain: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        return (DnsLookupOutcome.NXDOMAIN, ())

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)
    resultado = await verify_domain_txt("noexiste.example", uuid.uuid4().hex)
    assert resultado.outcome is DnsLookupOutcome.NXDOMAIN


async def test_timeout_del_dns_no_propaga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un DNS que no contesta devuelve `TIMEOUT`, no lanza.

    `NXDOMAIN` y "no hay TXT" también producen una lista vacía; el corte del bucle de
    eventos tiene que distinguirse de ambos, y un `500` para un dominio que aún no existe
    dejaría al usuario sin poder saber si esperar o abrir un ticket.
    """

    def _falso(domain: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        raise TimeoutError

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)
    resultado = await verify_domain_txt("empresa.com", uuid.uuid4().hex)
    assert resultado.outcome is DnsLookupOutcome.TIMEOUT
    assert resultado.verified is False


def test_el_nombre_del_registro_es_publicable() -> None:
    """El nombre del registro tiene que existir en una zona real.

    Nombre y valor son dos cosas distintas: el nombre es una etiqueta DNS y no admite `=`.
    Construir el nombre con el prefijo del valor —`fenix-domain-verify=fenix`— produce una
    cadena que ningún proveedor acepta, y el usuario vería en el panel una instrucción que
    falla al copiarla. Es un fallo silencioso: la API responde `200`, el panel muestra un
    valor, y el error solo aparece cuando el cliente va a su panel de DNS.
    """

    nombre = build_txt_record_name("empresa.com")
    assert nombre == f"{TXT_RECORD_LABEL}.empresa.com"
    assert is_publishable_record_name(nombre) is True


def test_la_etiqueta_de_verificacion_usa_la_regla_de_servicio() -> None:
    """La etiqueta lleva guion bajo, y para eso hay una regla **distinta**.

    Un nombre de dominio que alguien registra no admite `_`, pero un registro TXT de
    servicio sí, porque es así como funcionan `_dmarc` y `_domainkey`. Reutilizar la regla
    estricta del dominio para la etiqueta de verificación rechazaría un nombre que es
    perfectamente publicable: la plataforma no podría ni escribir su propia instrucción.
    """

    assert "_" in TXT_RECORD_LABEL
    assert SERVICIO_LABEL_PATTERN.match(TXT_RECORD_LABEL) is not None
    # Y la regla del dominio, que es más estricta, no lo acepta. Que sean distintas es
    # justo lo que esta prueba comprueba.
    assert ETIQUETA_DNS.match(TXT_RECORD_LABEL) is None


def test_un_nombre_no_publicable_se_rechaza() -> None:
    """La comprobación rechaza lo que una zona no aceptaría.

    Sin esto, cambiar la etiqueta a algo inválido —un espacio, un guion al final— no
    produciría ningún error hasta que un cliente intentara publicarlo.
    """

    assert is_publishable_record_name("con espacio.com") is False
    assert is_publishable_record_name("sin-dominio") is False
    assert is_publishable_record_name("-guion.com") is False
    assert is_publishable_record_name("igual.com") is True


def test_la_etiqueta_no_colisiona_con_un_registro_real() -> None:
    """La etiqueta empieza por guion bajo: es espacio del propietario del dominio.

    Un nombre sin guion bajo podría pisar un servicio real del cliente —`_dmarc`, `_domainkey`—,
    y la verificación se basaría en un registro que el cliente no controla. El guion bajo
    reserva el espacio justamente para esto.
    """

    assert TXT_RECORD_LABEL.startswith("_")
    assert TXT_RECORD_LABEL not in ("_dmarc", "_domainkey")


async def test_el_texto_que_se_consulta_es_el_que_se_publica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El nombre consultado y el que se enseña son el mismo, y el token también.

    Se captura el nombre que llega a la consulta y se compara con el que el servicio
    devuelve. Si divergieran, el cliente publicaría un registro y la verificación consultaría
    otro, y fallaría sin que ninguna de las dos partes tuviera un error visible.
    """

    token = uuid.uuid4().hex
    nombres_consultados: list[str] = []

    def _falso(nombre: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        nombres_consultados.append(nombre)
        return (DnsLookupOutcome.NO_TXT, (build_txt_record(token),))

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)
    await verify_domain_txt("empresa.com", token)

    assert nombres_consultados == [f"{TXT_RECORD_LABEL}.empresa.com"]
    assert build_txt_record(token) == f"{TXT_PREFIX}{token}"


# --------------------------------------------------------------------------- #
# Exclusividad de la propiedad del dominio entre tenants
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dominio_verificado_de_otro_tenant_es_409(
    integration_session: AsyncSession,
) -> None:
    """Un dominio ya verificado por otro workspace no se puede reclamar.

    Es el caso fuerte: si pasara, el tenant B descubriría la infraestructura de un dominio
    que el A demostró controlar. Es la razón de que la verificación exista.
    """

    session = integration_session
    assert session is not None
    _, org_a, _ = await _tenant(session, prefijo="owner")
    _, _, cabeceras_b = await _tenant(session, prefijo="intruso")

    await _dominio(session, org_a.id, "banco.com", verificado=True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.post(
                "/api/v1/assets/domains",
                json={"domain_name": "banco.com"},
                headers=cabeceras_b,
            )
        assert respuesta.status_code == 409, respuesta.text
        detalle = respuesta.json()["detail"]
        assert detalle["motivo"] == "OTRO_WORKSPACE"
        # El reclamo estaba verificado, y el panel necesita saberlo: es la diferencia
        # entre "nadie lo ha reclamado todavía" y "ya tiene dueño y está verificado".
        assert detalle["already_verified"] is True
        assert await _dominios_de(session, org_a.id) == 1
    finally:
        await _limpiar(session, org_a.id)


@pytest.mark.asyncio
async def test_dominio_pendiente_de_otro_tenant_tambien_es_409(
    integration_session: AsyncSession,
) -> None:
    """Un reclamo pendiente de otro workspace tampoco se puede reclamar.

    Parece más estricto de lo necesario y no lo es: si dos workspaces reclamaran el mismo
    nombre sin verificar, el que publicara el TXT segundo se quedaría con la verificación y
    el primero descubriría la infraestructura que el segundo le reclamaba. La propiedad no
    se establece dos veces, y se resuelve en el alta, que es cuando es barato.
    """

    session = integration_session
    assert session is not None
    _, org_a, _ = await _tenant(session, prefijo="pendiente")
    _, _, cabeceras_b = await _tenant(session, prefijo="segundo")

    await _dominio(session, org_a.id, "pendiente.com", verificado=False)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.post(
                "/api/v1/assets/domains",
                json={"domain_name": "pendiente.com"},
                headers=cabeceras_b,
            )
        assert respuesta.status_code == 409, respuesta.text
        detalle = respuesta.json()["detail"]
        assert detalle["motivo"] == "OTRO_WORKSPACE"
        assert detalle["already_verified"] is False
    finally:
        await _limpiar(session, org_a.id)


@pytest.mark.asyncio
async def test_el_mismo_tenant_no_se_choca_consigo_mismo(
    integration_session: AsyncSession,
) -> None:
    """Un dominio ya propio se rechaza con `409`, no se duplica.

    Lo que se mide es el número de filas de **ese** workspace: si el `UNIQUE` fallara, el
    mismo nombre aparecería dos veces con dos tokens distintos y "verificar" no tendría
    respuesta clara.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="propio")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            primera = await cliente.post(
                "/api/v1/assets/domains",
                json={"domain_name": "mio.com"},
                headers=cabeceras,
            )
            segunda = await cliente.post(
                "/api/v1/assets/domains",
                json={"domain_name": "MIO.com"},
                headers=cabeceras,
            )
        assert primera.status_code == 201, primera.text
        assert segunda.status_code == 409, segunda.text
        # El motivo viaja estructurado. Con un `detail` en prosa, el panel tendría que
        # leer la frase en español para decidir si ofrecer "abrir el dominio" o solo un
        # mensaje, y en inglés no habría forma de decidirlo.
        assert segunda.json()["detail"]["motivo"] == "ESTE_WORKSPACE"
        assert segunda.json()["detail"]["already_verified"] is False
        assert await _dominios_de(session, org.id) == 1
    finally:
        await _limpiar(session, org.id)


# --------------------------------------------------------------------------- #
# Aislamiento (R3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leer_dominio_de_otro_tenant_es_404(
    integration_session: AsyncSession,
) -> None:
    """Un `id` de otro workspace responde `404`, no `403`.

    Un `403` confirmaría que ese identificador existe, que es justo lo que R3 no permite
    revelar. Por eso `DomainNotFoundError` no distingue "no existe" de "es de otro".
    """

    session = integration_session
    assert session is not None
    _, org_a, _ = await _tenant(session, prefijo="leedor")
    _, _, cabeceras_b = await _tenant(session, prefijo="otro")
    dominio = await _dominio(session, org_a.id, "secreto.com", verificado=True)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            listado = await cliente.get("/api/v1/assets/domains", headers=cabeceras_b)
            activo = await cliente.post(
                f"/api/v1/assets/domains/{dominio.id}/verify", headers=cabeceras_b
            )
            borrado = await cliente.delete(
                f"/api/v1/assets/domains/{dominio.id}", headers=cabeceras_b
            )
        assert listado.json()["items"] == []
        assert activo.status_code == 404, activo.text
        assert borrado.status_code == 404, borrado.text
    finally:
        await _limpiar(session, org_a.id)


@pytest.mark.asyncio
async def test_el_inventario_no_enseña_activos_de_otro_tenant(
    integration_session: AsyncSession,
) -> None:
    """El filtro por organización va **siempre**, no solo cuando hay `domain_id`.

    El fallo sería un `GET /discovery` sin filtro devolviendo la superficie de todos los
    tenants. Se siembra un activo ajeno y se mide que el listado del otro viene vacío.
    """

    session = integration_session
    assert session is not None
    _, org_a, _ = await _tenant(session, prefijo="con-activo")
    _, org_b, cabeceras_b = await _tenant(session, prefijo="sin-activo")

    dominio_a = await _dominio(session, org_a.id, "a.com", verificado=True)
    session.add(
        DiscoveredAsset(
            domain_id=dominio_a.id,
            organization_id=org_a.id,
            asset_type=AssetTypeEnum.SUBDOMAIN,
            value="secreto.a.com",
        )
    )
    await session.commit()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            sin_filtro = await cliente.get(
                "/api/v1/assets/discovery", headers=cabeceras_b
            )
            # Y con el `domain_id` ajeno explícito: el filtro de tenant va delante del
            # recurso, no detras.
            con_id = await cliente.get(
                "/api/v1/assets/discovery",
                params={"domain_id": str(dominio_a.id)},
                headers=cabeceras_b,
            )
        assert sin_filtro.json()["total"] == 0
        assert con_id.json()["total"] == 0
        assert con_id.json()["items"] == []
    finally:
        await _limpiar(session, org_a.id)
        assert await _activos_de(session, org_b.id) == 0


# --------------------------------------------------------------------------- #
# El descubrimiento exige verificación
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_descubrir_un_dominio_sin_verificar_es_400(
    integration_session: AsyncSession,
) -> None:
    """Sin `is_verified`, el descubrimiento es `400`, no `409`.

    La petición está bien formada y el recurso existe; lo que falla es un requisito previo
    del dominio. Probar el código importa porque el panel distingue "no verificado" de
    "choca con el estado actual" pintando mensajes distintos.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="sin-verificar")
    dominio = await _dominio(session, org.id, "nuevo.com", verificado=False)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.post(
                f"/api/v1/assets/domains/{dominio.id}/discover", headers=cabeceras
            )
        assert respuesta.status_code == 400, respuesta.text
    finally:
        await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_descubrir_otro_tenant_es_404(
    integration_session: AsyncSession,
) -> None:
    """Lanzar descubrimiento sobre un dominio ajeno no se encola."""

    session = integration_session
    assert session is not None
    _, org_a, _ = await _tenant(session, prefijo="dueno")
    _, _, cabeceras_b = await _tenant(session, prefijo="ajeno")
    dominio = await _dominio(session, org_a.id, "de-otro.com", verificado=True)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.post(
                f"/api/v1/assets/domains/{dominio.id}/discover", headers=cabeceras_b
            )
        assert respuesta.status_code == 404, respuesta.text
    finally:
        await _limpiar(session, org_a.id)


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_un_miembro_no_admin_no_gestiona_la_superficie(
    integration_session: AsyncSession,
) -> None:
    """Registrar, verificar, borrar y descubrir exigen admin del tenant.

    Sin esta separación, un token de lectura —o un miembro con rol de inviting— podría dar
    de alta un dominio y lanzar un descubrimiento: capacidad de superficie de ataque desde
    un permiso de lectura. Se prueban las cuatro operaciones porque el filtro se puede
    olvidar en una y no en las otras.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, role=RoleEnum.MEMBER, prefijo="invitado")
    dominio = await _dominio(session, org.id, "del-miembro.com", verificado=True)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            alta = await cliente.post(
                "/api/v1/assets/domains",
                json={"domain_name": "otro.com"},
                headers=cabeceras,
            )
            verifica = await cliente.post(
                f"/api/v1/assets/domains/{dominio.id}/verify", headers=cabeceras
            )
            borrado = await cliente.delete(
                f"/api/v1/assets/domains/{dominio.id}", headers=cabeceras
            )
            descubre = await cliente.post(
                f"/api/v1/assets/domains/{dominio.id}/discover", headers=cabeceras
            )
        for respuesta, nombre in (
            (alta, "alta"),
            (verifica, "verificar"),
            (borrado, "borrar"),
            (descubre, "descubrir"),
        ):
            assert respuesta.status_code == 403, f"{nombre}: {respuesta.text}"
        # Y leer sí se permite: la vista del inventario no es una escritura.
        lectura = None
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            lectura = await cliente.get("/api/v1/assets/domains", headers=cabeceras)
        assert lectura.status_code == 200, lectura.text
    finally:
        await _limpiar(session, org.id)


# --------------------------------------------------------------------------- #
# Verificación por HTTP y borrado
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verificar_por_http_marca_el_dominio(
    integration_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El `POST /verify` marca el dominio y devuelve `200` también si no coincide.

    Un `409` por "no verificado" convertiría el resultado más frecuente —"todavía no lo he
    publicado"— en un error, y el panel no podría distinguir un `NO_TXT` normal de un fallo
    de red por el código.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="verificar")
    await _dominio(session, org.id, "verificar.com", verificado=False)

    token_en_la_fila = (
        (
            await session.execute(
                select(VerifiedDomain.verification_token).where(
                    VerifiedDomain.organization_id == org.id
                )
            )
        )
        .scalar_one()
    )

    def _falso(domain: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
        return (DnsLookupOutcome.NO_TXT, (build_txt_record(token_en_la_fila),))

    monkeypatch.setattr("backend.apps.assets.verifier._query_sync", _falso)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            listado = await cliente.get("/api/v1/assets/domains", headers=cabeceras)
            id_dominio = listado.json()["items"][0]["id"]
            respuesta = await cliente.post(
                f"/api/v1/assets/domains/{id_dominio}/verify", headers=cabeceras
            )
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["outcome"] == "MATCH"
        assert cuerpo["is_verified"] is True
        assert cuerpo["message_key"] == "verify.outcome.MATCH"

        fila = (
            await session.execute(
                select(VerifiedDomain).where(
                    VerifiedDomain.organization_id == org.id
                )
            )
        ).scalar_one()
        await session.refresh(fila)
        assert fila.is_verified is True
        assert fila.verified_at is not None
    finally:
        await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_borrar_un_dominio_verificado_es_409(
    integration_session: AsyncSession,
) -> None:
    """Un dominio verificado no se borra: es inventario que el cliente ya pagó.

    Borrarlo sin dejar rastro sería hacer desaparecer la superficie de ataque registrada, y
    la única vía para quitarlo sería la baja del workspace, que es otra decisión.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="borrar")
    dominio = await _dominio(session, org.id, "registrado.com", verificado=True)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.delete(
                f"/api/v1/assets/domains/{dominio.id}", headers=cabeceras
            )
        assert respuesta.status_code == 409, respuesta.text
        assert await _dominios_de(session, org.id) == 1
    finally:
        await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_borrar_un_dominio_pendiente_se_lleva_sus_activos(
    integration_session: AsyncSession,
) -> None:
    """El `CASCADE` de `domain_id` borra los activos de un dominio no verificado.

    Es el caso opuesto al de `organization_id` a propósito: un dominio sin verificar genera
    activos basura y borrarse tiene que llevárselos, o el inventario se llenaría de restos de
    dominios que ya no existen.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="cascada")
    dominio = await _dominio(session, org.id, "basura.com", verificado=False)
    session.add(
        DiscoveredAsset(
            domain_id=dominio.id,
            organization_id=org.id,
            asset_type=AssetTypeEnum.SUBDOMAIN,
            value="x.basura.com",
        )
    )
    await session.commit()
    assert await _activos_de(session, org.id) == 1

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.delete(
                f"/api/v1/assets/domains/{dominio.id}", headers=cabeceras
            )
        assert respuesta.status_code == 204, respuesta.text
        assert await _dominios_de(session, org.id) == 0
        assert await _activos_de(session, org.id) == 0
    finally:
        await _limpiar(session, org.id)


# --------------------------------------------------------------------------- #
# Filtros del inventario: texto y rango de fechas
# --------------------------------------------------------------------------- #


async def _activo(
    session: AsyncSession,
    dominio: VerifiedDomain,
    organization_id: uuid.UUID,
    valor: str,
    *,
    service_name: str | None = None,
    created_at: datetime | None = None,
) -> DiscoveredAsset:
    """Un activo escrito directamente en la base, con `created_at` fijable a mano.

    Se escribe en vez de usar `service.merge_assets` porque esta prueba mide **filtros**, no la
    idempotencia del descubrimiento, y el merge fija `last_scanned_at` al reloj. Fijar
    `created_at` a mano es lo único que permite medir un rango sin depender de la hora.
    """

    activo = DiscoveredAsset(
        domain_id=dominio.id,
        organization_id=organization_id,
        asset_type=AssetTypeEnum.SUBDOMAIN,
        value=valor,
        service_name=service_name,
        **({} if created_at is None else {"created_at": created_at}),
    )
    session.add(activo)
    await session.commit()
    return activo


@pytest.mark.asyncio
async def test_el_buscador_de_activos_alcanza_valor_servicio_y_dominio(
    integration_session: AsyncSession,
) -> None:
    """Un host se recuerda por tres cosas, y el buscador tiene que llegar a las tres.

    `api.empresa.com` se busca por el nombre completo, por `api`, o por el nombre de servicio que
    apareció en la tabla. Con una sola columna, dos de cada tres búsquedas darían página vacía y
    el buscador parecería roto.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="busca")
    dominio = await _dominio(session, org.id, "acme-filtro.com", verificado=True)
    await _activo(session, dominio, org.id, "api.acme-filtro.com", service_name="payments")
    await _activo(session, dominio, org.id, "www.acme-filtro.com", service_name="web")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            por_valor = await cliente.get(
                "/api/v1/assets/discovery", params={"query": "api."}, headers=cabeceras
            )
            por_servicio = await cliente.get(
                "/api/v1/assets/discovery", params={"query": "payments"}, headers=cabeceras
            )
            por_dominio = await cliente.get(
                "/api/v1/assets/discovery",
                params={"query": "acme-filtro"},
                headers=cabeceras,
            )

        assert por_valor.json()["total"] == 1
        assert por_valor.json()["items"][0]["value"] == "api.acme-filtro.com"
        assert por_servicio.json()["total"] == 1
        assert por_servicio.json()["items"][0]["value"] == "api.acme-filtro.com"
        assert por_dominio.json()["total"] == 2
    finally:
        await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_el_buscador_de_activos_no_trata_los_comodines_como_comodines(
    integration_session: AsyncSession,
) -> None:
    """`%` y `_` se buscan literales, y no devuelven el inventario entero.

    El fallo más silencioso de un buscador: la pantalla responde, enseña filas y el operador da
    por bueno un filtro que no ha filtrado nada. El `_` importa de verdad porque los hosts
    generados por los descubrimiento llevan `_` con frecuencia (`stage_acme`).
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="comodin")
    dominio = await _dominio(session, org.id, "comodin.com", verificado=True)
    await _activo(session, dominio, org.id, "web_app.comodin.com")
    await _activo(session, dominio, org.id, "webXapp.comodin.com")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            con_porcentaje = await cliente.get(
                "/api/v1/assets/discovery", params={"query": "%"}, headers=cabeceras
            )
            con_subrayado = await cliente.get(
                "/api/v1/assets/discovery", params={"query": "web_app"}, headers=cabeceras
            )

        assert con_porcentaje.json()["total"] == 0, con_porcentaje.json()
        assert con_subrayado.json()["total"] == 1
        assert con_subrayado.json()["items"][0]["value"] == "web_app.comodin.com"
    finally:
        await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_el_rango_de_activos_va_sobre_el_alta_y_no_sobre_la_ultima_revision(
    integration_session: AsyncSession,
) -> None:
    """El rango recorta por `created_at`, y su límite superior es inclusivo.

    `last_scanned_at` es `NULL` en un activo recién descubierto, así que un rango sobre ella no
    recortaría filas: las **borraría**, y todos los activos nuevos desaparecerían de la tabla en
    cuanto se tocara cualquiera de las dos fechas. Aquí se siembra un activo con
    `last_scanned_at` a `NULL` —el caso que lo rompe— y tiene que seguir apareciendo.
    """

    session = integration_session
    assert session is not None
    _, org, cabeceras = await _tenant(session, prefijo="rango")
    dominio = await _dominio(session, org.id, "rango-activos.com", verificado=True)
    nuevo = await _activo(
        session,
        dominio,
        org.id,
        "nuevo.rango-activos.com",
        created_at=datetime(2026, 3, 3, 23, 30, tzinfo=UTC),
    )
    viejo = await _activo(
        session,
        dominio,
        org.id,
        "viejo.rango-activos.com",
        created_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
    )
    # El activo nuevo no se ha vuelto a revisar nunca: es el caso que un rango sobre
    # `last_scanned_at` borraría de la tabla.
    assert nuevo.last_scanned_at is None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            dentro = await cliente.get(
                "/api/v1/assets/discovery",
                params={"created_from": "2026-03-01", "created_to": "2026-03-03"},
                headers=cabeceras,
            )
            invertido = await cliente.get(
                "/api/v1/assets/discovery",
                params={"created_from": "2026-03-05", "created_to": "2026-03-01"},
                headers=cabeceras,
            )

        ids = {item["id"] for item in dentro.json()["items"]}
        # El día final entra entero: el activo de las 23:30 del día 3 está dentro.
        assert ids == {str(nuevo.id), str(viejo.id)}
        # Rango invertido: lista vacía, no un `422`.
        assert invertido.status_code == 200, invertido.text
        assert invertido.json()["total"] == 0
    finally:
        await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_el_buscador_de_activos_no_atraviesa_el_aislamiento(
    integration_session: AsyncSession,
) -> None:
    """El texto no es una puerta trasera al filtro de organización.

    El `organization_id` es la primera condición de la consulta. Si el texto se mezclara con él
    en un `or_`, un término que casara con el host de otro workspace lo devolvería.
    """

    session = integration_session
    assert session is not None
    _, org_ajeno, _ = await _tenant(session, prefijo="ajeno-activos")
    _, org_propio, cabeceras = await _tenant(session, prefijo="propio-activos")
    dominio_ajeno = await _dominio(session, org_ajeno.id, "ajeno-activos.com", verificado=True)
    await _activo(session, dominio_ajeno, org_ajeno.id, "secreto.ajeno-activos.com")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.get(
                "/api/v1/assets/discovery", params={"query": "secreto"}, headers=cabeceras
            )
        assert respuesta.status_code == 200
        assert respuesta.json()["total"] == 0
        assert respuesta.json()["items"] == []
    finally:
        await _limpiar(session, org_ajeno.id)
        await _limpiar(session, org_propio.id)


# --------------------------------------------------------------------------- #
# Descubrimiento: idempotencia, fusión y no destructividad
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reescanear_no_duplica_ni_borra(
    integration_session: AsyncSession,
) -> None:
    """El segundo descubrimiento actualiza, no inserta de nuevo y no elimina.

    Se miden tres cosas distintas porque cada una puede romperse sola: el número de filas
    (**de ese tenant**), que `created_at` del activo superviviente no cambia —la fecha de
    primera aparición es lo que hace que el inventario distinga "nuevo" de "lleva años
    aquí"—, y que `last_scanned_at` sí se mueve.
    """

    session = integration_session
    assert session is not None
    _, org, _ = await _tenant(session, prefijo="idempotente")
    dominio = await _dominio(session, org.id, "idem.com", verificado=True)

    primera = await service.merge_assets(
        session,
        dominio.id,
        org.id,
        [
            (AssetTypeEnum.SUBDOMAIN, "www.idem.com", None, ["Nginx"]),
            (AssetTypeEnum.IP_ADDRESS, "203.0.113.10", None, []),
        ],
    )
    assert primera == 2
    activo_inicial = (
        await session.execute(
            select(DiscoveredAsset).where(
                DiscoveredAsset.organization_id == org.id,
                DiscoveredAsset.value == "www.idem.com",
            )
        )
    ).scalar_one()
    creado_original = activo_inicial.created_at
    assert activo_inicial.last_scanned_at is not None
    primer_marcado = activo_inicial.last_scanned_at

    segunda = await service.merge_assets(
        session,
        dominio.id,
        org.id,
        [
            (AssetTypeEnum.SUBDOMAIN, "www.idem.com", None, ["Nginx", "Cloudflare"]),
        ],
    )
    assert segunda == 0
    assert await _activos_de(session, org.id) == 2

    await session.refresh(activo_inicial)
    assert activo_inicial.created_at == creado_original
    assert activo_inicial.last_scanned_at >= primer_marcado
    # La fusión conserva lo conocido y añade lo nuevo, sin repetir lo repetido.
    assert activo_inicial.technologies == ["Nginx", "Cloudflare"]

    await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_una_tecnologia_que_desaparece_no_se_borra(
    integration_session: AsyncSession,
) -> None:
    """Un escaneo que no detecta algo previo no lo elimina del inventario.

    Puede que el escaneo no lo haya alcanzado, no que haya desaparecido. Reemplazar la lista
    haría que el inventario perdiera información cada vez que una ejecución viniera
    incompleta, que es la forma más rápida de que un cliente deje de fiarse de la
    herramienta.
    """

    session = integration_session
    assert session is not None
    _, org, _ = await _tenant(session, prefijo="fusion")
    dominio = await _dominio(session, org.id, "fusion.com", verificado=True)

    await service.merge_assets(
        session,
        dominio.id,
        org.id,
        [(AssetTypeEnum.SUBDOMAIN, "a.fusion.com", None, ["Cloudflare"])],
    )
    await service.merge_assets(
        session,
        dominio.id,
        org.id,
        [(AssetTypeEnum.SUBDOMAIN, "a.fusion.com", None, [])],
    )

    activo = (
        await session.execute(
            select(DiscoveredAsset).where(
                DiscoveredAsset.organization_id == org.id
            )
        )
    ).scalar_one()
    assert activo.technologies == ["Cloudflare"]
    await _limpiar(session, org.id)


@pytest.mark.asyncio
async def test_el_techo_de_activos_viene_de_la_configuracion(
    integration_session: AsyncSession,
) -> None:
    """El recorte por cuota sale de la configuración, no de una constante del módulo.

    R1 saca del código las cuotas de escaneo. La prueba fija el techo en 2 y comprueba que
    el cuarto candidato no se guarda: si el valor estuviera en el código, con la cuota por
    defecto de 500 los cuatro se guardarían y la prueba fallaría de forma informative.
    """

    session = integration_session
    assert session is not None
    _, org, _ = await _tenant(session, prefijo="cuota")
    dominio = await _dominio(session, org.id, "cuota.com", verificado=True)

    nuevos = await service.merge_assets(
        session,
        dominio.id,
        org.id,
        [(AssetTypeEnum.SUBDOMAIN, f"s{n}.cuota.com", None, []) for n in range(4)],
        max_assets=2,
    )
    assert nuevos == 2
    assert await _activos_de(session, org.id) == 2
    await _limpiar(session, org.id)


def test_el_techo_por_defecto_viene_de_la_configuracion() -> None:
    """Sin tope explícito, el valor sale de `Settings` y no del módulo.

    Es la mitad de la prueba anterior: la primera comprueba que el recorte **respeta** un
    tope dado, esta comprueba que el valor por defecto **es** el de la configuración. Sin
    esta, un `DEFAULT_MAX_ASSETS_PER_DISCOVERY` congelado pasaría la primera.
    """

    assert service.max_assets_per_discovery() == (
        settings.asset_discovery_max_assets
    )
    assert service.max_assets_per_discovery(7) == 7


# --------------------------------------------------------------------------- #
# Descubrimiento: la conversión a activos
# --------------------------------------------------------------------------- #


def test_un_host_produce_subdominio_e_ips() -> None:
    """Cada host con dirección produce un subdominio y una fila por IP.

    No son la misma cosa: el subdominio es una superficie expuesta y la IP es una máquina
    que puede servir varios nombres. Colapsarlas perdería una de las dos.
    """

    informe = discovery.DiscoveryReport(
        candidates_tried=2,
        hosts=(
            discovery.ResolvedHost(
                fqdn="www.empresa.com",
                addresses=("203.0.113.10", "2001:db8::1"),
                cname="cdn.proveedor.com",
            ),
        ),
    )
    filas = discovery.report_to_assets(informe)
    tipos = [(tipo, valor) for tipo, valor, _, _ in filas]
    assert (AssetTypeEnum.SUBDOMAIN, "www.empresa.com") in tipos
    assert (AssetTypeEnum.IP_ADDRESS, "203.0.113.10") in tipos
    assert (AssetTypeEnum.IP_ADDRESS, "2001:db8::1") in tipos
    assert len(filas) == 3


def test_un_cname_sin_ip_no_genera_activo() -> None:
    """Un `CNAME` sin dirección final no es un activo.

    Aparece en la consulta intermedia, no en el resultado. Registrarlo metería en el
    inventario un nombre que el cliente no controla directamente y que puede no resolver
    nunca.
    """

    informe = discovery.DiscoveryReport(
        candidates_tried=1,
        hosts=(
            discovery.ResolvedHost(
                fqdn="alias.empresa.com",
                addresses=(),
                cname="destino.otro.com",
            ),
        ),
    )
    assert discovery.report_to_assets(informe) == []


def test_una_ip_en_la_columna_de_subdominio_no_pasa() -> None:
    """Un `CNAME` es un nombre y no se guarda como `IP_ADDRESS`.

    Guardarlo en la columna de direcciones produciría un inventario con IPs que no son IPs, y
    cualquier consumidor que esperara una dirección fallaría.
    """

    from backend.apps.assets.discovery import _es_ip

    assert _es_ip("203.0.113.10") is True
    assert _es_ip("2001:db8::1") is True
    assert _es_ip("cdn.proveedor.com") is False


# --------------------------------------------------------------------------- #
# Candidatos: la lista viene de la configuración y se acota conservando el orden
# --------------------------------------------------------------------------- #


def test_los_candidatos_se_recortan_conservando_el_orden() -> None:
    """Al recortar la lista se conserva el orden de la configuración.

    El orden es el de prioridad que escribió quien despliega. Recortarlo por sorpresa
    descartaría justo los prefijos que aparecen primero, que son los que el despliegue
    considera más importantes.
    """

    assert discovery.candidate_prefixes("www,api,admin,staging") == (
        "www",
        "api",
        "admin",
        "staging",
    )
    assert discovery.candidate_prefixes("www,api,admin,staging", 2) == ("www", "api")


def test_los_candidatos_por_defecto_vienen_de_la_configuracion() -> None:
    """Sin lista explícita, la que se usa es la de `Settings`.

    Es la mitad de la prueba anterior: sin esto, un valor fijo en el módulo pasaría la
    primera y la lista de despliegue no se usaría nunca.
    """

    sin_argumentos = discovery.candidate_prefixes()
    from_config = discovery.candidate_prefixes(
        settings.asset_discovery_wordlist,
        settings.asset_discovery_max_candidates,
    )
    assert sin_argumentos == from_config
    assert sin_argumentos[0] == "www"


def test_los_candidatos_se_normalizan_y_deduplican() -> None:
    """Comas, espacios, mayúsculas, puntos finales y repeticiones se colapsan una vez."""

    assert discovery.candidate_prefixes(" WWW , api,www,  ,API, mail ") == (
        "www",
        "api",
        "mail",
    )
    assert discovery.candidate_prefixes("www.") == ("www",)


def test_una_lista_vacia_no_produce_candidatos() -> None:
    """Sin prefijos configurados no hay candidatos, y no un error.

    Un despliegue con la lista vacía tiene que descubrir cero subdominios, no fallar. La
    lista vacía es una configuración válida: la tarea lo registra y termina.
    """

    assert discovery.candidate_prefixes(" , , ") == ()
    assert discovery.candidate_prefixes("") == ()

# --------------------------------------------------------------------------- #
# Filtros y paginacion del listado de dominios
# --------------------------------------------------------------------------- #
#
# ## Por que estas pruebas miran `total` y no solo `items`
#
# Porque un filtro que no se aplica no falla de forma visible: devuelve la lista entera y la
# pantalla "funciona". Lo que delata el fallo es el numero, asi que se afirma sobre `total` y
# sobre **todas** las filas, nunca sobre `items[0]`.


async def _listar_dominios(cabeceras: dict[str, str], parametros: str = "") -> Response:
    """La **respuesta**, no el cuerpo ya parseado.

    Se devuelve el `Response` y el cuerpo se lee con `.json()` en cada aserción por la misma
    razón que en `test_pr_reviews_api.py`: `json()` devuelve `Any`, y con un `dict[str, object]`
    el tipado estricto se queja de cada `cuerpo["items"][0]` —que es ruido en una prueba— mientras
    que con `Any` el fallo real, que es una clave que no existe, sale en el `KeyError`.
    """

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get(
            f"/api/v1/assets/domains{parametros}", headers=cabeceras
        )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta


@pytest.mark.asyncio
async def test_el_listado_de_dominios_trae_total_limit_y_offset(
    integration_session: AsyncSession,
) -> None:
    """La paginacion viaja en la respuesta, y `total` cuenta los dominios del workspace.

    Sin `limit`/`offset` no hay forma de filtrar nada: el buscador devolveria la superficie
    entera en cada tecla. Y sin `total` la barra de paginacion no puede decir si hay mas.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    for indice in range(3):
        await _dominio(integration_session, org.id, f"empresa{indice}.com")

    cuerpo = (await _listar_dominios(cabeceras)).json()

    assert cuerpo["total"] == 3
    assert len(cuerpo["items"]) == 3
    assert cuerpo["limit"] == 25
    assert cuerpo["offset"] == 0


@pytest.mark.asyncio
async def test_el_buscador_de_dominios_trata_los_comodines_como_literales(
    integration_session: AsyncSession,
) -> None:
    """`_` y `%` se buscan literales en el nombre de dominio.

    El caso real es `_`, porque es el separador de wildcard de DNS: `acme_corp.com` y
    `acmeXcorp.com` son nombres validos y distintos, y sin escapar uno devuelve tambien el
    otro. Y `%` sin escapar devuelve la superficie entera, porque `%` casa con cualquier cosa.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    await _dominio(integration_session, org.id, "acme_corp.com")
    await _dominio(integration_session, org.id, "acmeXcorp.com")
    await _dominio(integration_session, org.id, "descuento100%off.com")
    await _dominio(integration_session, org.id, "descuento1000off.com")

    con_guion_bajo = (await _listar_dominios(cabeceras, "?search=acme_corp")).json()
    con_porcentaje = (await _listar_dominios(cabeceras, "?search=100%25off")).json()
    solo_porcentaje = (await _listar_dominios(cabeceras, "?search=%25")).json()

    assert con_guion_bajo["total"] == 1
    assert con_guion_bajo["items"][0]["domain_name"] == "acme_corp.com"
    assert con_porcentaje["total"] == 1
    assert con_porcentaje["items"][0]["domain_name"] == "descuento100%off.com"
    # `%` a secas devuelve **una** fila, la que de verdad lleva el simbolo, y no las cuatro.
    assert solo_porcentaje["total"] == 1
    assert solo_porcentaje["items"][0]["domain_name"] == "descuento100%off.com"


@pytest.mark.asyncio
async def test_el_filtro_de_dominios_por_estado_separa_verificados_de_pendientes(
    integration_session: AsyncSession,
) -> None:
    """`VERIFIED` y `PENDING` devuelven conjuntos disjuntos, y omitirlo devuelve los dos.

    Se comprueba el `total` de las tres peticiones porque el fallo tipico —ignorar el filtro y
    devolver la lista entera— solo se ve en el numero.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    await _dominio(integration_session, org.id, "verificado.com", verificado=True)
    await _dominio(integration_session, org.id, "pendiente.com", verificado=False)

    todos = (await _listar_dominios(cabeceras)).json()
    verificados = (await _listar_dominios(cabeceras, "?status=VERIFIED")).json()
    pendientes = (await _listar_dominios(cabeceras, "?status=PENDING")).json()

    assert todos["total"] == 2
    assert verificados["total"] == 1
    assert verificados["items"][0]["domain_name"] == "verificado.com"
    assert pendientes["total"] == 1
    assert pendientes["items"][0]["domain_name"] == "pendiente.com"


@pytest.mark.asyncio
async def test_un_estado_de_dominio_que_no_existe_es_422(
    integration_session: AsyncSession,
) -> None:
    """Un valor inventado se rechaza, en vez de devolver una lista vacia sin avisar.

    El `422` es lo que distingue "el desplegable mando algo que el servidor no entiende" de
    "este workspace no tiene dominios con ese estado". Lo segundo es un resultado legitimo y el
    primero es un fallo de programacion, asi que no pueden parecerse.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    await _dominio(integration_session, org.id, "pendiente.com")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get(
            "/api/v1/assets/domains?status=INVENTADO", headers=cabeceras
        )

    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.asyncio
async def test_el_rango_de_dominios_usa_la_fecha_de_alta_y_el_ultimo_dia_entra_entero(
    integration_session: AsyncSession,
) -> None:
    """Los cuatro bordes del rango, con el dia final **entero**.

    Y va sobre `created_at`, no sobre `verified_at`, que es `NULL` en todos los dominios
    pendientes: un filtro sobre ella no recorta filas, las **borra**, y desaparecerian
    justo los que el usuario quiere ver cuando pregunta por los que le faltan.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    ayer = await _dominio(
        integration_session,
        org.id,
        "ayer.com",
        created_at=datetime(2026, 3, 9, 23, 59, tzinfo=UTC),
    )
    primer_dia = await _dominio(
        integration_session,
        org.id,
        "primer.com",
        created_at=datetime(2026, 3, 10, 0, 0, tzinfo=UTC),
    )
    ultimo_dia = await _dominio(
        integration_session,
        org.id,
        "ultimo.com",
        created_at=datetime(2026, 3, 12, 23, 59, 59, tzinfo=UTC),
    )
    dia_siguiente = await _dominio(
        integration_session,
        org.id,
        "siguiente.com",
        created_at=datetime(2026, 3, 13, 0, 0, tzinfo=UTC),
    )

    rango = (
        await _listar_dominios(cabeceras, "?created_from=2026-03-10&created_to=2026-03-12")
    ).json()
    invertido = (
        await _listar_dominios(cabeceras, "?created_from=2026-03-12&created_to=2026-03-10")
    ).json()

    assert rango["total"] == 2
    assert {str(item["id"]) for item in rango["items"]} == {
        str(primer_dia.id),
        str(ultimo_dia.id),
    }
    assert str(ayer.id) not in {str(item["id"]) for item in rango["items"]}
    assert str(dia_siguiente.id) not in {str(item["id"]) for item in rango["items"]}
    # Un rango invertido no es un `422`: las dos condiciones son incompatibles por
    # construccion, asi que la lista vacia ya es la respuesta que corresponde.
    assert invertido["total"] == 0


@pytest.mark.asyncio
async def test_el_rango_de_dominios_no_borra_los_pendientes(
    integration_session: AsyncSession,
) -> None:
    """Un dominio sin `verified_at` sigue apareciendo cuando se toca cualquiera de las dos fechas.

    Es la otra mitad del rango, y es la que se Lee mal: `verified_at` es `NULL` en todos los
    pendientes, asi que un filtro sobre ella los deja fuera y el usuario leeria que sus dominios
    se han perdido.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    await _dominio(
        integration_session,
        org.id,
        "pendiente.com",
        verificado=False,
        created_at=datetime(2026, 3, 11, 12, 0, tzinfo=UTC),
    )

    cuerpo = (
        await _listar_dominios(
            cabeceras, "?created_from=2026-03-11&created_to=2026-03-11&status=PENDING"
        )
    ).json()

    assert cuerpo["total"] == 1
    assert cuerpo["items"][0]["domain_name"] == "pendiente.com"
    assert cuerpo["items"][0]["verified_at"] is None


@pytest.mark.asyncio
async def test_los_filtros_de_dominios_no_amplian_la_vista_del_tenant(
    integration_session: AsyncSession,
) -> None:
    """R3 tambien con los filtros nuevos: buscar no es una puerta trasera.

    Se mira lo que **no** devuelve tanto como lo que si, porque un buscador es la forma mas
    facil de meter en la consulta una condicion que el llamador elige.
    """

    _user, org_a, cabeceras_a = await _tenant(integration_session, prefijo="alpha")
    _otro, org_b, _cabeceras_b = await _tenant(integration_session, prefijo="beta")
    await _dominio(integration_session, org_a.id, "solo-de-alfa.com")
    await _dominio(integration_session, org_b.id, "solo-de-beta.com")

    ajeno = (await _listar_dominios(cabeceras_a, "?search=solo-de-beta")).json()
    propio = (await _listar_dominios(cabeceras_a, "?search=solo-de-alfa")).json()

    # Buscar el dominio del otro workspace devuelve cero, no su fila: el filtro de organizacion
    # va antes que el buscador en la misma clausula `WHERE`, no despues de filtrar en memoria.
    assert ajeno["total"] == 0
    assert ajeno["items"] == []
    assert propio["total"] == 1
    assert propio["items"][0]["domain_name"] == "solo-de-alfa.com"


@pytest.mark.asyncio
async def test_los_filtros_de_dominios_cuentan_antes_de_paginar(
    integration_session: AsyncSession,
) -> None:
    """`total` cuenta lo que coincide con los filtros, no lo que hay en el workspace.

    Es lo que hace honesta la barra de paginacion: si `total` saliera sin filtrar, el resumen
    "1-25 de 300" prometeria paginas que al pulsarlas saldrian vacias.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    for indice in range(3):
        await _dominio(integration_session, org.id, f"coincide{indice}.com")
    await _dominio(integration_session, org.id, "otro.com")

    primera = (await _listar_dominios(cabeceras, "?search=coincide&limit=2&offset=0")).json()
    segunda = (await _listar_dominios(cabeceras, "?search=coincide&limit=2&offset=2")).json()

    assert primera["total"] == 3
    assert len(primera["items"]) == 2
    assert primera["limit"] == 2
    assert primera["offset"] == 0
    assert segunda["offset"] == 2
    assert len(segunda["items"]) == 1
    assert all(
        "coincide" in str(item["domain_name"])
        for item in list(primera["items"]) + list(segunda["items"])
    )
