"""Guard de ruta que exige privilegios de SuperAdmin."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status

from backend.apps.organizations.models import User
from backend.core.middleware import get_current_user


async def require_superuser(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Falla cerrado cuando el usuario no tiene `is_superuser`."""

    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere privilegio de SuperAdmin",
        )
    return current_user


SuperuserDependency = Annotated[User, Depends(require_superuser)]
