-- One profile per user, modelled on the Dama app's profiles. Idempotent.
-- Depends on the two earlier migrations (pro_touch_updated_at, is_allowed_user).
-- Same sign-in model as Dama: guests are local to the app (no anonymous Supabase user); an account exists only
-- after Google/Facebook sign-in, and the other provider can be linked to it.
-- DISPLAY DATA ONLY: never authorize with anything in this table.

create table if not exists public.profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  generated_key uuid not null unique default gen_random_uuid(),   -- per-user identifier, NOT a secret or API key
  -- server-owned, from the sign-in provider
  email         text,                                             -- may be null (Facebook account without e-mail)
  full_name     text check (full_name is null or char_length(full_name) <= 200),
  avatar_url    text check (avatar_url is null or char_length(avatar_url) <= 2048),
  -- editable by the user
  display_name  text not null check (char_length(display_name) between 3 and 20
                                     and display_name = btrim(display_name)
                                     and display_name ~ '^[[:alnum:]_ .-]+$'),   -- same rule as Dama; not unique
  avatar_hidden boolean not null default false,                   -- true: show the default picture
  country       text check (country is null or country ~ '^[A-Z]{2}$'),
  gender        text check (gender is null or gender in ('male', 'female', 'unspecified')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
drop trigger if exists profiles_touch on public.profiles;
create trigger profiles_touch before update on public.profiles
  for each row execute function public.pro_touch_updated_at();

alter table public.profiles enable row level security;
alter table public.profiles force row level security;
revoke all on public.profiles from anon, authenticated;
grant select on public.profiles to authenticated;
grant update (display_name, avatar_hidden, country, gender) on public.profiles to authenticated;
grant all on public.profiles to service_role;
-- No insert/delete for clients: rows come from the triggers below and go with the auth user.
drop policy if exists profiles_select on public.profiles;
create policy profiles_select on public.profiles for select to authenticated
  using (id = (select auth.uid()));
drop policy if exists profiles_update on public.profiles;
create policy profiles_update on public.profiles for update to authenticated
  using (id = (select auth.uid())) with check (id = (select auth.uid()));
drop policy if exists profiles_provider on public.profiles;
create policy profiles_provider on public.profiles as restrictive for all to authenticated
  using ((select public.is_allowed_user())) with check ((select public.is_allowed_user()));

-- Default display name: the provider name cleaned to fit the rule, else "Reader" + part of the id.
create or replace function public.profiles_default_display_name() returns trigger
language plpgsql set search_path = '' as $$
declare
  v_fallback text := 'Reader' || upper(left(replace(new.id::text, '-', ''), 8));
  v_base     text := btrim(left(btrim(regexp_replace(coalesce(new.full_name, ''), '[^[:alnum:]_ .-]', '', 'g')), 20));
begin
  if char_length(v_base) < 3 then v_base := v_fallback; end if;
  if tg_op = 'INSERT' then
    new.display_name := coalesce(new.display_name, v_base);
  elsif old.full_name is null and new.full_name is not null and old.display_name = v_fallback then
    new.display_name := v_base;              -- the name arrived after the account did
  end if;
  return new;
end $$;
drop trigger if exists profiles_default_display_name on public.profiles;
create trigger profiles_default_display_name before insert or update of full_name on public.profiles
  for each row execute function public.profiles_default_display_name();

-- Only https images; anything unusable becomes null so a sign-up can never fail on it.
create or replace function public.safe_avatar_url(p_url text) returns text
language sql immutable set search_path = '' as $$
  select case when p_url ~ '^https://' and char_length(p_url) <= 2048 then p_url end
$$;

-- Creates or completes a profile. As in Dama, provider data only fills EMPTY fields.
create or replace function public.fill_profile(p_id uuid, p_email text, p_data jsonb) returns void
language sql security definer set search_path = '' as $$
  insert into public.profiles as p (id, email, full_name, avatar_url)
  values (p_id, p_email,
          left(nullif(btrim(coalesce(p_data ->> 'full_name', p_data ->> 'name')), ''), 200),
          public.safe_avatar_url(coalesce(p_data ->> 'avatar_url', p_data ->> 'picture')))
  on conflict (id) do update
    set email = coalesce(excluded.email, p.email),
        full_name = coalesce(p.full_name, excluded.full_name),
        avatar_url = coalesce(p.avatar_url, excluded.avatar_url)
$$;

-- Account created, or its e-mail / provider data changed.
create or replace function public.sync_profile() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  perform public.fill_profile(new.id, new.email, new.raw_user_meta_data);
  return new;
end $$;
drop trigger if exists profiles_sync on auth.users;
create trigger profiles_sync after insert or update of email, raw_user_meta_data on auth.users
  for each row execute function public.sync_profile();

-- Linked account: Supabase puts its name and photo on the identity, not on auth.users.
create or replace function public.sync_profile_from_identity() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if new.provider in ('google', 'facebook') then
    perform public.fill_profile(new.user_id, (select u.email from auth.users u where u.id = new.user_id),
                                new.identity_data);
  end if;
  return new;
end $$;
drop trigger if exists profiles_sync_identity on auth.identities;
create trigger profiles_sync_identity after insert on auth.identities
  for each row execute function public.sync_profile_from_identity();

revoke all on function public.profiles_default_display_name() from public, anon, authenticated;
revoke all on function public.safe_avatar_url(text) from public, anon, authenticated;
revoke all on function public.fill_profile(uuid, text, jsonb) from public, anon, authenticated;
revoke all on function public.sync_profile() from public, anon, authenticated;
revoke all on function public.sync_profile_from_identity() from public, anon, authenticated;

-- Accounts that existed before this migration: user data first, then identities (Google first: its photo URL is
-- stable, Facebook's expires).
do $$ declare r record; begin
  for r in select u.id, u.email, u.raw_user_meta_data from auth.users u loop
    perform public.fill_profile(r.id, r.email, r.raw_user_meta_data);
  end loop;
  for r in select i.user_id, u.email, i.identity_data from auth.identities i join auth.users u on u.id = i.user_id
           where i.provider in ('google', 'facebook') order by (i.provider = 'google') desc, i.created_at loop
    perform public.fill_profile(r.user_id, r.email, r.identity_data);
  end loop;
end $$;
