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
    EntitlementSourceUnavailable,
    get_subscription_state,
    get_subscription_state_via_rest,
    get_user_tier,
    get_user_tier_via_rest,
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
    "EntitlementSourceUnavailable",
    "get_subscription_state",
    "get_subscription_state_via_rest",
    "get_user_tier",
    "get_user_tier_via_rest",
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

# Kept in step with pyproject.toml by hand. These two disagreed until
# 1.1.0: this said 1.0.0 while the package built as 1.0.1, so anything
# reading the version at runtime reported something that was never released.
__version__ = "1.1.0"
