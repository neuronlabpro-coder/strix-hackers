"""phase3_git_integrations

Revision ID: e1f3a5c7e9b0
Revises: d0e2f4a6b8c9
Create Date: 2026-09-25 15:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f3a5c7e9b0"
down_revision: Union[str, Sequence[str], None] = "d0e2f4a6b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea el plano Git y las revisiones de Pull/Merge Requests."""

    op.create_table(
        "repositories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("GITHUB", "GITLAB", "BITBUCKET", "GITEA", name="git_provider_enum"),
            nullable=False,
        ),
        sa.Column("remote_repo_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=512), nullable=False),
        sa.Column("clone_url", sa.String(length=1024), nullable=False),
        sa.Column("default_branch", sa.String(length=128), server_default="main", nullable=False),
        sa.Column("pr_reviews_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("webhook_secret", sa.String(length=128), nullable=False),
        sa.Column("webhook_id", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "remote_repo_id",
            name="uq_repositories_provider_remote",
        ),
    )
    op.create_index(
        "ix_repositories_org_provider",
        "repositories",
        ["organization_id", "provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_repositories_organization_id"),
        "repositories",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "git_credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("GITHUB", "GITLAB", "BITBUCKET", "GITEA", name="git_provider_enum"),
            nullable=False,
        ),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("installation_id", sa.String(length=128), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_git_credentials_org_provider",
        "git_credentials",
        ["organization_id", "provider"],
        unique=True,
    )
    op.create_index(
        op.f("ix_git_credentials_organization_id"),
        "git_credentials",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "pull_request_reviews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("pr_title", sa.String(length=512), nullable=False),
        sa.Column("pr_author", sa.String(length=255), nullable=False),
        sa.Column("source_branch", sa.String(length=255), nullable=False),
        sa.Column("target_branch", sa.String(length=255), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("QUEUED", "SCANNING", "PASSED", "FAILED", "ERROR", name="pr_review_status_enum"),
            server_default="QUEUED",
            nullable=False,
        ),
        sa.Column("issues_caught_critical", sa.Integer(), server_default="0", nullable=False),
        sa.Column("issues_caught_high", sa.Integer(), server_default="0", nullable=False),
        sa.Column("merge_blocked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("comment_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("pr_number > 0", name="ck_pr_reviews_number_positive"),
        sa.CheckConstraint(
            "issues_caught_critical >= 0 AND issues_caught_high >= 0",
            name="ck_pr_reviews_issue_counts_nonnegative",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["pentest_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pr_reviews_repo_number",
        "pull_request_reviews",
        ["repository_id", "pr_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pull_request_reviews_organization_id"),
        "pull_request_reviews",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pull_request_reviews_repository_id"),
        "pull_request_reviews",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pull_request_reviews_run_id"),
        "pull_request_reviews",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pull_request_reviews_status"),
        "pull_request_reviews",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """Elimina únicamente el esquema Git en entornos de desarrollo."""

    op.drop_table("pull_request_reviews")
    op.drop_table("git_credentials")
    op.drop_table("repositories")
    sa.Enum(name="pr_review_status_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="git_provider_enum").drop(op.get_bind(), checkfirst=True)
