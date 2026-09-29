-- Minimal ownership and delivery tables needed by the engine boundary.
-- The web repository owns their final application shape.

create schema if not exists app;
revoke all on schema app from public;

create table app.orgs (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    plan text not null default 'starter'
        check (plan in ('starter', 'pro', 'growth', 'brokerage')),
    created_at timestamptz not null default now()
);

create table app.org_members (
    org_id uuid not null references app.orgs(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    role text not null check (role in ('owner', 'admin', 'member')),
    created_at timestamptz not null default now(),
    primary key (org_id, user_id)
);

create table app.org_markets (
    org_id uuid not null references app.orgs(id) on delete cascade,
    market_slug text not null,
    enabled boolean not null default true,
    created_at timestamptz not null default now(),
    primary key (org_id, market_slug)
);

create table app.thresholds (
    org_id uuid not null references app.orgs(id) on delete cascade,
    lens text not null
        check (lens in ('motivated_seller', 'fix_flip', 'buy_hold', 'str', 'airbnb')),
    tier text not null check (tier in ('top', 'strong')),
    enabled boolean not null default true,
    updated_at timestamptz not null default now(),
    primary key (org_id, lens)
);

create table app.notification_settings (
    org_id uuid not null references app.orgs(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    morning_brief boolean not null default true,
    tier_alerts boolean not null default true,
    in_app boolean not null default true,
    email boolean not null default true,
    updated_at timestamptz not null default now(),
    primary key (org_id, user_id)
);

create table app.alert_events (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null references app.orgs(id) on delete cascade,
    property_id uuid not null,
    market_slug text not null,
    lens text not null
        check (lens in ('motivated_seller', 'fix_flip', 'buy_hold', 'str', 'airbnb')),
    tier text not null check (tier in ('top', 'strong')),
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    read_at timestamptz
);

create table app.email_queue (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null references app.orgs(id) on delete cascade,
    user_id uuid references auth.users(id) on delete cascade,
    template text not null,
    payload jsonb not null default '{}'::jsonb,
    status text not null default 'queued'
        check (status in ('queued', 'processing', 'sent', 'failed', 'cancelled')),
    queued_at timestamptz not null default now(),
    processed_at timestamptz,
    error_class text
);

create index org_members_user_id_idx on app.org_members(user_id);
create index org_markets_market_slug_idx on app.org_markets(market_slug)
    where enabled;
create index alert_events_org_created_idx on app.alert_events(org_id, created_at desc);
create index email_queue_pending_idx on app.email_queue(status, queued_at)
    where status = 'queued';
