"""Compatibility exports for the Vietnam social service facade."""

from .vietnam_social import (
    VietnamSocialService,
    create_vietnam_social_service_from_env,
)

__all__ = ["VietnamSocialService", "create_vietnam_social_service_from_env"]
