-- E5 performance rewrite of the serving RLS policies (behaviour unchanged).
-- The 0002 policies were correlated EXISTS subqueries evaluated once per row: a 1,196-listing
-- shortlist cost ~186k buffer hits and 1.4 s. Rewritten as uncorrelated IN subqueries the
-- planner hashes the visible market / property set once per statement. The same RLS proof
-- tests (tests/rls) pass before and after; only the evaluation strategy changes.

drop policy if exists runs_select_subscribed on serving.runs;
create policy runs_select_subscribed on serving.runs
    for select to authenticated
    using (market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid())));

drop policy if exists properties_select_subscribed on serving.properties;
create policy properties_select_subscribed on serving.properties
    for select to authenticated
    using (market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid())));

drop policy if exists change_events_select_subscribed on serving.change_events;
create policy change_events_select_subscribed on serving.change_events
    for select to authenticated
    using (market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid())));

drop policy if exists market_daily_select_subscribed on serving.market_daily;
create policy market_daily_select_subscribed on serving.market_daily
    for select to authenticated
    using (market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid())));

drop policy if exists scores_select_subscribed on serving.scores;
create policy scores_select_subscribed on serving.scores
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists valuation_select_subscribed on serving.valuation;
create policy valuation_select_subscribed on serving.valuation
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists images_select_subscribed on serving.images;
create policy images_select_subscribed on serving.images
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists comps_select_subscribed on serving.comps;
create policy comps_select_subscribed on serving.comps
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists history_select_subscribed on serving.history;
create policy history_select_subscribed on serving.history
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists features_select_subscribed on serving.features;
create policy features_select_subscribed on serving.features
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists neighbourhood_select_subscribed on serving.neighbourhood;
create policy neighbourhood_select_subscribed on serving.neighbourhood
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists tax_history_select_subscribed on serving.tax_history;
create policy tax_history_select_subscribed on serving.tax_history
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        where m.user_id = (select auth.uid()))));

drop policy if exists agents_select_paid on serving.agents;
create policy agents_select_paid on serving.agents
    for select to authenticated
    using (property_id in (select p.id from serving.properties p where p.market_id in (select sm.id from serving.markets sm
        join app.org_markets om on om.market_slug = sm.slug and om.enabled
        join app.org_members m on m.org_id = om.org_id
        join app.orgs o on o.id = om.org_id
        where m.user_id = (select auth.uid()) and o.plan in ('pro', 'growth', 'brokerage'))));
