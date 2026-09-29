-- AevoraeX RLS contract from AGENTS.md section 6.3.

grant usage on schema serving to anon, authenticated, service_role;
grant usage on schema app to authenticated, service_role;
revoke all on all tables in schema app, serving from anon, authenticated;
grant all on all tables in schema app, serving to service_role;
grant all on all sequences in schema app, serving to service_role;

grant select on serving.demo_snapshots to anon, authenticated;
grant select (
    slug, city, state, tz, active, is_demo, last_checked_at, next_check_at,
    last_refreshed_at, check_status, listings_active, changed_last_check
) on serving.markets to anon, authenticated;
grant select on serving.properties, serving.scores, serving.valuation,
    serving.images, serving.comps, serving.history, serving.features,
    serving.neighbourhood, serving.tax_history, serving.change_events,
    serving.runs, serving.market_daily, serving.agents to authenticated;

grant select on app.orgs, app.org_members, app.org_markets, app.thresholds,
    app.notification_settings, app.alert_events to authenticated;
grant insert, update on app.notification_settings to authenticated;
grant update (read_at) on app.alert_events to authenticated;

alter table app.orgs enable row level security;
alter table app.org_members enable row level security;
alter table app.org_markets enable row level security;
alter table app.thresholds enable row level security;
alter table app.notification_settings enable row level security;
alter table app.alert_events enable row level security;
alter table app.email_queue enable row level security;

create policy org_members_select_self on app.org_members
    for select to authenticated
    using (user_id = (select auth.uid()));

create policy orgs_select_member on app.orgs
    for select to authenticated
    using (exists (
        select 1 from app.org_members m
        where m.org_id = orgs.id and m.user_id = (select auth.uid())
    ));

create policy org_markets_select_member on app.org_markets
    for select to authenticated
    using (exists (
        select 1 from app.org_members m
        where m.org_id = org_markets.org_id and m.user_id = (select auth.uid())
    ));

create policy thresholds_select_member on app.thresholds
    for select to authenticated
    using (exists (
        select 1 from app.org_members m
        where m.org_id = thresholds.org_id and m.user_id = (select auth.uid())
    ));

create policy notification_settings_select_self on app.notification_settings
    for select to authenticated
    using (user_id = (select auth.uid()));

create policy notification_settings_insert_self on app.notification_settings
    for insert to authenticated
    with check (
        user_id = (select auth.uid()) and exists (
            select 1 from app.org_members m
            where m.org_id = notification_settings.org_id
              and m.user_id = (select auth.uid())
        )
    );

create policy notification_settings_update_self on app.notification_settings
    for update to authenticated
    using (user_id = (select auth.uid()))
    with check (
        user_id = (select auth.uid()) and exists (
            select 1 from app.org_members m
            where m.org_id = notification_settings.org_id
              and m.user_id = (select auth.uid())
        )
    );

create policy alert_events_select_member on app.alert_events
    for select to authenticated
    using (exists (
        select 1 from app.org_members m
        where m.org_id = alert_events.org_id and m.user_id = (select auth.uid())
    ));

create policy alert_events_update_member on app.alert_events
    for update to authenticated
    using (exists (
        select 1 from app.org_members m
        where m.org_id = alert_events.org_id and m.user_id = (select auth.uid())
    ))
    with check (exists (
        select 1 from app.org_members m
        where m.org_id = alert_events.org_id and m.user_id = (select auth.uid())
    ));

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

create policy markets_select_public on serving.markets
    for select to anon using (active);

create policy markets_select_subscribed on serving.markets
    for select to authenticated
    using (exists (
        select 1 from app.org_markets om
        join app.org_members m on m.org_id = om.org_id
        where om.market_slug = markets.slug and om.enabled
          and m.user_id = (select auth.uid())
    ));

create policy demo_snapshots_select_public on serving.demo_snapshots
    for select to anon, authenticated using (true);

create policy runs_select_subscribed on serving.runs
    for select to authenticated
    using (exists (
        select 1 from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where sm.id = runs.market_id and m.user_id = (select auth.uid())
    ));

create policy properties_select_subscribed on serving.properties
    for select to authenticated
    using (exists (
        select 1 from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where sm.id = properties.market_id and m.user_id = (select auth.uid())
    ));

create policy scores_select_subscribed on serving.scores
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = scores.property_id and m.user_id = (select auth.uid())
    ));

create policy valuation_select_subscribed on serving.valuation
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = valuation.property_id and m.user_id = (select auth.uid())
    ));

create policy agents_select_paid on serving.agents
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        join app.orgs o on o.id = om.org_id
        where p.id = agents.property_id and m.user_id = (select auth.uid())
          and o.plan in ('pro', 'growth', 'brokerage')
    ));

create policy images_select_subscribed on serving.images
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = images.property_id and m.user_id = (select auth.uid())
    ));

create policy comps_select_subscribed on serving.comps
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = comps.property_id and m.user_id = (select auth.uid())
    ));

create policy history_select_subscribed on serving.history
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = history.property_id and m.user_id = (select auth.uid())
    ));

create policy features_select_subscribed on serving.features
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = features.property_id and m.user_id = (select auth.uid())
    ));

create policy neighbourhood_select_subscribed on serving.neighbourhood
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = neighbourhood.property_id and m.user_id = (select auth.uid())
    ));

create policy tax_history_select_subscribed on serving.tax_history
    for select to authenticated using (exists (
        select 1 from serving.properties p
        join serving.markets sm on sm.id = p.market_id
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where p.id = tax_history.property_id and m.user_id = (select auth.uid())
    ));

create policy change_events_select_subscribed on serving.change_events
    for select to authenticated using (exists (
        select 1 from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where sm.id = change_events.market_id and m.user_id = (select auth.uid())
    ));

create policy market_daily_select_subscribed on serving.market_daily
    for select to authenticated using (exists (
        select 1 from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where sm.id = market_daily.market_id and m.user_id = (select auth.uid())
    ));
