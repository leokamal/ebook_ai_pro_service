-- Share a PDF by its document id: one stored file and one set of vectors ("content"), several holders.
-- A holder is a `documents` row; (content_owner_id, content_id) = the uploader and the original document id.
-- Apply before deploying the code that reads these columns. Idempotent.

create schema if not exists extensions;
create extension if not exists "uuid-ossp" with schema extensions;

alter table public.documents add column if not exists content_id uuid;
alter table public.documents add column if not exists content_owner_id uuid;   -- no FK: outlives the uploader
alter table public.documents add column if not exists sharing_enabled boolean not null default false;
update public.documents set content_id = id, content_owner_id = user_id where content_id is null;
alter table public.documents alter column content_id set not null;
alter table public.documents alter column content_owner_id set not null;
create index if not exists documents_content_idx on public.documents (content_id);
create index if not exists documents_content_owner_idx on public.documents (content_owner_id);
create unique index if not exists documents_user_content_uidx on public.documents (user_id, content_id);

-- The path now belongs to the content, not to the holder.
alter table public.documents drop constraint if exists documents_check;
alter table public.documents drop constraint if exists documents_content_path_check;
alter table public.documents add constraint documents_content_path_check
  check (file_path = content_owner_id::text || '/' || content_id::text || '/' || file_name);

-- Clients have no column grant on content_*, so their inserts always arrive without them. An id whose file and
-- vectors others still use can never be taken again: a new owner of that id could overwrite those vectors.
create or replace function public.set_document_content()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if new.content_id is null then
    if exists (select 1 from public.documents d where d.content_id = new.id) then
      raise exception 'duplicate document id' using errcode = '23505';
    end if;
    new.content_id := new.id;
    new.content_owner_id := new.user_id;
  end if;
  return new;
end $$;
drop trigger if exists documents_content on public.documents;
create trigger documents_content before insert on public.documents
  for each row execute function public.set_document_content();

-- ------------------------------------------------------------------ RPCs
create or replace function public.share_document(p_document_id uuid)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := (select auth.uid());
  v_doc public.documents%rowtype;
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  select * into v_doc from public.documents where id = p_document_id and user_id = v_uid for update;
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
  if v_doc.content_owner_id <> v_uid or v_doc.content_id <> v_doc.id then   -- only the uploader shares
    raise exception 'forbidden' using errcode = '42501';
  end if;
  if v_doc.status <> 'completed' or v_doc.index_state <> 'indexed' then raise exception 'conflict'; end if;
  update public.documents set sharing_enabled = true where id = v_doc.id;
  return v_doc.id;
end $$;

create or replace function public.stop_sharing_document(p_document_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if (select auth.uid()) is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  update public.documents set sharing_enabled = false where id = p_document_id and user_id = (select auth.uid());
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
end $$;

-- Unknown, not shared and not ready all answer `not_found`, so an id reveals nothing.
create or replace function public.accept_shared_document(p_document_id uuid)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := (select auth.uid());
  v_src public.documents%rowtype;
  v_held public.documents%rowtype;
  v_id uuid := gen_random_uuid();
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('accept:' || v_uid::text, 0));
  -- the uploader's own row, locked: once it is `deleting` or gone, nobody can join the content
  select * into v_src from public.documents
    where id = p_document_id and content_id = id and content_owner_id = user_id for update;
  if not found or v_src.status <> 'completed' or v_src.index_state <> 'indexed'
     or (v_src.user_id <> v_uid and not v_src.sharing_enabled) then
    raise exception 'not_found' using errcode = 'P0002';
  end if;
  select * into v_held from public.documents where user_id = v_uid and content_id = v_src.content_id;
  if found then
    if v_held.status = 'deleting' then raise exception 'conflict'; end if;
    return v_held.id;
  end if;
  insert into public.documents (id, user_id, title, file_name, file_path, file_type, file_size, page_count, language,
                                status, index_state, embedding_model, chunk_count, content_id, content_owner_id)
  values (v_id, v_uid, v_src.title, v_src.file_name, v_src.file_path, v_src.file_type, v_src.file_size,
          v_src.page_count, v_src.language, 'completed', 'indexed', v_src.embedding_model, v_src.chunk_count,
          v_src.content_id, v_src.content_owner_id);
  -- the holder's own citation rows (ids and pages only); ids must equal Python's vector_store.chunk_id
  insert into public.document_chunks (id, document_id, user_id, chunk_index, page_number)
    select extensions.uuid_generate_v5(v_id, c.chunk_index::text), v_id, v_uid, c.chunk_index, c.page_number
    from public.document_chunks c where c.document_id = v_src.id;
  return v_id;
end $$;

revoke all on function public.set_document_content() from public, anon, authenticated;
revoke all on function public.share_document(uuid) from public, anon;
revoke all on function public.stop_sharing_document(uuid) from public, anon;
revoke all on function public.accept_shared_document(uuid) from public, anon;
grant execute on function public.share_document(uuid) to authenticated;
grant execute on function public.stop_sharing_document(uuid) to authenticated;
grant execute on function public.accept_shared_document(uuid) to authenticated;

-- ------------------------------------------------------------------ storage
-- Holders may read the shared file (their own rows only: the subquery runs under their RLS).
drop policy if exists user_documents_select_shared on storage.objects;
create policy user_documents_select_shared on storage.objects for select to authenticated
  using (bucket_id = 'user-documents'
         and exists (select 1 from public.documents d where d.file_path = objects.name));

-- Sees every user's rows, but only answers for the caller's own folder (anything else counts as in use).
create or replace function public.path_in_use(p_path text)
returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce(split_part(p_path, '/', 1) <> (select auth.uid())::text, true)
      or exists (select 1 from public.documents d where d.file_path = p_path)
$$;
revoke all on function public.path_in_use(text) from public, anon;
grant execute on function public.path_in_use(text) to authenticated, service_role;

-- RESTRICTIVE on purpose: it narrows `user_documents_delete`, whose own check only sees the caller's rows, and
-- it stays in force if the first migration is ever rerun.
drop policy if exists user_documents_delete_in_use on storage.objects;
create policy user_documents_delete_in_use on storage.objects as restrictive for delete to authenticated
  using (bucket_id <> 'user-documents' or not public.path_in_use(name));
