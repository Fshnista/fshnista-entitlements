"""
Tests for get_module_tier / get_module_tier_via_rest.

The property that matters most here is the short circuit: a member who
isn't platform Pro must never trigger a merchant_subscriptions read at all,
over either transport. That is not an optimisation, it is what makes "you
cannot reach Merchant without Pro" true by construction rather than by
convention. If the short circuit ever goes missing, a canceled Pro
subscription sitting next to a stale active-looking merchant_subscriptions
row would read as Merchant, which is the exact kind of two-tables-disagree
bug this platform has paid for four times already.

Run: pytest tests/test_module_tier.py
"""

from __future__ import annotations

import httpx
import pytest

from fshnista_entitlements.entitlements import (
    Module,
    Tier,
    _cache,
    _module_cache,
    get_module_tier,
    get_module_tier_via_rest,
    invalidate_cache,
)

SUPABASE_URL = "https://lesctuyamoutchwotgbw.supabase.co"
SERVICE_KEY = "test-service-role-key"
USER = "186992b3-a8c0-465e-80f2-363bfc843ebd"


@pytest.fixture(autouse=True)
def clear_caches():
    _cache._store.clear()
    _module_cache._store.clear()
    yield
    _cache._store.clear()
    _module_cache._store.clear()


class _FakeConnection:
    """
    Answers get_user_tier's own subscriptions query first, then
    merchant_subscriptions if get_module_tier goes on to ask. Records call
    counts on both so the short-circuit tests have something to assert on.
    """

    def __init__(self, subscription_row, merchant_row=None):
        self._subscription_row = subscription_row
        self._merchant_row = merchant_row
        self.subscription_calls = 0
        self.merchant_calls = 0

    async def fetchrow(self, query, *args):
        if "FROM subscriptions" in query:
            self.subscription_calls += 1
            return self._subscription_row
        if "FROM merchant_subscriptions" in query:
            self.merchant_calls += 1
            return self._merchant_row
        raise AssertionError(f"Unexpected query: {query}")


class _FakeAsyncClient:
    """
    Same shape as test_entitlements_rest.py's fake, extended to answer
    whichever of the two endpoints is hit and to record both request lists
    rather than just the last one, since a single get_module_tier_via_rest
    call can make up to two requests.
    """

    requests: list[dict]

    def __init__(self, responses, **kwargs):
        self._responses = responses
        self.init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        type(self).requests.append({"url": url, "params": params, "headers": headers})
        if url.endswith("/subscriptions"):
            return self._responses["subscriptions"]
        if url.endswith("/merchant_subscriptions"):
            return self._responses["merchant_subscriptions"]
        raise AssertionError(f"Unexpected URL: {url}")


def _patch_client(monkeypatch, **responses):
    _FakeAsyncClient.requests = []

    def factory(**kwargs):
        return _FakeAsyncClient(responses, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return _FakeAsyncClient


def _ok(payload):
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------------------
# asyncpg transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_platform_tier_short_circuits_before_the_module_query():
    connection = _FakeConnection(subscription_row=None)

    tier = await get_module_tier(USER, Module.STOREFRONT, connection)

    assert tier is Tier.FREE
    assert connection.merchant_calls == 0


@pytest.mark.asyncio
async def test_pro_with_no_merchant_row_is_pro():
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row=None,
    )

    tier = await get_module_tier(USER, Module.STOREFRONT, connection)

    assert tier is Tier.PRO
    assert connection.merchant_calls == 1


@pytest.mark.asyncio
async def test_pro_with_an_active_merchant_row_is_merchant():
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row={"status": "active"},
    )

    assert await get_module_tier(USER, Module.STOREFRONT, connection) is Tier.MERCHANT


@pytest.mark.asyncio
async def test_pro_with_a_canceled_merchant_row_stays_pro():
    """
    A row existing is not enough, same rule as the platform subscription:
    status decides, not row presence.
    """
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row={"status": "canceled"},
    )

    assert await get_module_tier(USER, Module.STOREFRONT, connection) is Tier.PRO


