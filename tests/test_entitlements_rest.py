"""
Tests for the PostgREST transport.

These pin the two things that actually go wrong. First, that a failed read
is distinguishable from a member who has not paid: those look identical from
the outside and confusing them is how a misconfigured service silently
downgrades every paying member. Second, that both transports agree, because
the whole reason this transport was added rather than reimplemented in one
service was to stop "pro" meaning two things depending on how the row was
fetched.

Run: pytest tests/test_entitlements_rest.py
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from fshnista_entitlements.entitlements import (
    EntitlementSourceUnavailable,
    SubscriptionState,
    Tier,
    _cache,
    _state_from_row,
    get_subscription_state,
    get_subscription_state_via_rest,
    get_user_tier_via_rest,
)

SUPABASE_URL = "https://lesctuyamoutchwotgbw.supabase.co"
SERVICE_KEY = "test-service-role-key"
USER = "186992b3-a8c0-465e-80f2-363bfc843ebd"


@pytest.fixture(autouse=True)
def clear_cache():
    """
    The cache is module level and 60 seconds long, so without this a test
    would read the previous test's answer and pass for the wrong reason.
    """
    _cache._store.clear()
    yield
    _cache._store.clear()


class _FakeAsyncClient:
    """
    Stands in for httpx.AsyncClient. Records the single request made so the
    tests can assert on the headers, which is where the service role key has
    to appear twice and in two different forms.
    """

    last_request: dict | None = None

    def __init__(self, response=None, raises=None, **kwargs):
        self._response = response
        self._raises = raises
        self.init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        type(self).last_request = {
            "url": url,
            "params": params,
            "headers": headers,
        }
        if self._raises is not None:
            raise self._raises
        return self._response


def _patch_client(monkeypatch, *, response=None, raises=None):
    def factory(**kwargs):
        return _FakeAsyncClient(response=response, raises=raises, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _ok(payload):
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_subscription_is_pro(monkeypatch):
    _patch_client(
        monkeypatch,
        response=_ok(
            [
                {
                    "tier": "pro",
                    "status": "active",
                    "current_period_end": "2036-08-04T16:29:40.676348+00:00",
                }
            ]
        ),
    )

    state = await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)

    assert state.tier is Tier.PRO
    assert state.status == "active"
    # PostgREST already hands back an ISO string. It must survive untouched
    # rather than being reformatted into a second representation.
    assert state.current_period_end == "2036-08-04T16:29:40.676348+00:00"


@pytest.mark.asyncio
async def test_no_row_is_free_and_is_not_an_error(monkeypatch):
    """An empty array is a real answer, not a failure. Most members have one."""
    _patch_client(monkeypatch, response=_ok([]))

    state = await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)

    assert state == SubscriptionState(
        tier=Tier.FREE, status="none", current_period_end=None
    )


@pytest.mark.asyncio
async def test_past_due_still_grants_pro(monkeypatch):
    """A failed card gets a grace period. Same set as the Stripe webhook."""
    _patch_client(
        monkeypatch,
        response=_ok([{"tier": "pro", "status": "past_due", "current_period_end": None}]),
    )

    assert await get_user_tier_via_rest(USER, SUPABASE_URL, SERVICE_KEY) is Tier.PRO


@pytest.mark.asyncio
async def test_status_beats_the_tier_column(monkeypatch):
    """
    A cancelled row can still carry tier='pro' if the webhook wrote status
    first, or if a stale read beat it. Status is the source of truth.
    """
    _patch_client(
        monkeypatch,
        response=_ok([{"tier": "pro", "status": "canceled", "current_period_end": None}]),
    )

    assert await get_user_tier_via_rest(USER, SUPABASE_URL, SERVICE_KEY) is Tier.FREE


@pytest.mark.asyncio
async def test_the_key_is_sent_as_both_apikey_and_bearer(monkeypatch):
    """
    PostgREST needs apikey to route the request and Authorization to
    authorise it. Sending only one answers 401, which reads as a bad key.
    """
    _patch_client(monkeypatch, response=_ok([]))

    await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)

    sent = _FakeAsyncClient.last_request
    assert sent["headers"]["apikey"] == SERVICE_KEY
    assert sent["headers"]["Authorization"] == f"Bearer {SERVICE_KEY}"
    assert sent["params"]["user_id"] == f"eq.{USER}"
    assert sent["url"] == f"{SUPABASE_URL}/rest/v1/subscriptions"


@pytest.mark.asyncio
async def test_a_trailing_slash_on_the_url_does_not_double_up(monkeypatch):
    """
    The project URL arrives from an environment variable, so it may or may
    not carry a trailing slash. //rest/v1 is a 404 and would present as a
    missing table.
    """
    _patch_client(monkeypatch, response=_ok([]))

    await get_subscription_state_via_rest(USER, SUPABASE_URL + "/", SERVICE_KEY)

    assert _FakeAsyncClient.last_request["url"] == f"{SUPABASE_URL}/rest/v1/subscriptions"


# ---------------------------------------------------------------------------
# The failures, which must not look like a free account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_key_raises_rather_than_returning_free(monkeypatch):
    _patch_client(monkeypatch, response=httpx.Response(401, text="Invalid API key"))

    with pytest.raises(EntitlementSourceUnavailable) as caught:
        await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)

    # The body carries which failure it was, and that is the whole reason to
    # include it: 401 and 404 want completely different fixes.
    assert "401" in str(caught.value)
    assert "Invalid API key" in str(caught.value)


@pytest.mark.asyncio
async def test_a_network_failure_raises(monkeypatch):
    _patch_client(monkeypatch, raises=httpx.ConnectError("name resolution failed"))

    with pytest.raises(EntitlementSourceUnavailable):
        await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)


@pytest.mark.asyncio
async def test_a_200_that_is_not_json_raises(monkeypatch):
    """A proxy or an error page can answer 200 with HTML."""
    _patch_client(monkeypatch, response=httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(EntitlementSourceUnavailable):
        await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)


@pytest.mark.asyncio
async def test_a_failed_read_caches_nothing(monkeypatch):
    """
    Caching a failure would turn one bad minute into a minute of every
    member looking free, and it would do it silently.
    """
    _patch_client(monkeypatch, response=httpx.Response(500, text="boom"))

    with pytest.raises(EntitlementSourceUnavailable):
        await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)

    assert _cache.get(USER) is None


# ---------------------------------------------------------------------------
# The two transports must not disagree
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, row):
        self._row = row
        self.calls = 0

    async def fetchrow(self, *args):
        self.calls += 1
        return self._row


@pytest.mark.asyncio
async def test_both_transports_read_the_same_row_the_same_way():
    """
    The asyncpg path gets a datetime and the REST path gets an ISO string.
    Everything else about the answer has to match, or "pro" means two
    things depending on how the row was fetched, which is the exact fault
    this transport was added to avoid.
    """
    ends = datetime.datetime(2036, 8, 4, 16, 29, 40, 676348, tzinfo=datetime.timezone.utc)

    from_pg = _state_from_row(
        {"tier": "pro", "status": "active", "current_period_end": ends}
    )
    from_rest = _state_from_row(
        {"tier": "pro", "status": "active", "current_period_end": ends.isoformat()}
    )

    assert from_pg == from_rest


@pytest.mark.asyncio
async def test_the_cache_is_shared_across_transports(monkeypatch):
    """
    One cache, so a service that reads over REST and then over asyncpg, or
    a test that mixes them, cannot see two different answers a millisecond
    apart.
    """
    _patch_client(
        monkeypatch,
        response=_ok([{"tier": "pro", "status": "active", "current_period_end": None}]),
    )
    await get_subscription_state_via_rest(USER, SUPABASE_URL, SERVICE_KEY)

    connection = _FakeConnection(row=None)  # would answer FREE if it were consulted
    state = await get_subscription_state(USER, connection)

    assert state.tier is Tier.PRO
    assert connection.calls == 0
