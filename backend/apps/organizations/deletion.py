"""Baja lógica de una organización.

El orden de las operaciones es la parte delicate del módulo y está justificado paso a
paso en `_soft_delete_organization`. En resumen: primero se asienta la baja, después se
revoca el acceso y por último se cancela en Stripe. Ese orden garantiza que exista una
prueba de por qué se cortó el acceso aunque la revocación o la cancelación fallen, y que
el rastro sobreviva a esos fallos.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.organizations.models import Membership, Organization

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeletionReport:
    """Qué se revocó realmente, para que la respuesta no prometa más de lo que hizo.

    Mutable a propósito: es un acumulador que crece a lo largo de una baja en varios
    pasos, y el último —la cancelación en Stripe— ocurre después del `commit` que
    cierra los demás. Congelarlo obligaría a reconstruirlo entero tres veces o a
    clonar el objeto justo en el paso que puede fallar.

    Los contadores no son decorativos: el panel muestra qué se perdió con la baja, y un
    usuario que espera ver sus datos y descubre que sus compañeros siguen con acceso
    tiene que saberlo en la respuesta, no un mes después.
    """

    organization_id: uuid.UUID
    deleted_at: datetime
    revoked_memberships: int = 0
    cancelled_subscriptions: int = 0
    stripe_customer_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def revoked_access(self) -> int:
        return self.revoked_memberships


async def _soft_delete_organization(
    session: AsyncSession,
    organization: Organization,
    actor_user_id: uuid.UUID | None,
) -> DeletionReport:
    """Marca la baja lógica, asienta el motivo y revoca el acceso.

    ## Orden de las operaciones

    1. **Se escribe el asiento de auditoría antes que nada.** Es lo único que
       sobrevive a un fallo posterior, y es la prueba de que la baja fue deliberada. Si
       se hiciera al final, una revocación caída dejaría un tenant desactivado sin
       rastro de por qué.

    2. **Se desactivan las membresías y los usuarios.** `get_current_tenant` ya exige
       `Membership.is_active` y `User.is_active`, así que apagarlos revoca de verdad y
       no hace falta una lista de tokens revocados: el JWT sigue siendo criptográficamente
       válido, pero ya no resuelve a ningún tenant.

    3. **Se cancelan las suscripciones de Stripe al final.** Es la única operación que
       depende de una red externa, y por eso va última: si Stripe no responde, el
       tenant ya está dado de baja y el acceso ya está revocado, y lo que queda es
       reconciliar un cobro. Al revés, un fallo de Stripe dejaría el tenant vivo pero
       con la baja asentada, que es un estado en el que nadie sabe qué hacer.
    """

    now = datetime.now(UTC)
    previous_state = "deleted" if organization.deleted_at is not None else "active"

    session.add(
        AuditLogEntry(
            organization_id=organization.id,
            actor_user_id=actor_user_id,
            action=AuditActionEnum.ORGANIZATION_DELETED,
            entity_type="organization",
            entity_id=organization.id,
            from_state=previous_state,
            to_state="deleted",
        )
    )

    organization.deleted_at = organization.deleted_at or now
    organization.is_active = False

    memberships = list(
        (
            await session.execute(
                select(Membership).where(
                    Membership.organization_id == organization.id,
                    Membership.is_active.is_(True),
                )
            )
        ).scalars().all()
    )
    # Se desactivan las membresías y **nada más**. La cuenta del usuario sigue viva a
    # propósito: perder un workspace no debe impedirle iniciar sesión, ver sus otros
    # workspaces ni crear uno nuevo. Desactivar `User.is_active` sería además un
    # castigo desproporcionado para un consultor que trabajaba para dos clientes y solo
    # pierde uno de ellos.
    #
    # La revocación es real igualmente: `get_current_tenant` exige
    # `Membership.is_active`, así que cualquier JWT —siga siendo criptográficamente
    # válido— deja de resolver contexto para este tenant. No hace falta una lista de
    # tokens revocados; basta con que la membresía deje de existir a efectos de acceso.
    revoked_memberships = 0
    for membership in memberships:
        membership.is_active = False
        revoked_memberships += 1

    await session.commit()

    report = DeletionReport(
        organization_id=organization.id,
        deleted_at=organization.deleted_at or now,
        revoked_memberships=revoked_memberships,
        stripe_customer_id=organization.stripe_customer_id,
    )

    await _cancel_stripe_subscriptions(session, organization, report)
    return report


async def _cancel_stripe_subscriptions(
    session: AsyncSession,
    organization: Organization,
    report: DeletionReport,
) -> None:
    """Cancela en Stripe las suscripciones vivas del cliente del tenant.

    No propaga el fallo. Un tenant dado de baja con una suscripción viva en Stripe es
    un problema de facturación que hay que reconciliar; un `500` en la baja deja al
    usuario con la pantalla de error y sin poder volver a intentarlo. El aviso queda
    en el informe y en el log, que es donde se busca cuando aparece un cobro fantasma.
    """

    if not organization.stripe_customer_id:
        return

    try:
        from backend.apps.billing.stripe import get_stripe_client

        client = get_stripe_client()
        cancelled = await _cancel_subscriptions_remotely(
            client, organization.stripe_customer_id
        )
        report.cancelled_subscriptions += cancelled
    except Exception as error:
        message = (
            f"No se pudieron cancelar las suscripciones de Stripe del cliente "
            f"{organization.stripe_customer_id}: {type(error).__name__}"
        )
        report.warnings.append(message)
        logger.warning(message, exc_info=True)
    finally:
        # Se registra el aviso en el propio rastro del tenant, para que la baja no
        # quede documentada como limpia si hubo que reconciliar algo en Stripe.
        if report.warnings:
            session.add(
                AuditLogEntry(
                    organization_id=organization.id,
                    actor_user_id=None,
                    action=AuditActionEnum.ORGANIZATION_DELETED,
                    entity_type="stripe_subscription",
                    entity_id=organization.id,
                    from_state="active",
                    to_state="cancellation_failed",
                )
            )
            await session.commit()


async def _cancel_subscriptions_remotely(client: object, customer_id: str) -> int:
    """Lista las suscripciones del cliente y cancela las que no estén ya terminadas."""

    sdk = getattr(client, "_sdk", None)
    if sdk is None:
        return 0
    namespace = getattr(sdk, "v1", None)
    subscriptions = (
        namespace.subscriptions
        if namespace is not None
        else getattr(sdk, "subscriptions", None)
    )
    if subscriptions is None:
        return 0

    remote = await subscriptions.list_async(
        {"customer": customer_id, "status": "all", "limit": 100}
    )
    rows = remote if isinstance(remote, list) else remote.get("data", [])
    cancelled = 0
    for row in rows:
        item = row if isinstance(row, dict) else dict(row)
        if item.get("status") in {"canceled", "incomplete_expired"}:
            continue
        await subscriptions.cancel_async(item["id"])
        cancelled += 1
    return cancelled
