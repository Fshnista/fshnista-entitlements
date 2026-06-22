"""
FSHNiSTA Platform Entitlements
Per-module entitlement rules

Purpose
    The subscription tier in Supabase is platform-wide and binary, free or
    pro. What that tier actually permits is different in every module.
    Storefront has zero free access. Booking Manager allows 5 confirmed
    bookings a month on free. This file is where that divergence is defined
    explicitly, in one place, instead of scattered across route handlers in
    three different repositories.

    Each rule function takes the resolved Tier plus whatever module-specific
    usage data it needs, and returns a simple allow/deny decision with a
    human-readable reason. The reason matters, it is what gets surfaced to
    the user as the upgrade prompt copy.

Usage (inside a module's route handler)
    from fshnista_entitlements.entitlement_rules import check_storefront_access

    decision = check_storefront_access(tier)
    if not decision.allowed:
        raise HTTPException(403, decision.reason)
"""

from __future__ import annotations

from dataclasses import dataclass

from .entitlements import Tier, is_pro

# Free tier booking cap. Confirmed bookings only, cancellations and no-shows
# do not count against this, the limit tracks real usage not noise.
BOOKING_MANAGER_FREE_MONTHLY_CAP = 5


@dataclass(frozen=True)
class EntitlementDecision:
    allowed: bool
    reason: str | None = None


# -----------------------------------------------------------------------------
# Storefront
#
# Pro-only, zero free access. A free-tier user cannot create a storefront,
# cannot list a product, cannot see the seller dashboard. This was decided
# deliberately during the Storefront build, full marketplace access is the
# Pro value proposition, there is no diluted free version of it.
# -----------------------------------------------------------------------------

def check_storefront_access(tier: Tier) -> EntitlementDecision:
    if is_pro(tier):
        return EntitlementDecision(allowed=True)
    return EntitlementDecision(
        allowed=False,
        reason="Storefront is available on FSHNiSTA Pro. Upgrade to start selling.",
    )


# -----------------------------------------------------------------------------
# Booking Manager
#
# Freemium. Free tier providers get the full product, real services, real
# deposits, real client relationships, capped at 5 confirmed bookings per
# calendar month. Pro removes the cap entirely.
# -----------------------------------------------------------------------------

def check_booking_creation_access(
    tier: Tier,
    confirmed_bookings_this_month: int,
) -> EntitlementDecision:
    if is_pro(tier):
        return EntitlementDecision(allowed=True)

    if confirmed_bookings_this_month < BOOKING_MANAGER_FREE_MONTHLY_CAP:
        return EntitlementDecision(allowed=True)

    return EntitlementDecision(
        allowed=False,
        reason=(
            f"You've reached your {BOOKING_MANAGER_FREE_MONTHLY_CAP} free bookings "
            f"this month. Upgrade to Pro for unlimited bookings."
        ),
    )


# Service listing, calendar setup, and accepting deposits all remain fully
# available on free tier. Only the act of confirming a 6th+ booking in a
# calendar month is gated. This function exists so other Booking Manager
# routes can explicitly assert "this action is never gated" rather than
# leaving it ambiguous by omission.
def check_booking_setup_access(tier: Tier) -> EntitlementDecision:
    return EntitlementDecision(allowed=True)
