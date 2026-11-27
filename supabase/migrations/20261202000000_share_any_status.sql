-- Share as soon as a document is imported, not only once it is analysed. Apply after
-- 20261201000000_document_sharing.sql. Idempotent.
--
-- A copy accepted before the analysis ends has no job of its own: it follows the uploader's row (status, then
-- chunk rows) until that row is completed. It can always start its own analysis (`request_processing`).

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
  if v_doc.status = 'deleting' then raise exception 'conflict'; end if;
  update public.documents set sharing_enabled = true where id = v_doc.id;
  return v_doc.id;
end $$;

-- Unknown, not shared and being deleted all answer `not_found`, so an id reveals nothing.
create or replace function public.accept_shared_document(p_document_id uuid)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := (select auth.uid());
  v_src public.documents%rowtype;
  v_held public.documents%rowtype;
  v_id uuid := gen_random_uuid();
  v_ready boolean;
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('accept:' || v_uid::text, 0));
  -- the uploader's own row, locked: once it is `deleting` or gone, nobody can join the content
  select * into v_src from public.documents
    where id = p_document_id and content_id = id and content_owner_id = user_id for update;
  if not found or v_src.status = 'deleting' or (v_src.user_id <> v_uid and not v_src.sharing_enabled) then
    raise exception 'not_found' using errcode = 'P0002';
  end if;
  select * into v_held from public.documents where user_id = v_uid and content_id = v_src.content_id;
  if found then
    if v_held.status = 'deleting' then raise exception 'conflict'; end if;
    return v_held.id;
  end if;
  v_ready := v_src.status = 'completed' and v_src.index_state = 'indexed';
  insert into public.documents (id, user_id, title, file_name, file_path, file_type, file_size, page_count, language,
                                status, index_state, embedding_model, chunk_count, error_message, error_code,
                                content_id, content_owner_id)
  values (v_id, v_uid, v_src.title, v_src.file_name, v_src.file_path, v_src.file_type, v_src.file_size,
          v_src.page_count, v_src.language, v_src.status,
          -- only a ready copy may say `indexed`
          case when v_ready then 'indexed' when v_src.index_state = 'indexed' then 'stale' else v_src.index_state end,
          v_src.embedding_model, v_src.chunk_count, v_src.error_message, v_src.error_code,
          v_src.content_id, v_src.content_owner_id);
  -- the holder's own citation rows (ids and pages only); ids must equal Python's vector_store.chunk_id
  if v_ready then
    insert into public.document_chunks (id, document_id, user_id, chunk_index, page_number)
      select extensions.uuid_generate_v5(v_id, c.chunk_index::text), v_id, v_uid, c.chunk_index, c.page_number
      from public.document_chunks c where c.document_id = v_src.id;
  end if;
  return v_id;
end $$;

-- Copies that are still waiting follow the uploader's row. A copy with its own queued/running job is left alone.
create or replace function public.sync_shared_copies()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  v_copy record;
begin
  if tg_op = 'DELETE' or new.status = 'deleting' then
    -- nobody will finish the analysis for them: fail the waiting ones so they can retry on the shared file
    update public.documents d set status = 'failed', error_message = 'The shared analysis was interrupted',
        error_code = 'source_removed'
      where d.content_id = old.content_id and d.id <> old.id and d.index_state <> 'indexed'
        and d.status in ('queued', 'processing')
        and not exists (select 1 from public.processing_jobs j
                        where j.document_id = d.id and j.status in ('queued', 'running'));
    return null;
  end if;
  -- a completed but unindexed source is being rebuilt: the worker handles that
  if new.status = 'completed' and new.index_state <> 'indexed' then return null; end if;

  for v_copy in
    select d.id, d.user_id from public.documents d
      where d.content_id = new.content_id and d.id <> new.id and d.index_state <> 'indexed'
        and d.status in ('uploaded', 'queued', 'processing', 'failed')
        and not exists (select 1 from public.processing_jobs j
                        where j.document_id = d.id and j.status in ('queued', 'running'))
      for update
  loop
    if new.status = 'completed' then
      insert into public.document_chunks (id, document_id, user_id, chunk_index, page_number)
        select extensions.uuid_generate_v5(v_copy.id, c.chunk_index::text), v_copy.id, v_copy.user_id,
               c.chunk_index, c.page_number
        from public.document_chunks c where c.document_id = new.id
        on conflict (id) do update set page_number = excluded.page_number;
    end if;
    update public.documents set status = new.status,
        index_state = case when new.status = 'completed' then 'indexed' else index_state end,
        page_count = new.page_count, chunk_count = new.chunk_count, embedding_model = new.embedding_model,
        error_message = new.error_message, error_code = new.error_code
      where id = v_copy.id;
  end loop;
  return null;
end $$;

-- Only the uploader's own row (id = content_id) drives copies, so updating a copy never recurses.
drop trigger if exists documents_sync_copies on public.documents;
create trigger documents_sync_copies after update of status, index_state on public.documents
  for each row when (new.id = new.content_id
                     and (old.status is distinct from new.status or old.index_state is distinct from new.index_state))
  execute function public.sync_shared_copies();
drop trigger if exists documents_sync_copies_delete on public.documents;
create trigger documents_sync_copies_delete after delete on public.documents
  for each row when (old.id = old.content_id) execute function public.sync_shared_copies();

revoke all on function public.sync_shared_copies() from public, anon, authenticated;
