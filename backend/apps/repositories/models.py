"""Modelos de repositorios, credenciales Git y revisiones de Pull Requests."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.apps.organizations.models import TimestampMixin
from backend.core.database import Base


class GitProviderEnum(StrEnum):
    """Proveedores Git soportados por la plataforma."""

    GITHUB = "GITHUB"
    GITLAB = "GITLAB"
    BITBUCKET = "BITBUCKET"
    GITEA = "GITEA"


class PRReviewStatusEnum(StrEnum):
    """Estados de una revisión automática de Pull/Merge Request."""

    QUEUED = "QUEUED"
    SCANNING = "SCANNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"


def generate_webhook_secret() -> str:
    """Genera un secreto HMAC aleatorio para un repositorio nuevo."""

    return secrets.token_urlsafe(32)


class Repository(TimestampMixin, Base):
    """Repositorio remoto pertenece a una única organización."""

    __tablename__ = "repositories"
    __table_args__ = (
        Index("ix_repositories_org_provider", "organization_id", "provider"),
        UniqueConstraint(
            "provider",
            "remote_repo_id",
            name="uq_repositories_provider_remote",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_repositories_id_organization",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[GitProviderEnum] = mapped_column(
        SQLEnum(GitProviderEnum, name="git_provider_enum"), nullable=False
    )
    remote_repo_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    clone_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    default_branch: Mapped[str] = mapped_column(
        String(128), nullable=False, default="main", server_default="main"
    )
    pr_reviews_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    webhook_secret: Mapped[str] = mapped_column(
        String(128), nullable=False, default=generate_webhook_secret
    )
    webhook_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class GitCredential(TimestampMixin, Base):
    """Tokens cifrados de un proveedor y organización."""

    __tablename__ = "git_credentials"
    __table_args__ = (
        Index(
            "ix_git_credentials_org_provider",
            "organization_id",
            "provider",
            unique=True,
        ),
        CheckConstraint(
            "encrypted_access_token ~ '^v1\\.[A-Za-z0-9_-]{16,}\\.[A-Za-z0-9_-]{22,}$'",
            name="ck_git_credentials_access_token_encrypted",
        ),
        CheckConstraint(
            "encrypted_refresh_token IS NULL OR encrypted_refresh_token ~ "
            "'^v1\\.[A-Za-z0-9_-]{16,}\\.[A-Za-z0-9_-]{22,}$'",
            name="ck_git_credentials_refresh_token_encrypted",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[GitProviderEnum] = mapped_column(
        SQLEnum(GitProviderEnum, name="git_provider_enum"), nullable=False
    )
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    installation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PullRequestReview(TimestampMixin, Base):
    """Revisión de seguridad asociada a un repositorio y opcionalmente a un run."""

    __tablename__ = "pull_request_reviews"
    __table_args__ = (
        Index("ix_pr_reviews_repo_number", "repository_id", "pr_number"),
        UniqueConstraint(
            "repository_id",
            "pr_number",
            "commit_sha",
            "base_sha",
            name="uq_pr_reviews_repository_pr_commit",
        ),
        ForeignKeyConstraint(
            ["repository_id", "organization_id"],
            ["repositories.id", "repositories.organization_id"],
            name="fk_pr_reviews_repository_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint("pr_number > 0", name="ck_pr_reviews_number_positive"),
        CheckConstraint(
            "issues_caught_critical >= 0 AND issues_caught_high >= 0",
            name="ck_pr_reviews_issue_counts_nonnegative",
        ),
        CheckConstraint(
            "base_sha IS NULL OR base_sha ~ '^[0-9a-fA-F]{40,64}$'",
            name="ck_pr_reviews_base_sha",
        ),
        CheckConstraint(
            "head_clone_url IS NULL OR head_clone_url ~ '^https://[^/]+/.+'",
            name="ck_pr_reviews_head_clone_url",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pentest_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_title: Mapped[str] = mapped_column(String(512), nullable=False)
    pr_author: Mapped[str] = mapped_column(String(255), nullable=False)
    source_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    target_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_clone_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[PRReviewStatusEnum] = mapped_column(
        SQLEnum(PRReviewStatusEnum, name="pr_review_status_enum"),
        nullable=False,
        default=PRReviewStatusEnum.QUEUED,
        server_default=PRReviewStatusEnum.QUEUED.name,
        index=True,
    )
    issues_caught_critical: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    issues_caught_high: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    merge_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    comment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
