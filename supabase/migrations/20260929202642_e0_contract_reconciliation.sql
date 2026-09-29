-- Forward repair for projects that applied provisional 0001/0002 before AGENTS.md
-- was restored. The deployed serving tables were verified empty before this
-- migration was authored; app-owned tables are preserved.

drop schema serving cascade;

create schema serving;
revoke all on schema serving from public;

create table serving.markets (
 id uuid primary key, city text not null, state text not null check (char_length(state)=2),
 region_id text not null, slug text not null unique, tz text not null, zips text[] not null default '{}',
 active boolean not null default true, is_demo boolean not null default false,
 check_cadence_minutes int not null default 120 check (check_cadence_minutes>0),
 last_checked_at timestamptz, next_check_at timestamptz, last_refreshed_at timestamptz,
 check_status text not null default 'warming' check (check_status in ('ok','late','failed','warming')),
 listings_active int not null default 0, changed_last_check int not null default 0,
 pool_size jsonb not null default '{}'::jsonb, source_status jsonb not null default '{}'::jsonb,
 updated_at timestamptz not null default now()
);
create table serving.runs (
 id uuid primary key, market_id uuid not null references serving.markets(id) on delete cascade,
 kind text not null, trigger text not null, started_at timestamptz not null, finished_at timestamptz,
 status text not null, counts jsonb not null default '{}'::jsonb, error_class text, duration_s int
);
create table serving.properties (
 id uuid primary key, market_id uuid not null references serving.markets(id) on delete cascade,
 redfin_id text, apn text, address text not null, unit text, city text not null,
 state text not null check (char_length(state)=2), zip text not null, lat numeric, lng numeric,
 county text, property_type text, beds int, baths numeric, sqft int, lot_sqft numeric,
 year_built int, year_renovated int, stories numeric, hoa_monthly numeric, price int,
 price_is_placeholder boolean not null default false, price_per_sqft numeric, dom int, dom_mls int,
 listing_status text, listing_status_normalized text, listed_at timestamptz, listing_url text,
 first_seen_at timestamptz, last_seen_at timestamptz, delisted_at timestamptz, refreshed_at timestamptz,
 flags jsonb not null default '{}'::jsonb, climate jsonb not null default '{}'::jsonb,
 mobility jsonb not null default '{}'::jsonb, description text, ai_summary text,
 photo_count int not null default 0, updated_at timestamptz not null default now()
);
create table serving.scores (
 property_id uuid not null references serving.properties(id) on delete cascade, lens text not null,
 score numeric not null, grade text not null, percentile numeric not null, confidence numeric not null,
 tier text not null check (tier in ('top','strong','rest')), prev_score numeric, prev_percentile numeric,
 rationale text, flags jsonb not null default '[]'::jsonb, breakdown jsonb, version text not null,
 computed_at timestamptz not null, primary key(property_id,lens)
);
create table serving.valuation (
 property_id uuid primary key references serving.properties(id) on delete cascade,
 market_value numeric, market_value_method text, arv numeric, arv_method text, price_to_value_ratio numeric,
 comp_count int, comp_median_ppsf numeric, comp_p75_ppsf numeric, rehab_low numeric, rehab_mid numeric,
 rehab_high numeric, condition_class text, rent_estimate_monthly numeric, rent_method text,
 gross_yield numeric, annual_taxes numeric, annual_insurance numeric, annual_hoa numeric,
 annual_operating_expenses numeric, noi_annual numeric, cap_rate numeric, max_allowable_offer numeric,
 valuation_confidence numeric, flags jsonb not null default '[]'::jsonb, version text not null,
 computed_at timestamptz not null
);
create table serving.agents (
 property_id uuid primary key references serving.properties(id) on delete cascade,
 listing_agent text, listing_agent_phone text, listing_broker text, mls_id text, source text
);
create table serving.images (
 property_id uuid not null references serving.properties(id) on delete cascade,
 sort_order int not null, url text not null, primary key(property_id,sort_order)
);
create table serving.comps (
 id uuid primary key, property_id uuid not null references serving.properties(id) on delete cascade,
 address text, price int, beds int, baths numeric, sqft int, sold_date date
);
create table serving.history (
 id uuid primary key, property_id uuid not null references serving.properties(id) on delete cascade,
 event_type text, event text, price int, event_date timestamptz, event_source text,
 is_rental boolean not null default false
);
create table serving.features (
 property_id uuid primary key references serving.properties(id) on delete cascade,
 features jsonb not null default '{}'::jsonb
);
create table serving.neighbourhood (
 property_id uuid primary key references serving.properties(id) on delete cascade,
 schools jsonb not null default '[]'::jsonb, location_scores jsonb not null default '{}'::jsonb,
 transport_count int not null default 0
);
create table serving.tax_history (
 property_id uuid not null references serving.properties(id) on delete cascade, tax_year int not null,
 tax_amount int, assessed_value int, primary key(property_id,tax_year)
);
create table serving.change_events (
 id uuid primary key, property_id uuid not null references serving.properties(id) on delete cascade,
 market_id uuid not null references serving.markets(id) on delete cascade,
 kind text not null check (kind in ('new','price_cut','price_increase','status','relisted','delisted','tier_up','tier_down','score_move')),
 detail jsonb not null default '{}'::jsonb, observed_at timestamptz not null
);
create table serving.market_daily (
 market_id uuid not null references serving.markets(id) on delete cascade, day date not null,
 listings_active int not null, tier_counts jsonb not null default '{}'::jsonb,
 primary key(market_id,day)
);
create table serving.demo_snapshots (
 market_slug text not null, lens text not null, payload jsonb not null, generated_at timestamptz not null,
 primary key(market_slug,lens)
);
create table serving.heartbeats (id serial primary key, at timestamptz not null default now(), note text);
create table serving.schema_version (version int primary key);
insert into serving.schema_version(version) values (2);

