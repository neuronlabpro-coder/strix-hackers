from sqlalchemy import Table

from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
    generate_webhook_secret,
)


def test_git_models_define_provider_status_and_required_indexes() -> None:
    repository_table = Repository.__table__
    credential_table = GitCredential.__table__
    review_table = PullRequestReview.__table__

    assert isinstance(repository_table, Table)
    assert isinstance(credential_table, Table)
    assert isinstance(review_table, Table)
    assert [item.value for item in GitProviderEnum] == [
        "GITHUB",
        "GITLAB",
        "BITBUCKET",
        "GITEA",
    ]
    assert [item.value for item in PRReviewStatusEnum] == [
        "QUEUED",
        "SCANNING",
        "PASSED",
        "FAILED",
        "ERROR",
    ]
    assert "ix_repositories_org_provider" in {index.name for index in repository_table.indexes}
    assert "uq_repositories_provider_remote" in {
        constraint.name for constraint in repository_table.constraints
    }
    assert "ix_git_credentials_org_provider" in {
        index.name for index in credential_table.indexes
    }
    assert "ix_pr_reviews_repo_number" in {index.name for index in review_table.indexes}
    assert "fk_pr_reviews_repository_organization" in {
        constraint.name for constraint in review_table.constraints
    }
    assert "uq_pr_reviews_repository_pr_commit" in {
        constraint.name for constraint in review_table.constraints
    }
    assert "ck_pr_reviews_base_sha" in {
        constraint.name for constraint in review_table.constraints
    }
    assert "ck_pr_reviews_head_clone_url" in {
        constraint.name for constraint in review_table.constraints
    }


def test_generated_webhook_secret_is_random_and_non_empty() -> None:
    first = generate_webhook_secret()
    second = generate_webhook_secret()

    assert len(first) >= 32
    assert first != second
