-- Expose pgmq.read/delete/archive to PostgREST via SECURITY DEFINER shims in public.
-- Service-role only; anon/authenticated are explicitly revoked.

create or replace function public.pgmq_read(
    queue_name text,
    vt integer,
    qty integer
)
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
    from pgmq.read(queue_name, vt, qty);
$$;

create or replace function public.pgmq_delete(
    queue_name text,
    msg_id bigint
)
returns boolean
language sql
security definer
set search_path = pgmq, public
as $$
    select pgmq.delete(queue_name, msg_id);
$$;

create or replace function public.pgmq_archive(
    queue_name text,
    msg_id bigint
)
returns boolean
language sql
security definer
set search_path = pgmq, public
as $$
    select pgmq.archive(queue_name, msg_id);
$$;

revoke all on function public.pgmq_read(text, integer, integer) from public, anon, authenticated;
revoke all on function public.pgmq_delete(text, bigint)          from public, anon, authenticated;
revoke all on function public.pgmq_archive(text, bigint)         from public, anon, authenticated;

grant execute on function public.pgmq_read(text, integer, integer) to service_role;
grant execute on function public.pgmq_delete(text, bigint)          to service_role;
grant execute on function public.pgmq_archive(text, bigint)         to service_role;
