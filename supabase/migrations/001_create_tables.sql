-- ============================================================
-- Extensions
-- ============================================================
create extension if not exists vector with schema extensions;
create extension if not exists pgmq;

-- ============================================================
-- Documents table
-- ============================================================
create table public.documents (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users(id) on delete cascade,
    filename        text not null,
    file_path       text not null,
    status          text not null default 'pending'
                        check (status in ('pending', 'processing', 'completed', 'failed')),
    error_message   text,
    page_count      integer,
    total_chunks    integer,
    doc_json_path   text,
    markdown_path   text,
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index idx_documents_user_id on public.documents (user_id, created_at desc);
create index idx_documents_status on public.documents (status)
    where status in ('pending', 'processing');

-- ============================================================
-- Document chunks with vector embeddings
-- ============================================================
create table public.document_chunks (
    id              uuid primary key default gen_random_uuid(),
    document_id     uuid not null references public.documents(id) on delete cascade,
    user_id         uuid not null references auth.users(id) on delete cascade,
    chunk_index     integer not null,
    content         text not null,
    headings        text[] default '{}',
    label           text,
    page_no         integer,
    embedding       extensions.vector(1536),
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now()
);

create index idx_chunks_document_id on public.document_chunks (document_id, chunk_index);
create index idx_chunks_user_id on public.document_chunks (user_id);
create index idx_chunks_embedding on public.document_chunks
    using hnsw (embedding extensions.vector_cosine_ops);

-- ============================================================
-- RLS
-- ============================================================
alter table public.documents enable row level security;
alter table public.document_chunks enable row level security;

create policy "Users can view own documents"
    on public.documents for select using (auth.uid() = user_id);
create policy "Users can insert own documents"
    on public.documents for insert with check (auth.uid() = user_id);
create policy "Users can update own documents"
    on public.documents for update using (auth.uid() = user_id);
create policy "Users can delete own documents"
    on public.documents for delete using (auth.uid() = user_id);

create policy "Users can view own chunks"
    on public.document_chunks for select using (auth.uid() = user_id);

-- ============================================================
-- pgmq queue
-- ============================================================
select pgmq.create('document_jobs');

-- ============================================================
-- Helper: enqueue a document job (called by edge function)
-- ============================================================
create or replace function public.enqueue_document_job(
    p_document_id uuid,
    p_user_id uuid,
    p_file_path text,
    p_filename text,
    p_metadata jsonb default '{}'::jsonb
) returns bigint
language plpgsql security definer
as $$
declare
    msg_id bigint;
begin
    select pgmq.send(
        'document_jobs',
        jsonb_build_object(
            'document_id', p_document_id,
            'user_id', p_user_id,
            'file_path', p_file_path,
            'filename', p_filename,
            's3_bucket', 'documents',
            's3_key', p_file_path,
            'metadata', p_metadata
        )
    ) into msg_id;
    return msg_id;
end;
$$;

-- ============================================================
-- Helper: vector similarity search (respects RLS)
-- ============================================================
create or replace function public.match_document_chunks(
    query_embedding extensions.vector(1536),
    match_threshold float default 0.7,
    match_count int default 10,
    filter_document_id uuid default null
)
returns table (
    id uuid,
    document_id uuid,
    content text,
    headings text[],
    label text,
    page_no integer,
    metadata jsonb,
    similarity float
)
language plpgsql security invoker
as $$
begin
    return query
    select
        dc.id,
        dc.document_id,
        dc.content,
        dc.headings,
        dc.label,
        dc.page_no,
        dc.metadata,
        1 - (dc.embedding <=> query_embedding) as similarity
    from public.document_chunks dc
    where
        (filter_document_id is null or dc.document_id = filter_document_id)
        and 1 - (dc.embedding <=> query_embedding) > match_threshold
    order by dc.embedding <=> query_embedding
    limit match_count;
end;
$$;

-- ============================================================
-- Trigger: auto-update updated_at
-- ============================================================
create or replace function public.update_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger documents_updated_at
    before update on public.documents
    for each row execute function public.update_updated_at();

-- ============================================================
-- Storage bucket + policies
-- ============================================================
insert into storage.buckets (id, name, public)
values ('documents', 'documents', false)
on conflict (id) do nothing;

create policy "Users can upload to own folder"
    on storage.objects for insert
    with check (
        bucket_id = 'documents'
        and auth.uid()::text = (storage.foldername(name))[1]
    );

create policy "Users can read own files"
    on storage.objects for select
    using (
        bucket_id = 'documents'
        and auth.uid()::text = (storage.foldername(name))[1]
    );

create policy "Users can delete own files"
    on storage.objects for delete
    using (
        bucket_id = 'documents'
        and auth.uid()::text = (storage.foldername(name))[1]
    );
