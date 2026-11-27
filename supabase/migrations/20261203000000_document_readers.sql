-- The uploader sees who added their shared document (display name and date only) and can remove a reader.
-- Apply after 20261202000000_share_any_status.sql. Idempotent.

create or replace function public.document_readers(p_document_id uuid)
returns table (reader_document_id uuid, display_name text, added_at timestamptz)
language plpgsql stable security definer set search_path = '' as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  perform 1 from public.documents d
    where d.id = p_document_id and d.user_id = v_uid and d.content_id = d.id and d.content_owner_id = v_uid;
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
  -- no user id, e-mail or avatar leaves this function
  return query
    select d.id, p.display_name, d.created_at
    from public.documents d left join public.profiles p on p.id = d.user_id
    where d.content_id = p_document_id and d.user_id <> v_uid and d.status <> 'deleting'
    order by d.created_at desc, d.id;
end $$;

-- The worker then removes the reader's row and conversations; the uploader's file and vectors stay.
create or replace function public.remove_document_reader(p_document_id uuid, p_reader_document_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  perform 1 from public.documents d
    where d.id = p_document_id and d.user_id = v_uid and d.content_id = d.id and d.content_owner_id = v_uid;
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
  update public.documents set status = 'deleting'
    where id = p_reader_document_id and content_id = p_document_id and user_id <> v_uid and status <> 'deleting';
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
end $$;

revoke all on function public.document_readers(uuid) from public, anon;
revoke all on function public.remove_document_reader(uuid, uuid) from public, anon;
grant execute on function public.document_readers(uuid) to authenticated;
grant execute on function public.remove_document_reader(uuid, uuid) to authenticated;
