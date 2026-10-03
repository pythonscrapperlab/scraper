-- E5: additive app.* changes the engine needs to evaluate alerts and morning briefs.
-- Nothing here removes or renames a column. The web repository owns the final app.* shape;
-- docs/handover-web.md states exactly what it must keep.

alter table app.orgs
    add column if not exists tz text not null default 'America/New_York',
    add column if not exists default_lens text not null default 'motivated_seller';

alter table app.orgs drop constraint if exists orgs_default_lens_check;
alter table app.orgs add constraint orgs_default_lens_check
    check (default_lens in ('motivated_seller', 'fix_flip', 'buy_hold', 'str', 'airbnb'));

-- A threshold is a percentile floor, a tier floor, or both (percentile wins when both are set).
alter table app.thresholds
    add column if not exists min_percentile numeric
        check (min_percentile > 0 and min_percentile <= 100);
alter table app.thresholds alter column tier drop not null;
alter table app.thresholds drop constraint if exists thresholds_rule_check;
alter table app.thresholds add constraint thresholds_rule_check
    check (tier is not null or min_percentile is not null);

-- Quiet hours are org-local wall-clock times; start > end means the window crosses midnight.
alter table app.notification_settings
    add column if not exists quiet_hours_enabled boolean not null default true,
    add column if not exists quiet_start time not null default '21:00',
    add column if not exists quiet_end time not null default '07:00';

-- A percentile threshold can fire for a property whose tier is still 'rest'.
alter table app.alert_events drop constraint if exists alert_events_tier_check;
alter table app.alert_events add constraint alert_events_tier_check
    check (tier in ('top', 'strong', 'rest'));
alter table app.alert_events add column if not exists dedupe_key text;
create unique index if not exists alert_events_dedupe_idx
    on app.alert_events(org_id, dedupe_key) where dedupe_key is not null;

-- The engine only enqueues. The web's sender must not deliver before send_after.
alter table app.email_queue
    add column if not exists send_after timestamptz not null default now(),
    add column if not exists dedupe_key text;
create unique index if not exists email_queue_dedupe_idx
    on app.email_queue(org_id, dedupe_key) where dedupe_key is not null;
drop index if exists app.email_queue_pending_idx;
create index email_queue_pending_idx on app.email_queue(status, send_after)
    where status = 'queued';
