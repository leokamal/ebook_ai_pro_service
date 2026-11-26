-- Per-user storage space: limits, upload gate, usage and cleanup helpers. Idempotent.
-- Depends on 20261125000000_pro_schema.sql (never edit that applied migration).

create table if not exists public.app_limits (
  id                boolean primary key default true check (id),          -- exactly one row
  max_storage_bytes bigint  not null default 104857600 check (max_storage_bytes >= 0),  -- 100 MB per user
  max_objects       integer not null default 220 check (max_objects >= 0)
);
insert into public.app_limits (id) values (true) on conflict (id) do nothing;
alter table public.app_limits enable row level security;
alter table public.app_limits force row level security;
revoke all on public.app_limits from anon, authenticated;
grant select on public.app_limits to authenticated;
grant all on public.app_limits to service_role;
drop policy if exists app_limits_select on public.app_limits;
create policy app_limits_select on public.app_limits for select to authenticated using (true);

create index if not exists documents_file_path_idx on public.documents (file_path);

-- Real bytes/objects a user holds in the bucket (what they uploaded, registered or not).
create or replace function public.user_storage_usage(p_user uuid)
returns table (bytes bigint, objects integer)
language sql stable security definer set search_path = '' as $$
  select coalesce(sum(case when (o.metadata->>'size') ~ '^[0-9]+$' then (o.metadata->>'size')::bigint else 0 end), 0)::bigint,
         count(*)::integer
  from storage.objects o
  where o.bucket_id = 'user-documents' and o.name like p_user::text || '/%'
$$;

-- Upload gate used by the storage INSERT policy. No parameter: a user can only ask about themself.
-- Checked BEFORE the upload: one file may overshoot, and PARALLEL uploads (or signed upload URLs minted while
-- under the limit) can exceed it. The API re-checks usage at registration and the janitor removes unregistered files.
create or replace function public.can_upload()
returns boolean
language sql stable security definer set search_path = '' as $$
  select coalesce((
    select u.bytes < l.max_storage_bytes and u.objects < l.max_objects
    from public.app_limits l, public.user_storage_usage((select auth.uid())) u
    where (select auth.uid()) is not null
  ), false)
$$;

-- Backend-only helpers. SECURITY INVOKER on purpose: they run as service_role (which bypasses RLS). If they
-- ever ran as a role that cannot see public.documents, they fail instead of reporting everything as orphaned.
create or replace function public.list_user_objects(p_user uuid)
returns setof text language sql stable set search_path = '' as $$
  select o.name from storage.objects o
  where o.bucket_id = 'user-documents' and o.name like p_user::text || '/%'
  order by o.name
$$;

create or replace function public.list_orphan_objects(p_older_than interval, p_limit integer default 50)
returns setof text language sql stable set search_path = '' as $$
  select o.name from storage.objects o
  where o.bucket_id = 'user-documents'
    and o.created_at < now() - greatest(p_older_than, interval '1 hour')   -- never touch fresh uploads
    and not exists (select 1 from public.documents d where d.file_path = o.name)
  order by o.created_at
  limit least(p_limit, 500)
$$;

revoke all on function public.user_storage_usage(uuid) from public, anon, authenticated;
revoke all on function public.can_upload() from public, anon;
revoke all on function public.list_user_objects(uuid) from public, anon, authenticated;
revoke all on function public.list_orphan_objects(interval, integer) from public, anon, authenticated;
grant execute on function public.user_storage_usage(uuid) to service_role;
grant execute on function public.can_upload() to authenticated, service_role;
grant execute on function public.list_user_objects(uuid) to service_role;
grant execute on function public.list_orphan_objects(interval, integer) to service_role;

-- Same folder rule as before, plus the space limit.
drop policy if exists user_documents_insert on storage.objects;
create policy user_documents_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'user-documents'
              and (storage.foldername(name))[1] = (select auth.uid())::text
              and public.can_upload());

-- A document being deleted must stay `deleting`: redefine the job functions accordingly
-- (same signatures, so the existing service_role-only grants are kept).
create or replace function public.claim_processing_job(p_worker text, p_lease_seconds integer default 300)
returns setof public.processing_jobs language plpgsql security definer set search_path = '' as $$
declare j public.processing_jobs;
begin
  loop
    select * into j from public.processing_jobs
      where (status = 'queued' and run_after <= now()) or (status = 'running' and locked_until < now())
      order by run_after, created_at limit 1 for update skip locked;
    if not found then return; end if;
    if j.attempts >= j.max_attempts then
      update public.processing_jobs set status = 'failed', completed_at = now(), locked_by = null,
        error_message = coalesce(error_message, 'Too many attempts') where id = j.id;
      update public.documents set status = 'failed', error_message = coalesce(j.error_message, 'Too many attempts')
        where id = j.document_id and status <> 'deleting';
      continue;
    end if;
    return query update public.processing_jobs
      set status = 'running', attempts = attempts + 1, locked_by = p_worker, started_at = coalesce(started_at, now()),
          locked_until = now() + make_interval(secs => p_lease_seconds)
      where id = j.id returning *;
    update public.documents set status = 'processing' where id = j.document_id and status <> 'deleting';
    return;
  end loop;
end $$;

create or replace function public.finish_job(p_job_id uuid, p_ok boolean, p_error text default null, p_retry_after_seconds integer default null)
returns void language plpgsql security definer set search_path = '' as $$
declare j public.processing_jobs;
begin
  select * into j from public.processing_jobs where id = p_job_id for update;
  if not found then return; end if;
  if p_ok then
    update public.processing_jobs set status = 'completed', progress = 100, completed_at = now(), locked_by = null where id = p_job_id;
  elsif p_retry_after_seconds is not null and j.attempts < j.max_attempts then
    update public.processing_jobs set status = 'queued', locked_by = null, error_message = p_error,
      run_after = now() + make_interval(secs => p_retry_after_seconds) where id = p_job_id;
  else
    update public.processing_jobs set status = 'failed', completed_at = now(), locked_by = null, error_message = p_error where id = p_job_id;
    update public.documents set status = 'failed', error_message = p_error where id = j.document_id and status <> 'deleting';
  end if;
end $$;
