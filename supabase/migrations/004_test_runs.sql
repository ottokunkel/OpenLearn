-- ============================================================
-- Test runs table
-- ============================================================
-- Stores benchmark and integration test runs triggered from
-- the admin panel. The worker picks up jobs from the test_jobs
-- pgmq queue and writes progress/results back here.

create table public.test_runs (
    id              uuid primary key default gen_random_uuid(),
    type            text not null check (type in ('benchmark', 'integration')),
    status          text not null default 'pending'
                        check (status in ('pending', 'running', 'completed', 'failed', 'cancelled')),
    config          jsonb not null default '{}'::jsonb,
    pdf_source      text not null check (pdf_source in ('builtin', 'storage')),
    pdf_label       text not null,
    pdf_path        text,
    document_id     uuid references public.documents(id) on delete set null,
    progress        jsonb not null default '{}'::jsonb,
    logs            text[] not null default '{}',
    results         jsonb,
    error_message   text,
    started_at      timestamptz,
    completed_at    timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index idx_test_runs_status on public.test_runs (status)
    where status in ('pending', 'running');
create index idx_test_runs_type on public.test_runs (type, created_at desc);
create index idx_test_runs_created on public.test_runs (created_at desc);

-- Reuse the existing updated_at trigger function
create trigger test_runs_updated_at
    before update on public.test_runs
    for each row execute function public.update_updated_at();

-- ============================================================
-- RLS: admin-only
-- ============================================================
alter table public.test_runs enable row level security;

create policy "Admins can view test runs"
    on public.test_runs for select using (public.is_admin());
create policy "Admins can insert test runs"
    on public.test_runs for insert with check (public.is_admin());
create policy "Admins can update test runs"
    on public.test_runs for update using (public.is_admin());
create policy "Admins can delete test runs"
    on public.test_runs for delete using (public.is_admin());

-- ============================================================
-- pgmq queue for test jobs
-- ============================================================
select pgmq.create('test_jobs');

-- ============================================================
-- Helper: enqueue a test run (admin-only)
-- ============================================================
create or replace function public.enqueue_test_run(
    p_type text,
    p_config jsonb,
    p_pdf_source text,
    p_pdf_label text,
    p_pdf_path text,
    p_document_id uuid
) returns uuid
language plpgsql security definer
as $$
declare
    v_run_id uuid;
    v_initial_progress jsonb;
begin
    if not public.is_admin() then
        raise exception 'Admin access required';
    end if;

    v_run_id := gen_random_uuid();

    if p_type = 'integration' then
        v_initial_progress := jsonb_build_object(
            'stages', jsonb_build_array(
                jsonb_build_object('name', 'Connect', 'status', 'pending'),
                jsonb_build_object('name', 'Create Document', 'status', 'pending'),
                jsonb_build_object('name', 'Enqueue Job', 'status', 'pending'),
                jsonb_build_object('name', 'Download PDF', 'status', 'pending'),
                jsonb_build_object('name', 'Convert PDF', 'status', 'pending'),
                jsonb_build_object('name', 'Embed & Write Chunks', 'status', 'pending'),
                jsonb_build_object('name', 'Update Status', 'status', 'pending'),
                jsonb_build_object('name', 'Verify', 'status', 'pending'),
                jsonb_build_object('name', 'Vector Search', 'status', 'pending'),
                jsonb_build_object('name', 'Cleanup', 'status', 'pending')
            )
        );
    else
        v_initial_progress := '{}'::jsonb;
    end if;

    insert into public.test_runs
        (id, type, status, config, pdf_source, pdf_label, pdf_path, document_id, progress)
    values
        (v_run_id, p_type, 'pending', p_config, p_pdf_source, p_pdf_label,
         p_pdf_path, p_document_id, v_initial_progress);

    perform pgmq.send(
        'test_jobs',
        jsonb_build_object(
            'test_run_id', v_run_id,
            'type', p_type
        )
    );

    return v_run_id;
end;
$$;
