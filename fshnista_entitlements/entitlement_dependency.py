"""
FSHNiSTA Platform Entitlements
FastAPI dependency

Purpose
    Wires the entitlements module into FastAPI route handlers as a
    dependency, matching the same dependency injection pattern already used
    for JWT auth in Booking Manager and Storefront (see auth.py in each).

Usage
    from fshnista_entitlements.entitlement_dependency import require_pro, get_tier

    @router.post("/storefront/products")
    async def create_product(
        user: User = Depends(get_current_user),
        _: None = Depends(require_pro),
    ):
        ...

    # Or, when a route needs the tier value itself rather than a hard block,
    # e.g. Booking Manager checking the tier alongside a usage count:

    @router.post("/bookings")
    async def create_booking(
        user: User = Depends(get_current_user),
        tier: Tier = Depends(get_tier),
        db: asyncpg.Connection = Depends(get_db),
    ):
        confirmed_count = await count_confirmed_bookings_this_month(user.id, db)
        decision = check_booking_creation_access(tier, confirmed_count)
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)
        ...
"""

from __future__ import annotations

import asyncpg
from fastapi import Depends, HTTPException

from .entitlements import Tier, get_user_tier, is_pro

# Each service supplies its own get_current_user and get_db dependencies,
# already built in Booking Manager's auth.py and db.py. Importing them here
# directly would create a circular dependency between shared/ and each
# service, so callers pass user_id and db in. See usage example above.


async def get_tier(user_id: str, db: asyncpg.Connection) -> Tier:
    """Resolve the caller's current subscription tier."""
    return await get_user_tier(user_id, db)


async def require_pro(user_id: str, db: asyncpg.Connection) -> None:
    """
    Hard gate. Raises 403 immediately if the user is not Pro. Use this for
    Storefront-style modules where there is no partial free access, only a
    wall.
    """
    tier = await get_user_tier(user_id, db)
    if not is_pro(tier):
        raise HTTPException(
            status_code=403,
            detail="This feature requires FSHNiSTA Pro.",
        )
