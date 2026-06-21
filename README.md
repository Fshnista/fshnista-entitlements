# fshnista-entitlements

Platform-wide subscription entitlement logic for FSHNiSTA microservices.

One subscription tier per user, `free` or `pro`, stored in Supabase. What
that tier actually permits is defined independently per module. Storefront
is Pro-only. Booking Manager is freemium with a 5-booking monthly cap on
free tier. This package is the single source of truth for both the tier
resolution logic and those per-module rules.

Consumed by `fshnista-booking-manager` and `fshnista-storefront`. The
Stripe webhook handler that writes subscription state lives separately, in
`fshnista-website/api/entitlements/`, since it deploys as a Vercel function
alongside the rest of that site's serverless functions.

## Install in a consuming service

Add to `requirements.txt`:

```
git+https://github.com/Fshnista/fshnista-entitlements.git@v1.0.0
```

Pin to a tag, not a branch. Bump the tag and update the pinned version in
each service when the rules change, that is the entire release process at
current team size, no publishing pipeline needed.

## Quick usage

```python
from fshnista_entitlements import Tier, get_user_tier, check_storefront_access

tier = await get_user_tier(user_id, db)
decision = check_storefront_access(tier)
if not decision.allowed:
    raise HTTPException(403, decision.reason)
```

## Repo structure

```
fshnista_entitlements/      importable package, the actual logic
  __init__.py                 public API surface
  entitlements.py             tier resolution against Supabase, with cache
  entitlement_rules.py        per-module free/pro rules
  entitlement_dependency.py   FastAPI dependency wiring

schema/                     SQL migrations for the subscriptions table
tests/                      Tier 1 unit tests, no database required
docs/                       full architecture documentation
```

Run tests with:

```
pip install -e ".[dev]"
python -m pytest tests/ -v
```

See `docs/ARCHITECTURE.md` for the full system design, the Stripe webhook
flow, and the deployment checklist.
