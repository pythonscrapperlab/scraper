-- E6 step 7: close the Supabase advisor finding on public.rls_auto_enable().
--
-- Provenance: the function is the body of the event trigger `ensure_rls` (ddl_command_end), which
-- Supabase installs for the "automatically enable RLS" project setting. It is not in this repo's
-- earlier migrations and is not a manual step of ours. It is SECURITY DEFINER and was executable by
-- anon/authenticated, i.e. callable through the REST API (`/rpc/rls_auto_enable`).
--
-- We REVOKE rather than DROP: an event trigger is invoked by the server, not through EXECUTE rights,
-- so the guard (RLS on any table accidentally created in `public`) keeps working while the function
-- stops being exposed. To remove the guard entirely instead:
--   drop event trigger ensure_rls; drop function public.rls_auto_enable();

do $$
begin
    if to_regprocedure('public.rls_auto_enable()') is not null then
        revoke all on function public.rls_auto_enable() from public;
        revoke all on function public.rls_auto_enable() from anon;
        revoke all on function public.rls_auto_enable() from authenticated;
        revoke all on function public.rls_auto_enable() from service_role;
    end if;
end
$$;
