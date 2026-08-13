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

Two transports, one rule
    The design note above assumes every service holds a connection to the
    Supabase database. That assumption is false for at least two services and
    has cost real outages because of it:

        Map Discovery and Booking Manager both handed their own Neon
        connection to get_user_tier(), which then ran
        SELECT ... FROM subscriptions against a database that has no such
        table, and every caller got
        asyncpg.exceptions.UndefinedTableError and a 500.

    Booking Manager worked around it by reading a local tier column, which is
    the same subscription state written twice in two stores with only one of
    them maintained. That is the drift this platform has already paid for
    four times over.

    So this module now offers the same rule over two transports:

        get_user_tier(user_id, db)          asyncpg, for services whose
                                            connection IS Supabase
        get_user_tier_via_rest(user_id,     PostgREST with a service role
            supabase_url, service_role_key) key, for services whose database
                                            is somewhere else

    **The rule itself is not duplicated.** Both paths derive the tier from the
    same _PRO_GRANTING_STATUSES set, build the same SubscriptionState, and
    share the same cache, so a service cannot disagree with another service
    about what "pro" means depending on how it happened to read the row. Only
    the transport differs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import asyncpg


class Tier(str, Enum):
    FREE = "free"
    PRO = "pro"
    MERCHANT = "merchant"


class Module(str, Enum):
    """
    The three modules Merchant is sold against, priced separately from one
    another (Storefront and Booking Manager both 4.99/month, Map Discovery
    3.99/month, Henry 2026-08-13). Matches the module CHECK constraint on
    merchant_subscriptions and the source_service values already written
    onto map_listings by Storefront and Booking Manager.
    """

    STOREFRONT = "storefront"
    BOOKING_MANAGER = "booking_manager"
    MAP_DISCOVERY = "map_discovery"


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
    Tiny in-process TTL cache keyed by an arbitrary string. Not shared across
    processes or instances, each service worker holds its own. That is fine,
    the cost of a stale read for up to 60 seconds is negligible against the
    cost of querying Postgres on every request.

    Generic over what it stores: the platform cache below keys by bare
    user_id and holds a SubscriptionState, the module cache keys by
    "user_id:module" and holds a bare Tier. Same shape, same TTL discipline,
    deliberately not two hand-rolled copies of the same twelve lines.
    """

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


_cache = _EntitlementCache()
# Keyed by "user_id:module", holds a bool: whether an active
# merchant_subscriptions row holds that module. A bool rather than a Tier
# since v1.3.0, because the module row no longer decides the final answer
# alone: a non-holding row falls through to the platform tier, which has its
# own cache and its own freshness, and caching the composed Tier here would
# let a stale composition outlive either ingredient.
_module_cache = _EntitlementCache()


def _module_cache_key(user_id: str, module: "Module") -> str:
    return f"{user_id}:{module.value}"


class EntitlementSourceUnavailable(Exception):
    """
    The subscriptions table could not be read at all.

    Deliberately distinct from "this user has no subscription", which is a
    real answer and returns FREE. This means the question was never answered:
    the network failed, the key was rejected, or the service is misconfigured.

    It is raised rather than swallowed because the two cases want opposite
    handling and only the caller knows which it wants. A service that
    under-grants on ambiguity should catch this and log loudly, so the
    misconfiguration is visible rather than looking like a member who has not
    paid. A service that would rather fail than silently downgrade a paying
    member should let it propagate. Returning FREE from in here would take
    that choice away and make every misconfiguration indistinguishable from a
    free account.
    """


def _state_from_row(row: Optional[Any]) -> SubscriptionState:
    """
    Turns one subscriptions row, or its absence, into a SubscriptionState.

    **The single definition of what a tier is.** Both transports funnel
    through here so that a REST-reading service and an asyncpg-reading
    service cannot come to different conclusions about the same row.

    Accepts anything indexable by column name, which covers both an
    asyncpg.Record and the plain dict PostgREST returns.
    """
    if row is None:
        # No row covers every member who has never started a checkout, which
        # today is most of them.
        return SubscriptionState(tier=Tier.FREE, status="none", current_period_end=None)

    status = row["status"]

    # Status is the real source of truth, not the stored tier column.
    # The tier column is a convenience snapshot, but if status has
    # drifted (e.g. a cancellation webhook landed but a stale read beat
    # it), deriving from status here is the safety net.
    effective_tier = Tier.PRO if status in _PRO_GRANTING_STATUSES else Tier.FREE

    period_end = row["current_period_end"]
    # asyncpg hands back a datetime, PostgREST hands back an ISO string that
    # is already in the shape this dataclass wants.
    if period_end is not None and not isinstance(period_end, str):
        period_end = period_end.isoformat()

    return SubscriptionState(
        tier=effective_tier,
        status=status,
        current_period_end=period_end,
    )


async def get_subscription_state(
    user_id: str,
    db: asyncpg.Connection,
) -> SubscriptionState:
    """
    Returns the current subscription state for a user, read over asyncpg.
    Defaults to free if no row exists.

    **The connection must be the Supabase database.** Handing this a
    service's own Neon connection raises UndefinedTableError, which is not a
    hypothetical: it is how this function behaved for every caller in Map
    Discovery and Booking Manager. Services whose database is not Supabase
    want get_subscription_state_via_rest instead.
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

    state = _state_from_row(row)
    _cache.set(user_id, state)
    return state


