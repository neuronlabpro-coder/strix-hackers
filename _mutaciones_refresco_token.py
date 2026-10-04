"""Piloto de mutaciones: cada entrada debe hacer CAER el test que dice.

Cada entrada es una lista de sustituciones sobre el mismo fichero, porque hay defectos que no se
pueden quitar con un solo cambio: por ejemplo, «renueva siempre» son dos guardas —la lectura rápida
y la lectura bloqueante— y quitar solo una no cambia nada.
"""

import os
import pathlib
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
TR = RAIZ / "backend" / "apps" / "repositories" / "token_refresh.py"
OA = RAIZ / "backend" / "apps" / "repositories" / "oauth.py"
RO = RAIZ / "backend" / "apps" / "repositories" / "router.py"
TEST = "backend/tests/test_refresh_token_credencial.py"

PREVIA = "    if caduca is None or caduca > limite:\n        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)"
BLOQUEADA = (
    "    caduca = credential.token_expires_at\n"
    "    if caduca is not None and caduca > limite:\n"
    "        await session.commit()\n"
    "        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)\n"
)

MUTACIONES: list[tuple[str, pathlib.Path, list[tuple[str, str]], str]] = [
    (
        "no escribe el token de acceso nuevo",
        TR,
        [
            (
                "    credential.encrypted_access_token = encrypt_organization_credential(\n"
                "        token.access_token,",
                '    credential.encrypted_access_token = encrypt_organization_credential(\n'
                '        "gho" + "z" * 36,',
            )
        ],
        "test_una_credencial_caducada_se_renueva_y_el_inventario_responde_200",
    ),
    (
        "no guarda el refresh_token rotado",
        TR,
        [
            (
                "    if token.refresh_token:\n"
                "        credential.encrypted_refresh_token = encrypt_organization_credential(\n"
                "            token.refresh_token,",
                "    if False:\n"
                "        credential.encrypted_refresh_token = encrypt_organization_credential(\n"
                "            token.refresh_token,",
            )
        ],
        "test_la_renovacion_consume_el_refresh_antiguo_y_guarda_el_que_rota_el_proveedor",
    ),
    (
        "desaparecen las DOS guardas de vigencia (renueva siempre)",
        TR,
        [
            (PREVIA, "    if caduca is None:\n        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)"),
            (
                BLOQUEADA,
                "    caduca = credential.token_expires_at\n"
                "    if False:\n"
                "        await session.commit()\n"
                "        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)\n",
            ),
        ],
        "test_un_token_vigente_no_llama_al_proveedor",
    ),
    (
        "el margen de anticipacion se pone a cero",
        TR,
        [
            (
                "        else timedelta(seconds=settings.git_token_refresh_margin_seconds)",
                "        else timedelta(seconds=0)",
            )
        ],
        "test_un_token_que_caduca_dentro_del_margen_se_renueva_antes_de_caducar",
    ),
    (
        "un PAT con refresh antiguo se renueva",
        TR,
        [
            (
                PREVIA,
                "    if caduca is not None and caduca > limite:\n"
                "        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)",
            )
        ],
        "test_un_token_personal_no_se_renueva_nunca_aunque_le_quede_un_refresh_antiguo",
    ),
    (
        "el rechazo del refresh_token se traduce a 502",
        RO,
        [
            (
                "    if resultado.estado is EstadoDeRefresh.NO_DISPONIBLE:",
                "    if resultado.estado in {EstadoDeRefresh.NO_DISPONIBLE, EstadoDeRefresh.RECHAZADO}:",
            )
        ],
        "test_el_refresh_token_rechazado_responde_410_pidiendo_reconectar",
    ),
    (
        "la caida del proveedor se traduce a 410",
        RO,
        [
            (
                "    if resultado.estado is EstadoDeRefresh.NO_DISPONIBLE:\n"
                "        raise HTTPException(\n"
                "            status_code=status.HTTP_502_BAD_GATEWAY,",
                "    if resultado.estado in {EstadoDeRefresh.NO_DISPONIBLE, EstadoDeRefresh.SIN_RENOVACION}:\n"
                "        raise HTTPException(\n"
                "            status_code=status.HTTP_410_GONE,",
            )
        ],
        "test_un_proveedor_caido_durante_el_refresco_responde_502_y_no_410",
    ),
    (
        "sin OAuth App se responde 410 en vez de 503",
        RO,
        [
            (
                "    if resultado.estado is EstadoDeRefresh.NO_CONFIGURADO:\n"
                "        raise HTTPException(\n"
                "            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,",
                "    if resultado.estado is EstadoDeRefresh.NO_CONFIGURADO:\n"
                "        raise HTTPException(\n"
                "            status_code=status.HTTP_410_GONE,",
            )
        ],
        "test_sin_oauth_app_configurada_el_refresco_responde_503",
    ),
    (
        "el aviso de rechazo imprime str(error) en vez del tipo",
        TR,
        [
            (
                '            "El proveedor no devolvió un token renovado: organization=%s provider=%s "\n'
                '            "rechazo=%s",\n'
                "            organization_id,\n"
                "            provider.value,\n"
                "            type(error).__name__,",
                '            "El proveedor no devolvió un token renovado: organization=%s provider=%s "\n'
                '            "rechazo=%s",\n'
                "            organization_id,\n"
                "            provider.value,\n"
                "            str(error),",
            )
        ],
        "test_el_rechazo_del_refresh_token_no_saca_el_secreto_a_la_respuesta_ni_al_log",
    ),
    (
        "el log de la renovacion imprime el token de acceso en claro",
        TR,
        [
            (
                '            "Credencial Git renovada: organization=%s provider=%s caduca_en=%s",\n'
                "            organization_id,\n"
                "            provider.value,\n"
                "            nueva_caducidad.isoformat(),",
                '            "Credencial Git renovada: organization=%s provider=%s caduca_en=%s",\n'
                "            organization_id,\n"
                "            provider.value,\n"
                "            nueva_caducidad.isoformat() + ' ' + token.access_token,",
            )
        ],
        "test_la_renovacion_no_escribe_ningun_secreto_en_el_log",
    ),
    (
        "el aviso de expires_in ausente baja a debug",
        TR,
        [
            (
                "        logger.warning(\n"
                '            "El proveedor no devolvió expires_in al renovar la credencial',
                "        logger.debug(\n"
                '            "El proveedor no devolvió expires_in al renovar la credencial',
            )
        ],
        "test_una_renovacion_sin_expires_in_avisa_en_vez_de_callarse",
    ),
    (
        "la lectura previa deja de filtrar por organization_id",
        TR,
        [
            (
                "        ).where(\n"
                "            GitCredential.organization_id == organization_id,\n"
                "            GitCredential.provider == provider,\n"
                "        )\n"
                "    )\n"
                "    fila = prelectura.one_or_none()",
                "        ).where(GitCredential.provider == provider)\n"
                "    )\n"
                "    fila = prelectura.one_or_none()",
            )
        ],
        "test_renovar_una_organizacion_no_toca_la_credencial_de_otra",
    ),
    (
        "la renovacion se sale del try que traduce CryptoError",
        RO,
        [
            (
                "        await _exigir_credencial_vigente(session, organization_id, provider)\n"
                "        client = await build_organization_client(session, organization_id, provider)",
                "        pass\n"
                "        client = await build_organization_client(session, organization_id, provider)\n"
                "    await _exigir_credencial_vigente(session, organization_id, provider)\n"
                "    try:",
            )
        ],
        "test_un_refresh_token_que_no_se_puede_descifrar_responde_500",
    ),
    (
        "se quita el SELECT ... FOR UPDATE",
        TR,
        [
            (
                "        .with_for_update()\n        .execution_options(populate_existing=True)",
                "        .execution_options(populate_existing=True)",
            )
        ],
        "test_dos_refrescos_simultaneos_llaman_una_vez_al_proveedor",
    ),
    (
        "se quita populate_existing de la lectura bloqueante",
        TR,
        [
            (
                "        .with_for_update()\n        .execution_options(populate_existing=True)",
                "        .with_for_update()",
            )
        ],
        "test_dos_refrescos_simultaneos_llaman_una_vez_al_proveedor",
    ),
    (
        "el bloqueo pasa a ser global (pg_advisory_xact_lock)",
        TR,
        [
            (
                "    bloqueada = await session.execute(\n        select(GitCredential)",
                "    await session.execute(text('SELECT pg_advisory_xact_lock(1)'))\n"
                "    bloqueada = await session.execute(\n        select(GitCredential)",
            ),
            ("from sqlalchemy import select", "from sqlalchemy import select, text"),
        ],
        "test_dos_organizaciones_se_renuevan_a_la_vez_sin_bloquearse",
    ),
    (
        "la URL del refresco se escribe a mano para GitHub",
        OA,
        [
            (
                "                config.token_url,\n"
                "                data={\n"
                '                    "client_id": config.client_id,\n'
                '                    "client_secret": config.client_secret,\n'
                '                    "grant_type": "refresh_token",',
                '                "https://github.com/login/oauth/access_token",\n'
                "                data={\n"
                '                    "client_id": config.client_id,\n'
                '                    "client_secret": config.client_secret,\n'
                '                    "grant_type": "refresh_token",',
            )
        ],
        "test_el_refresco_de_gitlab_va_a_su_propio_endpoint",
    ),
    (
        "se vuelve a raise_for_status y no se mira el cuerpo",
        OA,
        [
            (
                '    codigo_de_error = payload.get("error")\n'
                "    if codigo_de_error:\n"
                "        raise OAuthRefreshRejectedError(\n"
                '            f"El proveedor rechazó el refresh token: {_codigo_de_error(codigo_de_error)}"\n'
                "        )\n",
                "    codigo_de_error = None\n",
            )
        ],
        "test_un_200_con_error_de_github_es_un_rechazo_y_no_un_token",
    ),
    (
        "deja de mirar el 5xx antes que el cuerpo",
        OA,
        [
            (
                "    if response.status_code >= 500:\n"
                '        raise OAuthRefreshUnavailableError("El proveedor falló al refrescar el token")\n',
                "",
            )
        ],
        "test_un_5xx_al_refrescar_es_transitorio_y_no_un_rechazo",
    ),
    (
        "se deja de validar que las URLs sean HTTPS",
        OA,
        [
            (
                "    validate_oauth_provider_settings(config)\n    # El límite de longitud",
                "    # El límite de longitud",
            )
        ],
        "test_un_endpoint_sin_https_no_se_llega_a_enviar_el_refresh_token",
    ),
]


