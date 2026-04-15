-- ============================================================
-- Enable Realtime for doc_worker_v2.documents
-- ============================================================
-- The Supabase-managed `supabase_realtime` publication streams
-- row-level changes to connected clients via the Realtime service.
-- We only need row-state changes (status, artifacts, error_message),
-- not DDL.

do $$
begin
    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'doc_worker_v2'
          and tablename = 'documents'
    ) then
        execute 'alter publication supabase_realtime add table doc_worker_v2.documents';
    end if;
end $$;