async def get_subscription_state_via_rest(
    user_id: str,
    supabase_url: str,
    service_role_key: str,
    timeout_seconds: float = 5.0,
) -> SubscriptionState:
    """
    Returns the current subscription state for a user, read over PostgREST.

    For services that authenticate Supabase JWTs but keep their own data
    somewhere else, which is most of them: Map Discovery, Booking Manager,
    Looks, Messaging and Signal all hold a Supabase project URL and a Neon
    database. They cannot run the query above and this is what they call
    instead.

    Requires the service role key, because subscriptions is not readable by
    anon and RLS on it is written for the owning member rather than for a
    service asking about somebody else.

    Raises EntitlementSourceUnavailable when the read fails, and caches
    nothing in that case. A missing row is not a failure: it is a real answer
    and returns FREE, cached like any other.

    The timeout is short on purpose. This runs inside the transaction of the
    write it is gating, so a hanging entitlement read holds a database
    transaction open, which is worse than under-granting for one request.
    """
    cached = _cache.get(user_id)
    if cached is not None:
        return cached

    # Imported here rather than at module scope so that services using only
    # the asyncpg transport do not have to carry httpx.
    import httpx

    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/subscriptions"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                endpoint,
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "tier,status,current_period_end",
                    "limit": "1",
                },
                headers={
                    "apikey": service_role_key,
                    "Authorization": f"Bearer {service_role_key}",
                    "Accept": "application/json",
                },
            )
    except Exception as exc:  # noqa: BLE001
        raise EntitlementSourceUnavailable(
            f"Could not reach the subscriptions table at {endpoint}: {exc}"
        ) from exc

    if response.status_code != 200:
        # The body is included because the two failures that actually happen
        # here say which is which: an expired or wrong key answers 401 with a
        # message, and a missing table answers 404 with the relation name.
        raise EntitlementSourceUnavailable(
            f"subscriptions read returned {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        rows = response.json()
    except ValueError as exc:
        raise EntitlementSourceUnavailable(
            f"subscriptions read returned a 200 that was not JSON: "
            f"{response.text[:200]}"
        ) from exc

    state = _state_from_row(rows[0] if rows else None)
    _cache.set(user_id, state)
    return state


async def get_user_tier(user_id: str, db: asyncpg.Connection) -> Tier:
    """Convenience wrapper when only the tier is needed, not full state."""
    state = await get_subscription_state(user_id, db)
    return state.tier


async def get_user_tier_via_rest(
    user_id: str,
    supabase_url: str,
    service_role_key: str,
    timeout_seconds: float = 5.0,
) -> Tier:
    """Convenience wrapper over get_subscription_state_via_rest."""
    state = await get_subscription_state_via_rest(
        user_id, supabase_url, service_role_key, timeout_seconds
    )
    return state.tier


def _merchant_row_grants_tier(row: Optional[Any]) -> bool:
    """
    True when a merchant_subscriptions row represents an active hold on that
    module. Shares _PRO_GRANTING_STATUSES with the platform tier rather than
    a second set: a failed card gets the same one-cycle grace period on a
    Merchant module subscription as it does on Pro itself, and there is no
    stated reason for those two grace periods to disagree.
    """
    return row is not None and row["status"] in _PRO_GRANTING_STATUSES


async def get_module_tier(
    user_id: str,
    module: Module,
    db: asyncpg.Connection,
) -> Tier:
    """
    Returns the member's effective tier for ONE module: FREE, PRO, or
    MERCHANT.

    Composes two independent facts rather than reading one column, because
    Merchant is held per module (merchant_subscriptions) while Pro is
    platform-wide (subscriptions). A member can be Pro on the platform and
    Merchant on Storefront alone, still capped at Pro's allowance on Booking
    Manager and Map Discovery; get_user_tier() alone cannot express that,
    since it takes no module and answers the same platform-wide tier
    regardless of which module is asking.

    **Paid time stays usable. Henry, 2026-08-13.** An active
    merchant_subscriptions row grants MERCHANT on its module even when the
    platform subscription has lapsed: money already taken must never buy
    nothing, so a member who cancels Pro halfway through a paid Merchant
    year keeps that one module working until the period they paid for runs
    out. This deliberately replaces v1.2.0's rule, which read a lapsed
    platform subscription as FREE against any merchant row. The ladder is
    enforced where money changes hands instead: buying Merchant requires
    Pro at the checkout endpoint, and the Stripe webhook stops Merchant
    renewals the moment Pro is cancelled, so Merchant-without-Pro is only
    ever a paid period running out, never a state anybody buys into or
    renews inside. The truthfulness of the row's status is therefore the
    webhook's responsibility; this read believes it.

    **Requires the caller's own connection to be Supabase.** Same
    requirement as get_subscription_state: a service whose database is
    somewhere else wants get_module_tier_via_rest instead.
    """
    cache_key = _module_cache_key(user_id, module)
    merchant_hold = _module_cache.get(cache_key)

    if merchant_hold is None:
        row = await db.fetchrow(
            """
            SELECT status FROM merchant_subscriptions
            WHERE user_id = $1 AND module = $2
            """,
            user_id,
            module.value,
        )
        merchant_hold = _merchant_row_grants_tier(row)
        _module_cache.set(cache_key, merchant_hold)

    if merchant_hold:
        return Tier.MERCHANT

    return await get_user_tier(user_id, db)


async def get_module_tier_via_rest(
    user_id: str,
    module: Module,
    supabase_url: str,
    service_role_key: str,
    timeout_seconds: float = 5.0,
) -> Tier:
    """
    Returns the member's effective tier for ONE module, read over PostgREST.

    For services in the same position get_subscription_state_via_rest
    already serves: Map Discovery, Booking Manager, Looks, Messaging and
    Signal all hold a Supabase project URL and a database that is not
    Supabase. Same paid-time-stays-usable rule as get_module_tier above:
    the module row is asked first, and an active hold answers MERCHANT
    without the platform read at all, since is_pro() admits MERCHANT
    through every at-least-Pro gate. The platform tier is only fetched
    when the module row does not hold.
    """
    cache_key = _module_cache_key(user_id, module)
    merchant_hold = _module_cache.get(cache_key)
    if merchant_hold is not None:
        if merchant_hold:
            return Tier.MERCHANT
        return await get_user_tier_via_rest(
            user_id, supabase_url, service_role_key, timeout_seconds
        )

    import httpx

    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/merchant_subscriptions"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                endpoint,
                params={
                    "user_id": f"eq.{user_id}",
                    "module": f"eq.{module.value}",
                    "select": "status",
                    "limit": "1",
                },
                headers={
                    "apikey": service_role_key,
                    "Authorization": f"Bearer {service_role_key}",
                    "Accept": "application/json",
                },
            )
    except Exception as exc:  # noqa: BLE001
        raise EntitlementSourceUnavailable(
            f"Could not reach the merchant_subscriptions table at {endpoint}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise EntitlementSourceUnavailable(
            f"merchant_subscriptions read returned {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        rows = response.json()
    except ValueError as exc:
        raise EntitlementSourceUnavailable(
            f"merchant_subscriptions read returned a 200 that was not JSON: "
            f"{response.text[:200]}"
        ) from exc

    merchant_hold = _merchant_row_grants_tier(rows[0] if rows else None)
    _module_cache.set(cache_key, merchant_hold)

    if merchant_hold:
        return Tier.MERCHANT

    return await get_user_tier_via_rest(
        user_id, supabase_url, service_role_key, timeout_seconds
    )


def is_pro(tier: Tier) -> bool:
    """
    True for PRO and MERCHANT both. Merchant is priced as the step above
    Pro's allowance, not a separate track, so anything gated on "is this
    member at least Pro" must also admit a Merchant member. Comparing
    against Tier.PRO alone would silently lock a Merchant member, who is
    paying more than a Pro one, out of whatever that gate protects.
    """
    return tier in (Tier.PRO, Tier.MERCHANT)


def is_merchant(tier: Tier) -> bool:
    return tier == Tier.MERCHANT


def invalidate_cache(user_id: str) -> None:
    """
    Call this after any flow that might have just changed a user's tier,
    e.g. immediately after a successful Checkout redirect, so the next
    request doesn't wait out the cache TTL on stale Free state.

    Also clears every module's cached tier for this user. A platform tier
    change can change what get_module_tier answers even when no
    merchant_subscriptions row moved at all (a lapsed Pro member drops
    every module to FREE regardless of what they hold), so a caller who
    only knows "this user's subscription changed" does not have to also
    know which modules to individually invalidate.
    """
    _cache.invalidate(user_id)
    for module in Module:
        _module_cache.invalidate(_module_cache_key(user_id, module))
