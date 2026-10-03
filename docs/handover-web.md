# Handover to the web repository (engine v0.1.0)

Written for the engineers building the Next.js app. The engine pushes data to Supabase; the web
only ever **reads** `serving.*` and **reads and writes `app.*`**. Read `docs/schema.md` for the
view definitions and `docs/breakdown/*.md` for what each score breakdown contains.

> **Status of the per-method map.** The web repo's `DataApi` interface had not been pasted when
> this was written, so section 3 is organised by the data each screen needs. Section 4 is the
> table to fill, one row per `DataApi` method, once the interface is shared. No method has been
> invented here.

## 0. Before the first query

1. **Expose the schemas.** In Supabase -> Settings -> API -> *Exposed schemas*, add `serving` and
   `app`. (Verify; the engine does not depend on it and this was not checked.) Client calls then
   look like `supabase.schema('serving').from('v_shortlist')`.
2. **Keys.** The browser and server components use the publishable (anon) key plus the user's JWT.
   The service-role key must never reach the client. The web needs it server-side only for the
   writes listed in section 5.
3. **Everything is market-scoped by RLS.** A signed-in user sees a market only if some org they
   belong to has it in `app.org_markets` with `enabled = true`. Do not add your own `market_id`
   authorisation; filter for UX, not for security.
4. **Honesty rules.** Show `last_checked_at` and `last_refreshed_at` on every surface. Show
   `check_status` (`ok`, `late`, `failed`, `warming`) and, when it is not `ok`, say the data may be
   older. A `null` score means that lens could not be scored for that property: show "not scored",
   never `0`. Absolute scores are shown but never used for colour; colour by `tier`.

## 1. Plans and what RLS enforces

| Plan | Properties, scores, valuation, comps, history, images, features, neighbourhood, tax, change feed | Agent and broker contact (`v_agent`) |
|---|---|---|
| `starter` | yes, for the org's enabled markets | **no rows** (RLS), so hide the UI |
| `pro`, `growth`, `brokerage` | yes | yes |

City limits per plan are **not** enforced by the engine or by RLS: the web must refuse to insert
more `app.org_markets` rows than the plan allows.

## 2. Public (signed-out) data

| Need | Query |
|---|---|
| Live coverage list | `serving.v_market_public` where `active` (order by `city`) |
| Freshness badge for a city | same view: `last_checked_at, next_check_at, last_refreshed_at, check_status, listings_active, changed_last_check` |
| Landing-page demo | `serving.demo_snapshots` where `market_slug = :slug and lens = :lens` -> `payload` (JSON; 3 full rows, 5 stubs, freshness block; no agent fields, no full breakdown) |

## 3. Signed-in data, by need

