"""
FSHNiSTA Platform Entitlements

The shared entitlement module consumed by every backend microservice
(Booking Manager, Storefront, and future modules) to check a user's
subscription tier and enforce per-module access rules.

See docs/ARCHITECTURE.md in this repo for the full system design.
"""

from .entitlements import (
    Tier,
    SubscriptionState,
    get_subscription_state,
    get_user_tier,
    is_pro,
    invalidate_cache,
)
from .entitlement_rules import (
    EntitlementDecision,
    BOOKING_MANAGER_FREE_MONTHLY_CAP,
    check_storefront_access,
    check_booking_creation_access,
    check_booking_setup_access,
)
from .entitlement_dependency import get_tier, require_pro

__all__ = [
    "Tier",
    "SubscriptionState",
    "get_subscription_state",
    "get_user_tier",
    "is_pro",
    "invalidate_cache",
    "EntitlementDecision",
    "BOOKING_MANAGER_FREE_MONTHLY_CAP",
    "check_storefront_access",
    "check_booking_creation_access",
    "check_booking_setup_access",
    "get_tier",
    "require_pro",
]

__version__ = "1.0.0"
