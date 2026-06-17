"""
app.auth — Authentication module public API.

Re-exports the most commonly used symbols so consumers can do:

    from app.auth import router, get_current_user, require_role, UserPayload
"""

from app.auth.dependencies import get_current_user, require_role
from app.auth.models import UserPayload
from app.auth.router import router

__all__ = [
    "router",
    "get_current_user",
    "require_role",
    "UserPayload",
]