| Need | View / table | Columns | Filters and ordering |
|---|---|---|---|
| Shortlist for a market and lens | `serving.v_shortlist` | identity and facts, `price`, `dom`, `<lens>_score/_grade/_percentile/_tier`, `latest_change_kind`, `latest_change_at` | `market_slug = :slug`, `<lens>_tier in ('top','strong')`, order by `<lens>_percentile desc nulls last`, `limit`. Optional: `zip`, `beds >= n`, `price <= n`, `property_type` |
| "Worth a call" | `v_shortlist` | as above | `<lens>_tier = 'top'` |
| What changed lately | `serving.change_events` | `kind, detail, observed_at, property_id` | `market_id = :id` (read `id` from `serving.markets` by `slug`; that column is selectable) or `property_id = :pid`; order by `observed_at desc`. Kinds: `new, price_cut, price_increase, status, relisted, delisted, tier_up, tier_down, score_move` |
| Property page | `serving.v_property` | everything incl. `scores` JSON, `valuation_flags`, `features`, `schools`, `location_scores` | `property_id = :pid` |
| Score anatomy panel | `v_property.scores -> :lens -> breakdown` | components with weight and subscore, adjustments (applied or not), flags, rationale | shape in `AGENTS.md` 6.2; render every row including "not triggered" |
| Photos | `serving.images` | `url` | `property_id = :pid` order by `sort_order` (max 12) |
| Sold comps | `serving.comps` | `address, price, beds, baths, sqft, sold_date` | `property_id = :pid` order by `sold_date desc` (at most 6) |
| Price and status history | `serving.history` | `event_type, event, price, event_date, event_source, is_rental` | `property_id = :pid`, order by `event_date desc` (at most 15 events, last 7 years; rentals are not published, so the `is_rental` filter is a no-op) |
| Tax history | `serving.tax_history` | `tax_year, tax_amount, assessed_value` | `property_id = :pid` order by `tax_year desc` (latest 5 years; `assessed_value` is often null) |
| Listing agent and broker | `serving.v_agent` | `listing_agent, listing_agent_phone, listing_broker, mls_id, source` | `property_id = :pid`; **empty result on Starter is normal** |
| Market trend | `serving.market_daily` | `day, listings_active, tier_counts` | `market_id = :id` order by `day` |
| Engine health in the UI | `serving.runs`, `v_market_public` | `kind, status, started_at, duration_s` | `market_id = :id` order by `started_at desc limit 20` |
| Alerts feed | `app.alert_events` | `id, property_id, market_slug, lens, tier, payload, created_at, read_at` | `org_id` (RLS limits it), order by `created_at desc`; mark read with `update ... set read_at = now()` (only `read_at` is writable) |
| Notification preferences | `app.notification_settings` | `morning_brief, tier_alerts, in_app, email, quiet_hours_enabled, quiet_start, quiet_end` | own row; insert/update allowed by RLS |
| Alert thresholds | `app.thresholds` | `lens, tier, min_percentile, enabled` | read by RLS; writes: section 5 |
| The org's markets | `app.org_markets` | `market_slug, enabled` | read by RLS; writes: section 5 |

Notes: `<lens>` is one of `motivated_seller`, `fix_flip`, `buy_hold`, `str`, `airbnb`. `v_shortlist`
excludes delisted listings; `v_property` keeps them for 7 days after delisting (`delisted_at`
set), so a saved link to a sold home still renders. A property that stops appearing after 7 days
has been pruned. An alert about a pruned property keeps its row (`app.alert_events.property_id` becomes `null`);
render it from `payload` and do not link.

**Adding a city.** Insert `app.org_markets (org_id, market_slug)`. Within 5 minutes the engine
discovers it, runs a `warming` check and the first push creates the `serving.markets` row; until
then `v_market_public` has no row, so show "setting up". A slug the engine has no Redfin region id
for (not in `scrapers/constants.py`) is logged as unschedulable and **never appears**; there is no
feedback channel for that yet, so offer only slugs the engine supports. Currently: Orlando,
Miami, Tampa, Vero Beach (FL) and San Jose (CA).

## 4. Per-method map (to fill from the pasted `DataApi`)

| `DataApi` method | View / table | Columns | Filters | Notes |
|---|---|---|---|---|
| *(paste the interface and this table is completed method by method)* | | | | |

## 5. The shape the web must give the `app.*` tables

The engine reads and writes these exact columns; keep them. Adding columns is fine.

