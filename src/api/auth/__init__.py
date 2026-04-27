"""
api.auth — Authentication module public API.

Re-exports the most commonly used symbols so consumers can do:

    from api.auth import router, get_current_user, require_role, UserPayload
"""

from api.auth.dependencies import get_current_user, require_role
from api.auth.models import UserPayload
from api.auth.router import router

__all__ = [
    "router",
    "get_current_user",
    "require_role",
    "UserPayload",
]
