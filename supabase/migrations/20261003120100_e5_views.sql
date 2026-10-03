-- E5: read-only views for the web. Every view is security_invoker, so the caller's own RLS
-- on the base tables applies (a view never widens access). Columns are an explicit list so a
-- later base-table column cannot leak through by accident.

create index if not exists change_events_property_observed_idx
    on serving.change_events(property_id, observed_at desc);

create view serving.v_market_public with (security_invoker = true) as
select slug, city, state, tz, active, is_demo, last_checked_at, next_check_at,
       last_refreshed_at, check_status, listings_active, changed_last_check
from serving.markets;

create view serving.v_shortlist with (security_invoker = true) as
select
    p.id as property_id, p.market_id, m.slug as market_slug,
    p.address, p.unit, p.city, p.state, p.zip, p.lat, p.lng,
    p.property_type, p.beds, p.baths, p.sqft, p.year_built,
    p.price, p.price_is_placeholder, p.price_per_sqft, p.dom,
    p.listing_status_normalized, p.listed_at, p.refreshed_at, p.photo_count,
    sc.motivated_seller_score, sc.motivated_seller_grade,
    sc.motivated_seller_percentile, sc.motivated_seller_tier,
    sc.fix_flip_score, sc.fix_flip_grade, sc.fix_flip_percentile, sc.fix_flip_tier,
    sc.buy_hold_score, sc.buy_hold_grade, sc.buy_hold_percentile, sc.buy_hold_tier,
    sc.str_score, sc.str_grade, sc.str_percentile, sc.str_tier,
    sc.airbnb_score, sc.airbnb_grade, sc.airbnb_percentile, sc.airbnb_tier,
    lc.kind as latest_change_kind, lc.observed_at as latest_change_at
from serving.properties p
join serving.markets m on m.id = p.market_id
left join lateral (
    select
        max(s.score) filter (where s.lens = 'motivated_seller') as motivated_seller_score,
        max(s.grade) filter (where s.lens = 'motivated_seller') as motivated_seller_grade,
        max(s.percentile) filter (where s.lens = 'motivated_seller') as motivated_seller_percentile,
        max(s.tier) filter (where s.lens = 'motivated_seller') as motivated_seller_tier,
        max(s.score) filter (where s.lens = 'fix_flip') as fix_flip_score,
        max(s.grade) filter (where s.lens = 'fix_flip') as fix_flip_grade,
        max(s.percentile) filter (where s.lens = 'fix_flip') as fix_flip_percentile,
        max(s.tier) filter (where s.lens = 'fix_flip') as fix_flip_tier,
        max(s.score) filter (where s.lens = 'buy_hold') as buy_hold_score,
        max(s.grade) filter (where s.lens = 'buy_hold') as buy_hold_grade,
        max(s.percentile) filter (where s.lens = 'buy_hold') as buy_hold_percentile,
        max(s.tier) filter (where s.lens = 'buy_hold') as buy_hold_tier,
        max(s.score) filter (where s.lens = 'str') as str_score,
        max(s.grade) filter (where s.lens = 'str') as str_grade,
        max(s.percentile) filter (where s.lens = 'str') as str_percentile,
        max(s.tier) filter (where s.lens = 'str') as str_tier,
        max(s.score) filter (where s.lens = 'airbnb') as airbnb_score,
        max(s.grade) filter (where s.lens = 'airbnb') as airbnb_grade,
        max(s.percentile) filter (where s.lens = 'airbnb') as airbnb_percentile,
        max(s.tier) filter (where s.lens = 'airbnb') as airbnb_tier
    from serving.scores s where s.property_id = p.id
) sc on true
left join lateral (
    select ce.kind, ce.observed_at from serving.change_events ce
    where ce.property_id = p.id order by ce.observed_at desc limit 1
) lc on true
where p.delisted_at is null;

create view serving.v_property with (security_invoker = true) as
select
    p.id as property_id, p.market_id, m.slug as market_slug, m.tz as market_tz,
    m.last_checked_at as market_last_checked_at, m.last_refreshed_at as market_last_refreshed_at,
    p.redfin_id, p.apn, p.address, p.unit, p.city, p.state, p.zip, p.lat, p.lng, p.county,
    p.property_type, p.beds, p.baths, p.sqft, p.lot_sqft, p.year_built, p.year_renovated,
    p.stories, p.hoa_monthly, p.price, p.price_is_placeholder, p.price_per_sqft, p.dom, p.dom_mls,
    p.listing_status, p.listing_status_normalized, p.listed_at, p.listing_url,
    p.first_seen_at, p.last_seen_at, p.delisted_at, p.refreshed_at,
    p.flags, p.climate, p.mobility, p.description, p.ai_summary, p.photo_count,
    v.market_value, v.market_value_method, v.arv, v.arv_method, v.price_to_value_ratio,
    v.comp_count, v.comp_median_ppsf, v.comp_p75_ppsf, v.rehab_low, v.rehab_mid, v.rehab_high,
    v.condition_class, v.rent_estimate_monthly, v.rent_method, v.gross_yield, v.annual_taxes,
    v.annual_insurance, v.annual_hoa, v.annual_operating_expenses, v.noi_annual, v.cap_rate,
    v.max_allowable_offer, v.valuation_confidence, v.flags as valuation_flags,
    f.features, n.schools, n.location_scores, n.transport_count,
    (select coalesce(jsonb_object_agg(s.lens, jsonb_build_object(
        'score', s.score, 'grade', s.grade, 'percentile', s.percentile,
        'confidence', s.confidence, 'tier', s.tier, 'prev_score', s.prev_score,
        'prev_percentile', s.prev_percentile, 'rationale', s.rationale, 'flags', s.flags,
        'breakdown', s.breakdown, 'version', s.version, 'computed_at', s.computed_at
    )), '{}'::jsonb) from serving.scores s where s.property_id = p.id) as scores
from serving.properties p
join serving.markets m on m.id = p.market_id
left join serving.valuation v on v.property_id = p.id
left join serving.features f on f.property_id = p.id
left join serving.neighbourhood n on n.property_id = p.id;

create view serving.v_agent with (security_invoker = true) as
select a.property_id, p.market_id, m.slug as market_slug,
       a.listing_agent, a.listing_agent_phone, a.listing_broker, a.mls_id, a.source
from serving.agents a
join serving.properties p on p.id = a.property_id
join serving.markets m on m.id = p.market_id;

revoke all on serving.v_market_public, serving.v_shortlist, serving.v_property, serving.v_agent
    from public, anon, authenticated;
grant select on serving.v_market_public to anon, authenticated;
grant select on serving.v_shortlist, serving.v_property, serving.v_agent to authenticated;
grant select on serving.v_market_public, serving.v_shortlist, serving.v_property, serving.v_agent
    to service_role;