| Table | Engine reads | Engine writes | Web must provide |
|---|---|---|---|
| `app.orgs` | `id, plan, tz, default_lens` | - | `plan` in `starter,pro,growth,brokerage`; `tz` is an IANA name (set from the user's browser at onboarding); `default_lens` is one of the five lens codes. The stub has no trial/active flag, so enablement is only `org_markets.enabled`; add a flag and tell the engine if cancelled orgs should stop being scheduled |
| `app.org_members` | `org_id, user_id` | - | every user who should receive mail is a member |
| `app.org_markets` | `market_slug, enabled` | - | **owner/admin insert, update, delete**: not granted by the engine's RLS; do it through a server route (service role) or add policies. Enforce the plan's city limit here |
| `app.thresholds` | `org_id, lens, tier, min_percentile, enabled` | - | one row per org per lens. Set `min_percentile` (1-100) or `tier` (`top` or `strong`); percentile wins if both. Same write caveat as `org_markets` |
| `app.notification_settings` | all columns | - | one row per member; a **missing row means all defaults on** (brief, alerts, in-app, e-mail, quiet hours 21:00-07:00), so you need not create rows eagerly |
| `app.alert_events` | - | one row per alert per org (idempotent via `dedupe_key`) | list, mark read |
| `app.email_queue` | - | `template in ('threshold_alert','morning_brief')`, `payload`, `send_after`, `dedupe_key`, `status='queued'` | the **sender** (below) |

### The e-mail sender contract (the web owns delivery; the engine never sends mail)

1. Pick rows with `status = 'queued' and send_after <= now()`, oldest `queued_at` first, with
   `for update skip locked`; set `processing`, send, then `sent` with `processed_at`, or `failed`
   with a class-only `error_class`. Never write exception text or addresses into `error_class`.
2. Do not send before `send_after`: that is the quiet-hours rule.
3. The recipient is `user_id` (an `auth.users` id). Resolve the address at send time; the engine
   stores none.
4. Do not log `payload`; it contains listing addresses.

`threshold_alert` payload: `{market_slug, total, items[<=20]}`; each item has
`property_id, lens, reason ('crossed'|'new_qualifier'), score, grade, percentile, tier,
previous_percentile, previous_tier, address, price`. `total` can exceed the 20 items shown.

`morning_brief` payload: `{market_slug, lens, local_date, top[], changes_24h{counts, examples[]},
freshness{last_checked_at, last_refreshed_at, check_status, listings_active, note}}`; each `top`
item has `property_id, address, city, zip, price, beds, baths, sqft, dom, score, grade, percentile,
tier`. When `freshness.note` is not null, print it. `lens` is the org's `default_lens`; the number
of items is plan-based (5, 10, 15, 25). Deep links should use `property_id`.

## 6. Known limits to design around

- Data is Redfin only (MLS facts as distributed via Redfin). Say so in the sources copy.
- No owner name, mailing address, liens, permits or tax-delinquency data exists. Pro ships listing
  agent and broker contact only.
- The Airbnb lens is a proxy, and `str` means mid-term (30+ days). Use the labels in `AGENTS.md` 3.
- Score distributions are compressed (the motivated-seller lens tops out at 78.7 and Orlando has no row at 80 or above), which
  is why "worth a call" is a percentile tier, not an absolute score.
- Freshness is two-tier: search-level checks about every two hours 07:00-21:00 market-local, detail
  refresh nightly and immediately for changed listings. The laptop pushes; if it is offline the
  market goes `late` and the UI must say so.

## 7. What changed in serving schema v3 (E6)

The column set is unchanged; payload content is slimmer. See `docs/schema.md` ("Serving v3 payload contract").

- `features.features` is now a curated object of at most 25 named keys (list in `docs/schema.md`), not the raw
  amenities blob. Read missing keys as "unknown", never as "no".
- `properties.description` is contact-redacted and at most 600 characters; `ai_summary` is redacted. Do not
  try to re-add contact details; Pro agent contact comes only from `v_agent`.
- `history` is at most 15 events / 7 years and has no rental rows; `tax_history` is 5 years; `comps` is 6.
- `properties.dom`/`dom_mls`/`last_seen_at`/`refreshed_at` can lag on an unchanged listing (they travel with
  the next real change). Prefer `listed_at` for an exact days-on-market and `markets.last_refreshed_at` for freshness.
- `scores.breakdown` drivers have no `null` values and only carry `field` when it names a served column.
- Delisted listings disappear after 7 days. `app.alert_events.property_id` may be `null` for a pruned property.