create index properties_market_status_idx on serving.properties(market_id,listing_status_normalized);
create index properties_zip_idx on serving.properties(zip);
create index scores_lens_percentile_idx on serving.scores(lens,percentile desc);
create index scores_lens_tier_idx on serving.scores(lens,tier);
create index change_events_market_observed_idx on serving.change_events(market_id,observed_at desc);
create index history_property_event_date_idx on serving.history(property_id,event_date desc);

alter table app.alert_events add constraint alert_events_property_fk
 foreign key(property_id) references serving.properties(id) on delete cascade;

grant usage on schema serving to anon, authenticated, service_role;
grant all on all tables in schema serving to service_role;
grant all on all sequences in schema serving to service_role;
grant select on serving.demo_snapshots to anon, authenticated;
grant select (slug,city,state,tz,active,is_demo,last_checked_at,next_check_at,last_refreshed_at,
 check_status,listings_active,changed_last_check) on serving.markets to anon, authenticated;
grant select on serving.properties,serving.scores,serving.valuation,serving.images,serving.comps,
 serving.history,serving.features,serving.neighbourhood,serving.tax_history,serving.change_events,
 serving.runs,serving.market_daily,serving.agents to authenticated;

alter table serving.markets enable row level security;
alter table serving.runs enable row level security;
alter table serving.properties enable row level security;
alter table serving.scores enable row level security;
alter table serving.valuation enable row level security;
alter table serving.agents enable row level security;
alter table serving.images enable row level security;
alter table serving.comps enable row level security;
alter table serving.history enable row level security;
alter table serving.features enable row level security;
alter table serving.neighbourhood enable row level security;
alter table serving.tax_history enable row level security;
alter table serving.change_events enable row level security;
alter table serving.market_daily enable row level security;
alter table serving.demo_snapshots enable row level security;
alter table serving.heartbeats enable row level security;
alter table serving.schema_version enable row level security;

create policy markets_select_public on serving.markets for select to anon using(active);
create policy markets_select_subscribed on serving.markets for select to authenticated using(exists(
 select 1 from app.org_markets om join app.org_members m on m.org_id=om.org_id
 where om.market_slug=markets.slug and om.enabled and m.user_id=(select auth.uid())));
create policy demo_snapshots_select_public on serving.demo_snapshots
 for select to anon,authenticated using(true);
create policy runs_select_subscribed on serving.runs for select to authenticated using(exists(
 select 1 from serving.markets sm join app.org_markets om on om.market_slug=sm.slug and om.enabled
 join app.org_members m on m.org_id=om.org_id where sm.id=runs.market_id and m.user_id=(select auth.uid())));
create policy properties_select_subscribed on serving.properties for select to authenticated using(exists(
 select 1 from serving.markets sm join app.org_markets om on om.market_slug=sm.slug and om.enabled
 join app.org_members m on m.org_id=om.org_id where sm.id=properties.market_id and m.user_id=(select auth.uid())));
create policy scores_select_subscribed on serving.scores for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id
 join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id
 where p.id=scores.property_id and m.user_id=(select auth.uid())));
create policy valuation_select_subscribed on serving.valuation for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id
 join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id
 where p.id=valuation.property_id and m.user_id=(select auth.uid())));
create policy agents_select_paid on serving.agents for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id
 join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id
 join app.orgs o on o.id=om.org_id where p.id=agents.property_id and m.user_id=(select auth.uid())
 and o.plan in ('pro','growth','brokerage')));
create policy images_select_subscribed on serving.images for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where p.id=images.property_id and m.user_id=(select auth.uid())));
create policy comps_select_subscribed on serving.comps for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where p.id=comps.property_id and m.user_id=(select auth.uid())));
create policy history_select_subscribed on serving.history for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where p.id=history.property_id and m.user_id=(select auth.uid())));
create policy features_select_subscribed on serving.features for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where p.id=features.property_id and m.user_id=(select auth.uid())));
create policy neighbourhood_select_subscribed on serving.neighbourhood for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where p.id=neighbourhood.property_id and m.user_id=(select auth.uid())));
create policy tax_history_select_subscribed on serving.tax_history for select to authenticated using(exists(
 select 1 from serving.properties p join serving.markets sm on sm.id=p.market_id join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where p.id=tax_history.property_id and m.user_id=(select auth.uid())));
create policy change_events_select_subscribed on serving.change_events for select to authenticated using(exists(
 select 1 from serving.markets sm join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where sm.id=change_events.market_id and m.user_id=(select auth.uid())));
create policy market_daily_select_subscribed on serving.market_daily for select to authenticated using(exists(
 select 1 from serving.markets sm join app.org_markets om on om.market_slug=sm.slug and om.enabled join app.org_members m on m.org_id=om.org_id where sm.id=market_daily.market_id and m.user_id=(select auth.uid())));
