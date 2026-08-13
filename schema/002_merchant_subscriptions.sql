-- =============================================================================
-- FSHNiSTA Platform Entitlements
-- Migration 002: Merchant subscriptions
--
-- Purpose
--   Merchant is the paid step above Pro's included allowance on each
--   module, priced separately per module (Storefront and Booking Manager
--   both 4.99/month, Map Discovery 3.99/month with 20% off paid annually,
--   Henry 2026-08-13), and held per module: a member can be Merchant on
--   Storefront while still capped at Pro's allowance on Booking Manager and
--   Map Discovery.
--
-- Design notes
--   Deliberately NOT a third value on subscriptions.tier. That column is
--   one row per user_id, platform-wide, and read as a single Tier by every
--   caller of get_user_tier()/get_user_tier_via_rest(). Adding 'merchant'
--   there would make Merchant on any one module read as Merchant on all
--   three to every service, since neither function takes a module
--   argument. So this is a second table: subscriptions.tier stays
--   Pro-only, platform-wide, and this one carries the per-module fact on
--   top of it. get_module_tier()/get_module_tier_via_rest() compose both.
--
--   Mirrors subscriptions' own shape (same Stripe columns, same
--   status enum, same trigger) and its RLS pattern: RLS on, exactly one
--   SELECT policy for the owner, everything else through service_role.
--   Applied directly against the live project 2026-08-13 via the Supabase
--   migration tool and verified in all three directions before this file
--   was written back here: anon sees zero rows, the owner reads their own
--   row, and an authenticated INSERT is denied by RLS with no INSERT
--   policy existing for any role.
-- =============================================================================

CREATE TABLE merchant_subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    module                  TEXT NOT NULL CHECK (module IN ('storefront', 'booking_manager', 'map_discovery')),

    status                  subscription_status NOT NULL DEFAULT 'incomplete',

    stripe_customer_id      VARCHAR(255),
    stripe_subscription_id  VARCHAR(255),
    stripe_price_id         VARCHAR(255),

    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT false,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, module)
);

CREATE INDEX merchant_subscriptions_user_id_idx ON merchant_subscriptions (user_id);

ALTER TABLE merchant_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Members read their own merchant subscriptions"
    ON merchant_subscriptions
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);

-- Matches subscriptions: reachable at the raw grant level (Supabase's
-- default per-role privileges), reachable in practice only through RLS.
-- Writes happen exclusively via service_role from the Stripe webhook
-- handler; no INSERT/UPDATE/DELETE policy exists for anon or authenticated
-- on purpose, the same deny-by-default shape as wallet_locks.
REVOKE ALL ON merchant_subscriptions FROM PUBLIC;
GRANT SELECT ON merchant_subscriptions TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON merchant_subscriptions TO service_role;

COMMENT ON TABLE merchant_subscriptions IS
    'Per-module Merchant subscription, one row per (user, module). subscriptions.tier stays the platform-wide Pro/Free source of truth; this table is what get_module_tier composes on top of it.';
