-- E5 fix found by the RLS proof: every "subscribed" policy in 0002 joins serving.markets on
-- sm.id / sm.slug, and a policy subquery runs with the *caller's* privileges. `authenticated`
-- only held column grants on the 12 public columns, so any signed-in read of properties,
-- scores, valuation, ... failed with "permission denied for table markets".
-- Granting the two join columns fixes it without exposing region_id, zips, pool_size or
-- source_status. Row visibility is still limited by markets_select_subscribed.

grant select (id, slug) on serving.markets to authenticated;
