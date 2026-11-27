-- The readers list also shows the reader's account photo, unless they chose the default picture
-- (`profiles.avatar_hidden`). Apply after 20261203000000_document_readers.sql. Idempotent.

-- Dropped first: the returned columns change, which `create or replace` cannot do.
drop function if exists public.document_readers(uuid);
create function public.document_readers(p_document_id uuid)
returns table (reader_document_id uuid, display_name text, avatar_url text, added_at timestamptz)
language plpgsql stable security definer set search_path = '' as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null or not public.is_allowed_user() then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  perform 1 from public.documents d
    where d.id = p_document_id and d.user_id = v_uid and d.content_id = d.id and d.content_owner_id = v_uid;
  if not found then raise exception 'not_found' using errcode = 'P0002'; end if;
  -- no user id or e-mail leaves this function; a hidden picture is never sent
  return query
    select d.id, p.display_name, case when p.avatar_hidden then null else p.avatar_url end, d.created_at
    from public.documents d left join public.profiles p on p.id = d.user_id
    where d.content_id = p_document_id and d.user_id <> v_uid and d.status <> 'deleting'
    order by d.created_at desc, d.id;
end $$;

revoke all on function public.document_readers(uuid) from public, anon;
grant execute on function public.document_readers(uuid) to authenticated;
