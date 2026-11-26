-- Per-user storage space: limits, upload gate, usage and cleanup helpers. Idempotent.
-- Depends on 20261125000000_pro_schema.sql (never edit that applied migration).

create table if not exists public.app_limits (
  id                boolean primary key default true check (id),          -- exactly one row
  max_storage_bytes bigint  not null default 104857600 check (max_storage_bytes >= 0),  -- 100 MB per user
  max_objects       integer not null default 220 check (max_objects >= 0),
  max_documents     integer not null default 200 check (max_documents >= 0),
  max_conversations integer not null default 1000 check (max_conversations >= 0)
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

-- Upload only into your own folder, and only while there is space.
drop policy if exists user_documents_insert on storage.objects;
create policy user_documents_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'user-documents'
              and (storage.foldername(name))[1] = (select auth.uid())::text
              and public.can_upload());

-- Row limits, enforced here because clients can reach PostgREST directly. The advisory lock serializes one
-- user's inserts so parallel requests cannot pass the count together. Fails closed without an app_limits row.
create or replace function public.enforce_row_limit() returns trigger
language plpgsql set search_path = '' as $$
declare v_max integer; v_count bigint;
begin
  perform pg_advisory_xact_lock(hashtextextended(tg_table_name || ':' || new.user_id::text, 0));
  select case tg_table_name when 'documents' then l.max_documents else l.max_conversations end
    into v_max from public.app_limits l;
  execute format('select count(*) from public.%I where user_id = $1', tg_table_name) into v_count using new.user_id;
  if v_max is null or v_count >= v_max then raise exception 'quota_exceeded'; end if;
  return new;
end $$;

drop trigger if exists documents_row_limit on public.documents;
create trigger documents_row_limit before insert on public.documents
  for each row execute function public.enforce_row_limit();
drop trigger if exists conversations_row_limit on public.conversations;
create trigger conversations_row_limit before insert on public.conversations
  for each row execute function public.enforce_row_limit();
