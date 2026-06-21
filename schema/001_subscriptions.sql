-- =============================================================================
-- FSHNiSTA Platform Entitlements
-- Migration 001: Subscriptions
--
-- Purpose
--   Single source of truth for a user's platform-wide subscription tier.
--   Every microservice (Booking Manager, Storefront, Twin Studio, etc.)
--   reads this table to decide what a user can access. No service writes
--   to it directly except the Stripe webhook handler.
--
-- Design notes
--   One row per user. Tier is platform-wide, not module-specific. Modules
--   decide independently what "free" and "pro" mean for them, but they all
--   read the same tier value from here.
--
--   This table lives in Supabase Postgres, the same database every service
--   already authenticates against. No standalone entitlement service.
-- =============================================================================

CREATE TYPE subscription_tier AS ENUM ('free', 'pro');

CREATE TYPE subscription_status AS ENUM (
    'active',           -- paid and current
    'trialing',         -- in trial period, treated as pro
    'past_due',         -- payment failed, grace period, still treated as pro
    'canceled',         -- user canceled, reverts to free at period end
    'incomplete'        -- checkout started but never completed, treated as free
);

CREATE TABLE subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,

    tier                    subscription_tier NOT NULL DEFAULT 'free',
    status                  subscription_status NOT NULL DEFAULT 'incomplete',

    stripe_customer_id      VARCHAR(255) UNIQUE,
    stripe_subscription_id  VARCHAR(255) UNIQUE,
    stripe_price_id         VARCHAR(255),

    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT false,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every entitlement check in every module hits this index. user_id lookups
-- must be fast, this is the single hottest query path in the platform.
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);

-- Used by the webhook handler to find the row when Stripe sends an event
-- keyed by subscription ID rather than user ID.
CREATE INDEX idx_subscriptions_stripe_subscription_id
    ON subscriptions(stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- Stripe event audit log
--
-- Every webhook event Stripe sends gets logged here before it is processed.
-- This gives you replay safety and a debugging trail when a subscription
-- state looks wrong and you need to know exactly what Stripe told you, and
-- when, and whether you already processed it.
-- =============================================================================

CREATE TABLE stripe_webhook_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id     VARCHAR(255) NOT NULL UNIQUE,
    event_type          VARCHAR(100) NOT NULL,
    payload             JSONB NOT NULL,
    processed_at        TIMESTAMPTZ,
    error                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stripe retries unacknowledged webhooks. This index backs the idempotency
-- check that prevents double-processing the same event.
CREATE INDEX idx_stripe_webhook_events_event_id ON stripe_webhook_events(stripe_event_id);

COMMENT ON TABLE subscriptions IS
    'Platform-wide subscription tier per user. Source of truth for all entitlement checks across every microservice.';
COMMENT ON TABLE stripe_webhook_events IS
    'Audit log of every Stripe webhook event received, for idempotency and debugging.';
