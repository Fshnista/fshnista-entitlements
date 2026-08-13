"""
FSHNiSTA Platform Entitlements
Per-module entitlement rules

Purpose
    Below Pro, every module is a wall: Storefront, Booking Manager and Map
    Discovery all deny free tier outright, decided across all three
    2026-08-13 after Booking Manager was found still giving free tier its
    own 5 bookings a month with no Pro required, the one module that had
    never actually matched "Pro is a key, not a purchase." What differs
    between modules is only the shape of the Pro allowance, and the Merchant
    price that lifts it, both defined explicitly here in one place instead
    of scattered across route handlers in three different repositories.

    Each rule function takes the resolved per-module Tier (FREE, PRO or
    MERCHANT, from get_module_tier / get_module_tier_via_rest) plus whatever
    module-specific usage data it needs, and returns a simple allow/deny
    decision with a human-readable reason. The reason matters, it is what
    gets surfaced to the user as the upgrade prompt copy. **Never write "for
    more" onto a member who is already at the tier being offered** for the
    same reason CLAUDE.md locks: it has already been caught twice, once
    here.

Usage (inside a module's route handler)
    from fshnista_entitlements.entitlement_rules import check_storefront_access

    module_tier = await get_module_tier_via_rest(user_id, Module.STOREFRONT, ...)
    decision = check_storefront_access(module_tier)
    if not decision.allowed:
        raise HTTPException(403, decision.reason)
"""

from __future__ import annotations

from dataclasses import dataclass

from .entitlements import Tier, is_merchant, is_pro

# Pro's included booking allowance. Confirmed bookings only, cancellations
# and no-shows do not count against this, the limit tracks real usage not
# noise. Free tier gets none at all; see the module docstring above.
#
# Named PRO rather than FREE, unlike Booking Manager's own local
# FREE_TIER_MONTHLY_BOOKING_CAP (app/entitlements.py in that repo, not
# imported from here): that service still enforces its cap on its own local
# tier column and free-tier-gets-5 read, and needs both the column and this
# constant's old name retired together when it moves onto get_module_tier.
BOOKING_MANAGER_PRO_MONTHLY_CAP = 5

# Pro's included listing allowance on Storefront. A stock cap, never resets.
STOREFRONT_PRO_LISTING_CAP = 10


@dataclass(frozen=True)
class EntitlementDecision:
    allowed: bool
    reason: str | None = None


# -----------------------------------------------------------------------------
# Storefront
#
# Pro-only door, zero free access: a free-tier member cannot create a
# storefront, cannot list a product, cannot see the seller dashboard.
# Decided deliberately during the Storefront build, full marketplace access
# is the Pro value proposition, there is no diluted free version of it.
#
# Past the door, Pro carries ten listings as a stock cap that never resets.
# Merchant, priced 2026-08-13, removes it. check_storefront_access answers
# "can this member use Storefront at all"; check_storefront_listing_cap
# answers "can they create one more", and is only meaningful once the first
# has already passed.
# -----------------------------------------------------------------------------

def check_storefront_access(tier: Tier) -> EntitlementDecision:
    if is_pro(tier):
        return EntitlementDecision(allowed=True)
    return EntitlementDecision(
        allowed=False,
        reason="Storefront is available on FSHNiSTA Pro. Upgrade to start selling.",
    )


def check_storefront_listing_cap(
    tier: Tier,
    current_listing_count: int,
) -> EntitlementDecision:
    if is_merchant(tier):
        return EntitlementDecision(allowed=True)

    if current_listing_count < STOREFRONT_PRO_LISTING_CAP:
        return EntitlementDecision(allowed=True)

    return EntitlementDecision(
        allowed=False,
        reason=(
            f"You've used all {STOREFRONT_PRO_LISTING_CAP} listings included "
            f"with Pro. Upgrade to Storefront Merchant for unlimited listings."
        ),
    )


# -----------------------------------------------------------------------------
# Booking Manager
#
# Pro-only door, matching Storefront and Map Discovery. Corrected
# 2026-08-13: this file previously let free tier confirm 5 bookings a month
# with no Pro required at all, the one module that never actually matched
# "Pro is a key, not a purchase" (CLAUDE.md), and its own denial copy
# already said "Merchant removes the limit" for a tier nothing checked yet.
# Henry, 2026-08-13, choosing to bring it in line rather than leave it the
# platform's one freemium exception: free tier drops to zero.
#
# Past the door, Pro carries five confirmed bookings a month as a flow cap
# that resets on the 1st. Merchant removes it.
# -----------------------------------------------------------------------------

def check_booking_creation_access(
    tier: Tier,
    confirmed_bookings_this_month: int,
) -> EntitlementDecision:
    if is_merchant(tier):
        return EntitlementDecision(allowed=True)

    if not is_pro(tier):
        return EntitlementDecision(
            allowed=False,
            reason="Booking Manager is available on FSHNiSTA Pro.",
        )

    if confirmed_bookings_this_month < BOOKING_MANAGER_PRO_MONTHLY_CAP:
        return EntitlementDecision(allowed=True)

    return EntitlementDecision(
        allowed=False,
        reason=(
            f"You've used your {BOOKING_MANAGER_PRO_MONTHLY_CAP} bookings this "
            f"month on Pro. Upgrade to Booking Manager Merchant for unlimited "
            f"bookings."
        ),
    )


# Service listing, calendar setup, and accepting deposits all still require
# nothing beyond the Pro door itself, no further gate above it. Only the act
# of confirming a booking past the monthly cap is gated further. This
# function exists so other Booking Manager routes can explicitly assert
# "this action needs Pro and nothing more" rather than leaving it ambiguous
# by omission.
def check_booking_setup_access(tier: Tier) -> EntitlementDecision:
    if is_pro(tier):
        return EntitlementDecision(allowed=True)
    return EntitlementDecision(
        allowed=False,
        reason="Booking Manager is available on FSHNiSTA Pro.",
    )
