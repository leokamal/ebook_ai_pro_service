-- The app now works on its own rows directly. These functions cover what its column grants cannot do:
-- requesting processing, requesting deletion, reading its usage and asking for account deletion.
-- Processing and vector/file removal are still done by the backend worker (service role).
-- Every function uses auth.uid() only and repeats the provider rule, because `security definer` skips RLS.

create or replace function public.request_processing(p_document_id uuid)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := (select auth.uid());
  v_doc public.documents%rowtype;
  v_limit bigint;
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  -- one request at a time per user, so parallel calls cannot pass the active-job cap
  perform pg_advisory_xact_lock(hashtextextended(v_uid::text, 0));
  select * into v_doc from public.documents where id = p_document_id and user_id = v_uid for update;
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
  if v_doc.status not in ('uploaded', 'failed') then raise exception 'conflict'; end if;
  if not exists (select 1 from storage.objects where bucket_id = 'user-documents' and name = v_doc.file_path) then
    raise exception 'file_not_found';
  end if;
  -- the storage policy is only a pre-upload check: refuse if the OTHER files already fill the space
  select max_storage_bytes into v_limit from public.app_limits limit 1;
  if v_limit > 0 and (select u.bytes from public.user_storage_usage(v_uid) u) - v_doc.file_size >= v_limit then
    raise exception 'quota_exceeded';
  end if;
  if (select count(*) from public.processing_jobs
      where user_id = v_uid and status in ('queued', 'running')) >= 5 then
    raise exception 'quota_exceeded';
  end if;
  return public.enqueue_processing_job(p_document_id, v_uid, 'ingest');
end $$;

-- The worker janitor removes vectors, file and row of every `deleting` document.
create or replace function public.request_document_delete(p_document_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if (select auth.uid()) is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  update public.documents set status = 'deleting' where id = p_document_id and user_id = (select auth.uid());
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
end $$;

create or replace function public.my_usage()
returns json language plpgsql stable security definer set search_path = '' as $$
declare
  v_uid uuid := (select auth.uid());
  v_result json;
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  select json_build_object(
    'documents', (select count(*) from public.documents where user_id = v_uid),
    'storage_bytes', u.bytes,
    'storage_limit_bytes', l.max_storage_bytes,
    'objects', u.objects,
    'object_limit', l.max_objects)
  into v_result
  from public.user_storage_usage(v_uid) u cross join public.app_limits l;
  return v_result;
end $$;

-- Deleting an account needs vectors and files removed first, so the app only records the request.
create table if not exists public.account_deletion_requests (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
alter table public.account_deletion_requests enable row level security;
revoke all on public.account_deletion_requests from anon, authenticated;
grant all on public.account_deletion_requests to service_role;

create or replace function public.request_account_deletion()
returns void language plpgsql security definer set search_path = '' as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  insert into public.account_deletion_requests (user_id) values (v_uid) on conflict (user_id) do nothing;
  update public.documents set status = 'deleting' where user_id = v_uid;
end $$;

revoke all on function public.request_processing(uuid) from public, anon;
revoke all on function public.request_document_delete(uuid) from public, anon;
revoke all on function public.my_usage() from public, anon;
revoke all on function public.request_account_deletion() from public, anon;
grant execute on function public.request_processing(uuid) to authenticated;
grant execute on function public.request_document_delete(uuid) to authenticated;
grant execute on function public.my_usage() to authenticated;
grant execute on function public.request_account_deletion() to authenticated;
