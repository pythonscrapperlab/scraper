-- Exact AevoraeX serving schema v2 contract from AGENTS.md section 6.1.

create schema if not exists serving;
revoke all on schema serving from public;

create table serving.markets (
    id uuid primary key,
    city text not null,
    state text not null check (char_length(state) = 2),
    region_id text not null,
    slug text not null unique,
    tz text not null,
    zips text[] not null default '{}',
    active boolean not null default true,
    is_demo boolean not null default false,
    check_cadence_minutes integer not null default 120 check (check_cadence_minutes > 0),
    last_checked_at timestamptz,
    next_check_at timestamptz,
    last_refreshed_at timestamptz,
    check_status text not null default 'warming'
        check (check_status in ('ok', 'late', 'failed', 'warming')),
    listings_active integer not null default 0,
    changed_last_check integer not null default 0,
    pool_size jsonb not null default '{}'::jsonb,
    source_status jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create table serving.runs (
    id uuid primary key,
    market_id uuid not null references serving.markets(id) on delete cascade,
    kind text not null,
    trigger text not null,
    started_at timestamptz not null,
    finished_at timestamptz,
    status text not null,
    counts jsonb not null default '{}'::jsonb,
    error_class text,
    duration_s integer
);

create table serving.properties (
    id uuid primary key,
    market_id uuid not null references serving.markets(id) on delete cascade,
    redfin_id text,
    apn text,
    address text not null,
    unit text,
    city text not null,
    state text not null check (char_length(state) = 2),
    zip text not null,
    lat numeric,
    lng numeric,
    county text,
    property_type text,
    beds integer,
    baths numeric,
    sqft integer,
    lot_sqft numeric,
    year_built integer,
    year_renovated integer,
    stories numeric,
    hoa_monthly numeric,
    price integer,
    price_is_placeholder boolean not null default false,
    price_per_sqft numeric,
    dom integer,
    dom_mls integer,
    listing_status text,
    listing_status_normalized text,
    listed_at timestamptz,
    listing_url text,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    delisted_at timestamptz,
    refreshed_at timestamptz,
    flags jsonb not null default '{}'::jsonb,
    climate jsonb not null default '{}'::jsonb,
    mobility jsonb not null default '{}'::jsonb,
    description text,
    ai_summary text,
    photo_count integer not null default 0,
    updated_at timestamptz not null default now()
);

create table serving.scores (
    property_id uuid not null references serving.properties(id) on delete cascade,
    lens text not null,
    score numeric not null,
    grade text not null,
    percentile numeric not null,
    confidence numeric not null,
    tier text not null check (tier in ('top', 'strong', 'rest')),
    prev_score numeric,
    prev_percentile numeric,
    rationale text,
    flags jsonb not null default '[]'::jsonb,
    breakdown jsonb,
    version text not null,
    computed_at timestamptz not null,
    primary key (property_id, lens)
);

create table serving.valuation (
    property_id uuid primary key references serving.properties(id) on delete cascade,
    market_value numeric,
    market_value_method text,
    arv numeric,
    arv_method text,
    price_to_value_ratio numeric,
    comp_count integer,
    comp_median_ppsf numeric,
    comp_p75_ppsf numeric,
    rehab_low numeric,
    rehab_mid numeric,
    rehab_high numeric,
    condition_class text,
    rent_estimate_monthly numeric,
    rent_method text,
    gross_yield numeric,
    annual_taxes numeric,
    annual_insurance numeric,
    annual_hoa numeric,
    annual_operating_expenses numeric,
    noi_annual numeric,
    cap_rate numeric,
    max_allowable_offer numeric,
    valuation_confidence numeric,
    flags jsonb not null default '[]'::jsonb,
    version text not null,
    computed_at timestamptz not null
);

create table serving.agents (
    property_id uuid primary key references serving.properties(id) on delete cascade,
    listing_agent text,
    listing_agent_phone text,
    listing_broker text,
    mls_id text,
    source text
);

create table serving.images (
    property_id uuid not null references serving.properties(id) on delete cascade,
    sort_order integer not null,
    url text not null,
    primary key (property_id, sort_order)
);

create table serving.comps (
    id uuid primary key,
    property_id uuid not null references serving.properties(id) on delete cascade,
    address text,
    price integer,
    beds integer,
    baths numeric,
    sqft integer,
    sold_date date
);

create table serving.history (
    id uuid primary key,
    property_id uuid not null references serving.properties(id) on delete cascade,
    event_type text,
    event text,
    price integer,
    event_date timestamptz,
    event_source text,
    is_rental boolean not null default false
);

create table serving.features (
    property_id uuid primary key references serving.properties(id) on delete cascade,
    features jsonb not null default '{}'::jsonb
);

create table serving.neighbourhood (
    property_id uuid primary key references serving.properties(id) on delete cascade,
    schools jsonb not null default '[]'::jsonb,
    location_scores jsonb not null default '{}'::jsonb,
    transport_count integer not null default 0
);

create table serving.tax_history (
    property_id uuid not null references serving.properties(id) on delete cascade,
    tax_year integer not null,
    tax_amount integer,
    assessed_value integer,
    primary key (property_id, tax_year)
);

create table serving.change_events (
    id uuid primary key,
    property_id uuid not null references serving.properties(id) on delete cascade,
    market_id uuid not null references serving.markets(id) on delete cascade,
    kind text not null check (kind in (
        'new', 'price_cut', 'price_increase', 'status', 'relisted', 'delisted',
        'tier_up', 'tier_down', 'score_move'
    )),
    detail jsonb not null default '{}'::jsonb,
    observed_at timestamptz not null
);

create table serving.market_daily (
    market_id uuid not null references serving.markets(id) on delete cascade,
    day date not null,
    listings_active integer not null,
    tier_counts jsonb not null default '{}'::jsonb,
    primary key (market_id, day)
);

create table serving.demo_snapshots (
    market_slug text not null,
    lens text not null,
    payload jsonb not null,
    generated_at timestamptz not null,
    primary key (market_slug, lens)
);

create table serving.heartbeats (
    id serial primary key,
    at timestamptz not null default now(),
    note text
);

create table serving.schema_version (
    version integer primary key
);

insert into serving.schema_version (version) values (2);

create index properties_market_status_idx
    on serving.properties(market_id, listing_status_normalized);
create index properties_zip_idx on serving.properties(zip);
create index scores_lens_percentile_idx on serving.scores(lens, percentile desc);
create index scores_lens_tier_idx on serving.scores(lens, tier);
create index change_events_market_observed_idx
    on serving.change_events(market_id, observed_at desc);
create index history_property_event_date_idx
    on serving.history(property_id, event_date desc);

alter table app.alert_events
    add constraint alert_events_property_fk
    foreign key (property_id) references serving.properties(id) on delete cascade;