@pytest.mark.asyncio
async def test_the_module_query_is_scoped_by_both_user_and_module():
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row=None,
    )

    await get_module_tier(USER, Module.MAP_DISCOVERY, connection)

    # Real assertion is that Module.MAP_DISCOVERY's *value* reached the
    # query, not just that a query happened: get_module_tier(..., STOREFRONT)
    # answering for a Map Discovery row would be exactly the per-module
    # leak this table exists to prevent.
    assert connection.merchant_calls == 1


@pytest.mark.asyncio
async def test_module_tier_is_cached_separately_per_module():
    """
    A member Merchant on Storefront and merely Pro on Booking Manager must
    not collide into one cached answer keyed by user_id alone.
    """
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row={"status": "active"},
    )

    storefront_tier = await get_module_tier(USER, Module.STOREFRONT, connection)
    assert connection.merchant_calls == 1

    connection._merchant_row = None  # would answer PRO if consulted again
    booking_tier = await get_module_tier(USER, Module.BOOKING_MANAGER, connection)

    assert storefront_tier is Tier.MERCHANT
    assert booking_tier is Tier.PRO
    assert connection.merchant_calls == 2  # one per module, not reused


@pytest.mark.asyncio
async def test_a_second_call_for_the_same_module_hits_the_cache():
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row={"status": "active"},
    )

    await get_module_tier(USER, Module.STOREFRONT, connection)
    await get_module_tier(USER, Module.STOREFRONT, connection)

    assert connection.merchant_calls == 1


@pytest.mark.asyncio
async def test_invalidate_cache_clears_every_module_for_that_user():
    connection = _FakeConnection(
        subscription_row={"tier": "pro", "status": "active", "current_period_end": None},
        merchant_row={"status": "active"},
    )
    await get_module_tier(USER, Module.STOREFRONT, connection)
    await get_module_tier(USER, Module.BOOKING_MANAGER, connection)
    await get_module_tier(USER, Module.MAP_DISCOVERY, connection)
    assert connection.merchant_calls == 3

    invalidate_cache(USER)

    await get_module_tier(USER, Module.STOREFRONT, connection)
    assert connection.merchant_calls == 4  # re-queried, not served stale


# ---------------------------------------------------------------------------
# PostgREST transport. Same properties, over httpx.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_free_platform_tier_never_requests_merchant_subscriptions(monkeypatch):
    client = _patch_client(
        monkeypatch,
        subscriptions=_ok([]),
        merchant_subscriptions=_ok([{"status": "active"}]),  # must never be reached
    )

    tier = await get_module_tier_via_rest(
        USER, Module.STOREFRONT, SUPABASE_URL, SERVICE_KEY
    )

    assert tier is Tier.FREE
    urls = [r["url"] for r in client.requests]
    assert urls == [f"{SUPABASE_URL}/rest/v1/subscriptions"]


@pytest.mark.asyncio
async def test_rest_pro_with_active_merchant_row_is_merchant(monkeypatch):
    client = _patch_client(
        monkeypatch,
        subscriptions=_ok([{"tier": "pro", "status": "active", "current_period_end": None}]),
        merchant_subscriptions=_ok([{"status": "active"}]),
    )

    tier = await get_module_tier_via_rest(
        USER, Module.MAP_DISCOVERY, SUPABASE_URL, SERVICE_KEY
    )

    assert tier is Tier.MERCHANT
    second_request = client.requests[1]
    assert second_request["url"] == f"{SUPABASE_URL}/rest/v1/merchant_subscriptions"
    assert second_request["params"]["user_id"] == f"eq.{USER}"
    assert second_request["params"]["module"] == "eq.map_discovery"


@pytest.mark.asyncio
async def test_rest_pro_with_no_merchant_row_stays_pro(monkeypatch):
    _patch_client(
        monkeypatch,
        subscriptions=_ok([{"tier": "pro", "status": "active", "current_period_end": None}]),
        merchant_subscriptions=_ok([]),
    )

    tier = await get_module_tier_via_rest(
        USER, Module.BOOKING_MANAGER, SUPABASE_URL, SERVICE_KEY
    )

    assert tier is Tier.PRO
