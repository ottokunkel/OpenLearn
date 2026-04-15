-- ============================================================
-- doc-worker-v2: new schema, documents table, pgmq queue,
-- AFTER INSERT trigger, and pgmq RPC wrappers scoped to the
-- doc_ingest queue.
--
-- v1 (public.documents + public.document_jobs) is untouched.
-- ============================================================

-- ============================================================
-- Schema
-- ============================================================
create schema if not exists doc_worker_v2;

grant usage on schema doc_worker_v2 to service_role, authenticated, anon;

-- ============================================================
-- documents (v2)
-- ============================================================
create table doc_worker_v2.documents (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users(id) on delete cascade,
    filename        text not null,
    file_path       text not null,
    status          text not null default 'pending'
                        check (status in ('pending', 'processing', 'retrying', 'completed', 'failed')),
    error_message   text,
    page_count      integer,
    pipeline        text,                                   -- set on completion; e.g. 'granite_docling_vlm'
    artifacts       jsonb not null default '{}'::jsonb,     -- {"markdown":"path.md","docling_json":"path.json",...}
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index idx_docs_v2_user   on doc_worker_v2.documents (user_id, created_at desc);
create index idx_docs_v2_active on doc_worker_v2.documents (status)
    where status in ('pending', 'processing', 'retrying');

-- ============================================================
-- RLS
-- ============================================================
alter table doc_worker_v2.documents enable row level security;

create policy docs_v2_select
    on doc_worker_v2.documents for select using (auth.uid() = user_id);
create policy docs_v2_insert
    on doc_worker_v2.documents for insert with check (auth.uid() = user_id);
create policy docs_v2_update
    on doc_worker_v2.documents for update using (auth.uid() = user_id);
create policy docs_v2_delete
    on doc_worker_v2.documents for delete using (auth.uid() = user_id);

grant all on doc_worker_v2.documents to service_role;
grant select, insert, update, delete on doc_worker_v2.documents to authenticated;

-- ============================================================
-- updated_at touch
-- ============================================================
create or replace function doc_worker_v2.set_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger docs_v2_touch
    before update on doc_worker_v2.documents
    for each row execute function doc_worker_v2.set_updated_at();

-- ============================================================
-- pgmq queue
-- ============================================================
select pgmq.create('doc_ingest');

-- ============================================================
-- AFTER INSERT → enqueue
-- ============================================================
create or replace function doc_worker_v2.enqueue_on_insert()
returns trigger
language plpgsql
security definer
set search_path = pgmq, doc_worker_v2, public
as $$
begin
    perform pgmq.send(
        'doc_ingest',
        jsonb_build_object(
            'document_id', new.id,
            'user_id',     new.user_id,
            'file_path',   new.file_path,
            'filename',    new.filename,
            'metadata',    new.metadata
        )
    );
    return new;
end;
$$;

create trigger docs_v2_enqueue
    after insert on doc_worker_v2.documents
    for each row execute function doc_worker_v2.enqueue_on_insert();

-- ============================================================
-- pgmq wrappers — scoped to the doc_ingest queue, exposed in
-- public so PostgREST serves them without needing the
-- doc_worker_v2 schema in api.schemas just for the RPCs.
-- service_role only.
-- ============================================================
create or replace function public.pgmq_read_doc_ingest(vt integer, qty integer)
returns table (
    msg_id      bigint,
    read_ct     integer,
    enqueued_at timestamptz,
    vt          timestamptz,
    message     jsonb
)
language sql
security definer
set search_path = pgmq, public
as $$
    select msg_id, read_ct, enqueued_at, vt, message
    from pgmq.read('doc_ingest', vt, qty);
$$;

create or replace function public.pgmq_delete_doc_ingest(msg_id bigint)
returns boolean
language sql
security definer
set search_path = pgmq, public
as $$
    select pgmq.delete('doc_ingest', msg_id);
$$;

create or replace function public.pgmq_archive_doc_ingest(msg_id bigint)
returns boolean
language sql
security definer
set search_path = pgmq, public
as $$
    select pgmq.archive('doc_ingest', msg_id);
$$;

revoke all on function public.pgmq_read_doc_ingest(integer, integer) from public, anon, authenticated;
revoke all on function public.pgmq_delete_doc_ingest(bigint)          from public, anon, authenticated;
revoke all on function public.pgmq_archive_doc_ingest(bigint)         from public, anon, authenticated;

grant execute on function public.pgmq_read_doc_ingest(integer, integer) to service_role;
grant execute on function public.pgmq_delete_doc_ingest(bigint)           to service_role;
grant execute on function public.pgmq_archive_doc_ingest(bigint)          to service_role;
