-- Helper function to check if the current user is an admin
create or replace function public.is_admin()
returns boolean
language sql stable security definer
as $$
  select coalesce(
    (auth.jwt() -> 'app_metadata' ->> 'role') = 'admin',
    false
  );
$$;

-- Documents: admins can view all
create policy "Admins can view all documents"
    on public.documents for select using (public.is_admin());

-- Documents: admins can update all
create policy "Admins can update all documents"
    on public.documents for update using (public.is_admin());

-- Documents: admins can delete all
create policy "Admins can delete all documents"
    on public.documents for delete using (public.is_admin());

-- Document chunks: admins can view all
create policy "Admins can view all chunks"
    on public.document_chunks for select using (public.is_admin());

-- Storage: admins can read all files
create policy "Admins can read all files"
    on storage.objects for select using (
        bucket_id = 'documents' and public.is_admin()
    );
