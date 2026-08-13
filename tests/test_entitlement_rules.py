"""
Tests for entitlement_rules.py.

This file was empty until 2026-08-13, despite the module it covers gating
real billing decisions across three services. Written alongside the
Merchant tier work: the correction to Booking Manager's free-tier gate (it
previously let free tier confirm 5 bookings a month with no Pro required at
all) is exactly the kind of behavior a test would have caught changing
again by accident, which is the whole argument for these existing at all.

Run: pytest tests/test_entitlement_rules.py
"""

from __future__ import annotations

import pytest

from fshnista_entitlements.entitlements import Tier, is_merchant, is_pro
from fshnista_entitlements.entitlement_rules import (
    BOOKING_MANAGER_PRO_MONTHLY_CAP,
    STOREFRONT_PRO_LISTING_CAP,
    check_booking_creation_access,
    check_booking_setup_access,
    check_storefront_access,
    check_storefront_listing_cap,
)


# ---------------------------------------------------------------------------
# is_pro / is_merchant. Getting these wrong silently breaks every rule below.
# ---------------------------------------------------------------------------


def test_is_pro_admits_merchant():
    """
    Merchant is priced as the step above Pro's allowance, not a separate
    track. A Merchant member failing an is_pro() gate would be a paying
    member locked out of something their own Pro peers get, in the opposite
    direction from the mistake CLAUDE.md already locks against.
    """
    assert is_pro(Tier.PRO)
    assert is_pro(Tier.MERCHANT)
    assert not is_pro(Tier.FREE)


def test_is_merchant_is_exclusive():
    assert is_merchant(Tier.MERCHANT)
    assert not is_merchant(Tier.PRO)
    assert not is_merchant(Tier.FREE)


# ---------------------------------------------------------------------------
# Storefront: zero free access, then a listing cap Merchant lifts.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [Tier.PRO, Tier.MERCHANT])
def test_storefront_access_allowed_at_pro_and_above(tier):
    assert check_storefront_access(tier).allowed


def test_storefront_access_denied_at_free():
    decision = check_storefront_access(Tier.FREE)
    assert not decision.allowed
    assert "Pro" in decision.reason


def test_storefront_listing_cap_allows_under_the_cap_on_pro():
    decision = check_storefront_listing_cap(Tier.PRO, STOREFRONT_PRO_LISTING_CAP - 1)
    assert decision.allowed


def test_storefront_listing_cap_denies_at_the_cap_on_pro():
    decision = check_storefront_listing_cap(Tier.PRO, STOREFRONT_PRO_LISTING_CAP)
    assert not decision.allowed
    # Never "upgrade to Pro" to a member who is already Pro. CLAUDE.md locks
    # this, and it has already been caught twice under this exact phrasing.
    assert "upgrade to pro" not in decision.reason.lower()
    assert "Merchant" in decision.reason


def test_storefront_listing_cap_is_unlimited_on_merchant():
    decision = check_storefront_listing_cap(Tier.MERCHANT, STOREFRONT_PRO_LISTING_CAP * 50)
    assert decision.allowed


# ---------------------------------------------------------------------------
# Booking Manager: the corrected model. Zero free access, Pro carries the
# five-a-month allowance, Merchant removes it.
# ---------------------------------------------------------------------------


def test_booking_creation_denied_at_free_regardless_of_count():
    """
    The behavior this file's docstring calls out as corrected 2026-08-13:
    free tier used to get its own 5-a-month allowance with no Pro required.
    A count of 0 must still be denied at FREE.
    """
    decision = check_booking_creation_access(Tier.FREE, confirmed_bookings_this_month=0)
    assert not decision.allowed
    assert "Pro" in decision.reason


def test_booking_creation_allowed_under_the_cap_on_pro():
    decision = check_booking_creation_access(
        Tier.PRO, confirmed_bookings_this_month=BOOKING_MANAGER_PRO_MONTHLY_CAP - 1
    )
    assert decision.allowed


def test_booking_creation_denied_at_the_cap_on_pro():
    decision = check_booking_creation_access(
        Tier.PRO, confirmed_bookings_this_month=BOOKING_MANAGER_PRO_MONTHLY_CAP
    )
    assert not decision.allowed
    assert "upgrade to pro" not in decision.reason.lower()
    assert "Merchant" in decision.reason


def test_booking_creation_unlimited_on_merchant():
    decision = check_booking_creation_access(
        Tier.MERCHANT, confirmed_bookings_this_month=BOOKING_MANAGER_PRO_MONTHLY_CAP * 10
    )
    assert decision.allowed


@pytest.mark.parametrize("tier", [Tier.PRO, Tier.MERCHANT])
def test_booking_setup_needs_only_pro(tier):
    """
    Service listing, calendar setup and accepting deposits sit behind the
    Pro door and nothing further above it, for Pro and Merchant alike.
    """
    assert check_booking_setup_access(tier).allowed


def test_booking_setup_denied_at_free():
    decision = check_booking_setup_access(Tier.FREE)
    assert not decision.allowed
