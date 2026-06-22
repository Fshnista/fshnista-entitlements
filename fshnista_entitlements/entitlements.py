"""
FSHNiSTA Platform Entitlements
Shared entitlements module for Python microservices

Purpose
    Every backend service (Booking Manager, Storefront, future Twin Studio
    gating) imports this module to check a user's subscription tier. This
    replaces the hardcoded stub used in the Storefront build. Same call
    signature, real backend.

Usage
    from fshnista_entitlements.entitlements import get_user_tier, is_pro

    tier = await get_user_tier(user_id, db)
    if not is_pro(tier):
        raise HTTPException(403, "Pro subscription required")

Design notes
    Reads directly from the subscriptions table in Supabase Postgres. Each
    service holds its own connection to the same database it already
    authenticates JWTs against, so this is not a new external dependency,
    it is one more query against infrastructure already in use.

    A short in-process cache (60 seconds) exists because entitlement checks
    happen on most authenticated requests, and re-querying Postgres on every
    single API call for a value that changes maybe once a month is wasted
    round trips. The cache is intentionally short. A user who just paid
    should not be stuck looking like Free for longer than a minute.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import asyncpg


class Tier(str, Enum):
    FREE = "free"
    PRO = "pro"


# Statuses where the user keeps Pro access. Mirrors the same set used in the
# Stripe webhook handler. past_due is intentionally included, a failed card
# charge gets a grace period before access is revoked.
_PRO_GRANTING_STATUSES = {"active", "trialing", "past_due"}


@dataclass(frozen=True)
class SubscriptionState:
    tier: Tier
    status: str
    current_period_end: Optional[str]


class _EntitlementCache:
    """
    Tiny in-process TTL cache keyed by user_id. Not shared across processes
    or instances, each service worker holds its own. That is fine, the cost
    of a stale read for up to 60 seconds is negligible against the cost of
    querying Postgres on every request.
    """

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, SubscriptionState]] = {}

    def get(self, user_id: str) -> Optional[SubscriptionState]:
        entry = self._store.get(user_id)
        if entry is None:
            return None
        expires_at, state = entry
        if time.monotonic() > expires_at:
            del self._store[user_id]
            return None
        return state

    def set(self, user_id: str, state: SubscriptionState) -> None:
        self._store[user_id] = (time.monotonic() + self._ttl, state)

    def invalidate(self, user_id: str) -> None:
        self._store.pop(user_id, None)


_cache = _EntitlementCache()


async def get_subscription_state(
    user_id: str,
    db: asyncpg.Connection,
) -> SubscriptionState:
    """
    Returns the current subscription state for a user. Defaults to free if
    no row exists, which covers every user who has never started a checkout.
    """
    cached = _cache.get(user_id)
    if cached is not None:
        return cached

    row = await db.fetchrow(
        """
        SELECT tier, status, current_period_end
        FROM subscriptions
        WHERE user_id = $1
        """,
        user_id,
    )

    if row is None:
        state = SubscriptionState(tier=Tier.FREE, status="none", current_period_end=None)
    else:
        # Status is the real source of truth, not the stored tier column.
        # The tier column is a convenience snapshot, but if status has
        # drifted (e.g. a cancellation webhook landed but a stale read beat
        # it), deriving from status here is the safety net.
        effective_tier = (
            Tier.PRO if row["status"] in _PRO_GRANTING_STATUSES else Tier.FREE
        )
        state = SubscriptionState(
            tier=effective_tier,
            status=row["status"],
            current_period_end=(
                row["current_period_end"].isoformat() if row["current_period_end"] else None
            ),
        )

    _cache.set(user_id, state)
    return state


async def get_user_tier(user_id: str, db: asyncpg.Connection) -> Tier:
    """Convenience wrapper when only the tier is needed, not full state."""
    state = await get_subscription_state(user_id, db)
    return state.tier


def is_pro(tier: Tier) -> bool:
    return tier == Tier.PRO


def invalidate_cache(user_id: str) -> None:
    """
    Call this after any flow that might have just changed a user's tier,
    e.g. immediately after a successful Checkout redirect, so the next
    request doesn't wait out the cache TTL on stale Free state.
    """
    _cache.invalidate(user_id)