def main() -> int:
    fallados: list[str] = []
    for nombre, ruta, cambios, test in MUTACIONES:
        original = ruta.read_text(encoding="utf-8")
        mutado = original
        for viejo, nuevo in cambios:
            if viejo not in mutado:
                print(f"NO ENCONTRADO  {nombre}")
                fallados.append(nombre)
                break
            mutado = mutado.replace(viejo, nuevo, 1)
        else:
            try:
                ruta.write_text(mutado, encoding="utf-8")
                completado = subprocess.run(
                    [
                        "uv", "run", "--project", "backend", "pytest", "-c", "backend/pyproject.toml",
                        f"{TEST}::{test}", "-q", "--tb=line",
                    ],
                    cwd=RAIZ,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=900,
                    check=False,
                    env={**dict(os.environ), "DB_CONNECT_TIMEOUT_SECONDS": "60"},
                )
            finally:
                ruta.write_text(original, encoding="utf-8")
            cayo = completado.returncode != 0
            print(f"{'CAYO' if cayo else 'NO CAYO':8} {nombre}  ->  {test}")
            if not cayo:
                fallados.append(nombre)
                print(
                    "        "
                    + ((completado.stdout or "") + (completado.stderr or "")).strip().splitlines()[-1][:160]
                )
    print()
    if fallados:
        print(f"MUTACIONES QUE NO HICIERON CAER SU TEST: {len(fallados)}")
        for nombre in fallados:
            print(f"  - {nombre}")
        return 1
    print(f"Las {len(MUTACIONES)} mutaciones hicieron caer su test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
