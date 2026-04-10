-- ============================================================
-- Benchmark runs table
-- ============================================================
-- Stores results from the Python benchmark tool so they can be
-- viewed in the admin dashboard.

create table public.benchmark_runs (
    id                  uuid primary key default gen_random_uuid(),
    pdf_label           text not null,
    model               text not null,
    wall_time_seconds   double precision not null,
    page_count          integer not null,
    api_calls           integer not null,
    input_tokens        integer not null,
    output_tokens       integer not null,
    total_tokens        integer not null,
    estimated_cost_usd  double precision not null,
    markdown_length     integer not null,
    error               text,
    created_at          timestamptz not null default now()
);

create index idx_benchmark_runs_model on public.benchmark_runs (model, created_at desc);
create index idx_benchmark_runs_created on public.benchmark_runs (created_at desc);

-- ============================================================
-- RLS: admin-only access
-- ============================================================
alter table public.benchmark_runs enable row level security;

create policy "Admins can view benchmark runs"
    on public.benchmark_runs for select using (public.is_admin());

create policy "Admins can insert benchmark runs"
    on public.benchmark_runs for insert with check (public.is_admin());

create policy "Admins can delete benchmark runs"
    on public.benchmark_runs for delete using (public.is_admin());
