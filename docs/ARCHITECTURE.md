# FSHNiSTA Platform Entitlements

Technical documentation. Last updated 2026-06-22.

## What this is

The platform-wide subscription system every microservice has been stubbing.
Booking Manager and Storefront were both built with hardcoded entitlement
stubs because this system did not exist yet. This is that system, real.

One subscription, one Stripe product, one `tier` column per user. What that
tier permits is defined independently by each module.

## Architecture

No standalone service. Entitlement state lives directly in Supabase
Postgres, the same database every microservice already authenticates JWTs
against. The only new infrastructure is two Vercel serverless functions and
one new table.

```
Stripe Checkout
      |
      v
customer.subscription.* webhook events
      |
      v
Vercel function: fshnista-website/api/entitlements/stripe-webhook.js
      |
      v
Supabase: subscriptions table  <-------- read by every microservice
      |
      +---- Booking Manager (FastAPI, imports fshnista_entitlements)
      +---- Storefront (FastAPI, imports fshnista_entitlements)
      +---- Twin Studio (future)
```

Why not a standalone service. Entitlement checks happen on nearly every
authenticated request across every module. That is a read-heavy lookup
against one table, not a workload that justifies its own deployment
pipeline, its own database, and a new network hop that becomes a new
failure mode. A standalone service would mean Booking Manager going down
if Entitlement Service goes down, even though nothing about booking logic
actually changed. Reading from shared infrastructure each service already
trusts removes that failure mode entirely.

## Data model

### `subscriptions`

One row per user. See `schema/001_subscriptions.sql` for the full
migration with comments.

| Column | Purpose |
|---|---|
| `tier` | `free` or `pro`. Platform-wide, not module-specific. |
| `status` | Stripe-derived lifecycle state. Source of truth for whether `tier` should currently read as Pro. |
| `stripe_customer_id`, `stripe_subscription_id`, `stripe_price_id` | Stripe references for support and debugging. |
| `current_period_end` | When the current billing period ends. Used for UI display, not for gating logic. |
| `cancel_at_period_end` | True when a user canceled but retains access until period end. |

Status values and their tier effect:

| Status | Grants Pro? | Why |
|---|---|---|
| `active` | Yes | Paid and current. |
| `trialing` | Yes | In trial, full access. |
| `past_due` | Yes | Payment failed but inside grace period. A failed card should not instantly lock out a paying user. |
| `canceled` | No | Subscription ended. |
| `incomplete` | No | Checkout started but never finished. |

### `stripe_webhook_events`

Audit log of every webhook Stripe sends, written before processing begins.
Backs the idempotency check and gives a debugging trail when subscription
state looks wrong and you need to know what Stripe actually told you.

## Module-specific rules

Defined in `shared/entitlement_rules.py`, one function per module. This is
intentional duplication, each module's free-tier behavior is genuinely
different and burying that in a single generic function would hide the
actual product decision.

**Storefront.** Pro-only. Zero free access. `check_storefront_access`
returns a hard deny for any non-Pro tier, no exceptions, no partial access.

**Booking Manager.** Freemium. `check_booking_creation_access` allows free
tier providers up to 5 confirmed bookings per calendar month. Setting up
services, configuring availability, and accepting deposits are never gated,
only the act of confirming a 6th+ booking in a month is. Cancelled and
no-show bookings do not count against the cap.

Adding a new module's rules means adding a new function here, not touching
the shared tier resolution logic.

## Integration guide for a new module

1. Add this repo as a git dependency to the service's `requirements.txt`,
   pinned to a tag: `git+https://github.com/Fshnista/fshnista-entitlements.git@v1.0.0`
2. Add a rules function to this repo's `entitlement_rules.py` defining
   what free and pro mean for the new module, bump the version tag.
3. Update the pinned tag in the consuming service's `requirements.txt`.
4. Wire `get_tier` or `require_pro` into the relevant route as a FastAPI
   dependency, see docstring examples in `entitlement_dependency.py`.
