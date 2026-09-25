from typing import cast

from sqlalchemy import MetaData, Table

from backend.apps.organizations.models import (
    Invitation,
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)


def test_models_register_all_phase_one_tables() -> None:
    metadata: MetaData = Organization.metadata

    assert set(metadata.tables).issuperset({"organizations", "users", "memberships", "invitations"})
    assert [tier.value for tier in PlanTierEnum] == ["FREE", "PRO", "ENTERPRISE"]
    assert [role.value for role in RoleEnum] == ["admin", "member"]


def test_membership_has_unique_organization_user_index_and_cascades() -> None:
    membership_table = cast(Table, Membership.__table__)
    organization_foreign_key = next(
        foreign_key
        for foreign_key in membership_table.foreign_keys
        if foreign_key.parent is membership_table.c.organization_id
    )
    user_foreign_key = next(
        foreign_key
        for foreign_key in membership_table.foreign_keys
        if foreign_key.parent is membership_table.c.user_id
    )

    assert organization_foreign_key.ondelete == "CASCADE"
    assert user_foreign_key.ondelete == "CASCADE"
    assert "ix_membership_org_user" in {index.name for index in membership_table.indexes}
    assert next(
        index for index in membership_table.indexes if index.name == "ix_membership_org_user"
    ).unique


def test_invitation_has_organization_fk_and_unique_token_index() -> None:
    invitation_table = cast(Table, Invitation.__table__)
    organization_foreign_key = next(iter(invitation_table.foreign_keys))

    assert organization_foreign_key.ondelete == "CASCADE"
    assert invitation_table.c.token.unique is True
    assert "ix_invitations_token" in {index.name for index in invitation_table.indexes}


def test_user_email_is_unique_and_indexed_and_starts_unverified() -> None:
    user_table = cast(Table, User.__table__)

    assert user_table.c.email.unique is True
    assert user_table.c.email_verified.default is not None
    assert user_table.c.email_verified.server_default is not None
    assert "false" in str(user_table.c.email_verified.server_default).lower()
    assert "ix_users_email" in {index.name for index in user_table.indexes}
    assert user_table.c.email_verification_token_hash.unique is True
    assert user_table.c.email_verification_expires_at.nullable is True
