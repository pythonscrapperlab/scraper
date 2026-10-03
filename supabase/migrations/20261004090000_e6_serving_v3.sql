-- E6 step 5: serving schema v3 and alert history that survives prunes and rebuilds.
--
-- alert_events.property_id used to be NOT NULL with ON DELETE CASCADE (added outside the repo's
-- migrations; the stub in 0000 had no FK at all). Retention (7 days for delisted listings) and
-- `publisher rebuild` delete serving.properties rows, which silently erased users' alert history.
-- The alert keeps its address/price in `payload`, so a null property_id still renders.

alter table app.alert_events alter column property_id drop not null;

alter table app.alert_events drop constraint if exists alert_events_property_fk;
alter table app.alert_events
    add constraint alert_events_property_fk
    foreign key (property_id) references serving.properties(id) on delete set null;

-- v3: curated features (<= 25 named), redacted + capped text, capped history/tax/comps,
-- slimmed scores.breakdown. Column set is unchanged; payload content is not.
update serving.schema_version set version = 3;