5. If the module's free tier has a usage cap, build the count query (e.g.
   `count_confirmed_bookings_this_month` in Booking Manager) the same way
   Booking Manager does, then pass that count into the rules function.

This is a single shared repo consumed via pinned git install, not a
copy-pasted file per service. One source of truth for the rules logic,
one place to fix a bug, each service just updates its pinned tag when
ready to pick up a change.

## Stripe Checkout flow

`functions/create-pro-checkout.js` creates the Checkout Session. The
critical line is `subscription_data.metadata.fshnista_user_id`. Stripe
copies this metadata onto the actual Subscription object it creates, and
that is what the webhook handler reads to know which user to credit.
Without this, the webhook has no way to map a Stripe event back to a
FSHNiSTA user, and every upgrade silently fails to apply.

## Webhook handler

`functions/stripe-webhook.js`. Listens for:

- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_failed` (acknowledged only, no state write, the
  subscription's own `updated` event with `past_due` status handles the
  actual access change)

Idempotent by `stripe_event_id`. A retried webhook delivery checks
`stripe_webhook_events.processed_at` before doing any work, so Stripe's
automatic retries on a slow or failed response cannot double-apply state.

## Deployment checklist

**Supabase**
- [ ] Run `schema/001_subscriptions.sql` against the project database
- [ ] Confirm `auth.users` foreign key resolves correctly (RLS does not
      need to allow client-side reads of this table, all access goes
      through the service role key from backend services)

**Vercel (fshnista-website project)**
- [ ] Add `functions/stripe-webhook.js` as `api/entitlements/stripe-webhook.js`
- [ ] Add `functions/create-pro-checkout.js` as `api/entitlements/create-pro-checkout.js`
- [ ] Set environment variables: `STRIPE_WEBHOOK_SECRET`,
      `STRIPE_PRO_PRICE_ID`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
      `PUBLIC_SITE_URL`
- [ ] In Stripe Dashboard, add a webhook endpoint pointing to
      `https://www.fshnista.com/api/entitlements/stripe-webhook`, subscribed
      to the four event types listed above, copy the signing secret into
      `STRIPE_WEBHOOK_SECRET`

**Booking Manager and Storefront**
- [ ] Add `git+https://github.com/Fshnista/fshnista-entitlements.git@v1.0.0`
      to each service's `requirements.txt`
- [ ] Replace existing entitlement stubs with real calls to `get_tier` /
      `require_pro` imported from `fshnista_entitlements`
- [ ] Confirm each service's database connection can reach the
      `subscriptions` table (same Supabase project, should already be
      reachable)

## Testing

`tests/test_entitlement_rules.py` covers the rules layer in isolation, no
database required. Run with:

```
python -m pytest tests/test_entitlement_rules.py -v
```

11 tests, all passing. Covers Pro always-allow regardless of usage volume,
Free tier boundary conditions at, above, and below the booking cap, and
that denial reasons contain actionable upgrade copy rather than a bare
rejection.

Not yet covered, and worth building before this goes live: an integration
test against a real Supabase instance verifying the webhook handler
actually writes correct rows for each event type, and a test confirming
the in-process cache in `entitlements.py` does not serve stale Free state
for longer than its TTL after an upgrade.

## Open items

- Cache invalidation on upgrade. `invalidate_cache(user_id)` exists in
  `entitlements.py` but nothing calls it yet. The Checkout success
  redirect page should call it, otherwise a user who just paid can see
  stale Free-tier gating for up to 60 seconds.
- Row Level Security policy for the `subscriptions` table has not been
  defined. Current design assumes only the service role key reads and
  writes it, but if the mobile app ever needs to display subscription
  status directly from a client-side Supabase call, an RLS policy
  restricting each user to their own row will be needed.
- Twin Studio gating rules do not exist yet. Add a
  `check_twin_studio_access` function to `entitlement_rules.py` when that
  module's free/pro boundary is decided.
