-- E-Book AI Pro, Phase 2: per-user documents, conversations, messages, citations, jobs.
-- No embeddings here (ChromaDB is the only vector store). Idempotent where practical.
-- Roles: anon = nothing; authenticated = RLS + column grants; service_role = backend/worker.

create extension if not exists pgcrypto;

create or replace function public.pro_touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin new.updated_at = now(); return new; end $$;

-- ------------------------------------------------------------------ documents
create table if not exists public.documents (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  title         text not null check (char_length(title) between 1 and 300),
  file_name     text not null check (char_length(file_name) between 1 and 255
                                      and file_name !~ '[/\\]' and file_name !~ '[[:cntrl:]]' and file_name <> '..'),
  file_path     text not null,
  file_type     text not null default 'application/pdf' check (file_type = 'application/pdf'),
  file_size     bigint not null check (file_size > 0 and file_size <= 52428800),
  page_count    integer check (page_count is null or page_count >= 0),
  language      text check (language is null or char_length(language) <= 16),
  status        text not null default 'uploaded'
                check (status in ('uploaded','queued','processing','completed','failed','deleting')),
  index_state   text not null default 'none' check (index_state in ('none','indexed','missing','stale')),
  embedding_model text,
  chunk_count   integer not null default 0 check (chunk_count >= 0),
  error_message text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (user_id, file_path),
  unique (id, user_id),
  check (file_path = user_id::text || '/' || id::text || '/' || file_name)
);
create index if not exists documents_user_created_idx on public.documents (user_id, created_at desc, id);
create index if not exists documents_status_idx on public.documents (status);
drop trigger if exists documents_touch on public.documents;
create trigger documents_touch before update on public.documents
  for each row execute function public.pro_touch_updated_at();

-- ------------------------------------------------------------------ conversations
create table if not exists public.conversations (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  title       text not null default 'New conversation' check (char_length(title) between 1 and 200),
  mode        text not null check (mode in ('single_document','all_documents')),
  document_id uuid,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (id, user_id),
  check ((mode = 'single_document') = (document_id is not null)),
  -- a conversation can only point at a document owned by the same user
  foreign key (document_id, user_id) references public.documents (id, user_id) on delete cascade
);
create index if not exists conversations_user_updated_idx on public.conversations (user_id, updated_at desc, id);
create index if not exists conversations_document_idx on public.conversations (document_id);
drop trigger if exists conversations_touch on public.conversations;
create trigger conversations_touch before update on public.conversations
  for each row execute function public.pro_touch_updated_at();

-- ------------------------------------------------------------------ messages
create table if not exists public.messages (
  id              uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  role            text not null check (role in ('user','assistant','system')),
  content         text not null check (char_length(content) > 0),
  token_count     integer not null default 0 check (token_count >= 0),
  created_at      timestamptz not null default now()
);
create index if not exists messages_conv_created_idx on public.messages (conversation_id, created_at, id);

-- ------------------------------------------------------------------ document_chunks (no text, no vectors)
create table if not exists public.document_chunks (
  id           uuid primary key,                -- equals the Chroma id: uuid5(document_id, chunk_index)
  document_id  uuid not null references public.documents(id) on delete cascade,
  user_id      uuid not null references auth.users(id) on delete cascade,
  chunk_index  integer not null check (chunk_index >= 0),
  page_number  integer not null check (page_number >= 1),
  created_at   timestamptz not null default now(),
  unique (document_id, chunk_index),
  unique (id, document_id),
  foreign key (document_id, user_id) references public.documents (id, user_id) on delete cascade
);
create index if not exists document_chunks_user_idx on public.document_chunks (user_id);

-- ------------------------------------------------------------------ message_sources
create table if not exists public.message_sources (
  id              uuid primary key default gen_random_uuid(),
  message_id      uuid not null references public.messages(id) on delete cascade,
  document_id     uuid,
  chunk_id        uuid,
  page_number     integer not null check (page_number >= 1),
  relevance_score double precision,
  document_title  text,                         -- snapshot: survives document deletion
  created_at      timestamptz not null default now(),
  unique (message_id, chunk_id),
  -- chunk must belong to the stated document; history survives deletion/reindex (PG15+)
  foreign key (chunk_id, document_id) references public.document_chunks (id, document_id)
    on delete set null (chunk_id)
);
create index if not exists message_sources_message_idx on public.message_sources (message_id);
create index if not exists message_sources_document_idx on public.message_sources (document_id);

-- ------------------------------------------------------------------ processing_jobs (transactional outbox)
create table if not exists public.processing_jobs (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  document_id   uuid not null,
  kind          text not null default 'ingest' check (kind in ('ingest','reindex','delete_cleanup')),
  status        text not null default 'queued' check (status in ('queued','running','completed','failed')),
  attempts      integer not null default 0 check (attempts >= 0),
  max_attempts  integer not null default 3 check (max_attempts >= 1),
  progress      integer not null default 0 check (progress between 0 and 100),
  run_after     timestamptz not null default now(),
  locked_by     text,
  locked_until  timestamptz,
  error_message text,
  started_at    timestamptz,
  completed_at  timestamptz,
  created_at    timestamptz not null default now(),
  foreign key (document_id, user_id) references public.documents (id, user_id) on delete cascade
);
-- duplicate prevention: at most one active job per document
create unique index if not exists processing_jobs_active_uidx on public.processing_jobs (document_id)
  where status in ('queued','running');
create index if not exists processing_jobs_claim_idx on public.processing_jobs (status, run_after);
create index if not exists processing_jobs_user_idx on public.processing_jobs (user_id, created_at desc);

-- ------------------------------------------------------------------ RLS + privileges
do $$ declare t text; begin
  foreach t in array array['documents','conversations','messages','document_chunks','message_sources','processing_jobs'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
    execute format('grant all on public.%I to service_role', t);
    execute format('grant select on public.%I to authenticated', t);
  end loop;
end $$;

grant insert (id, user_id, title, file_name, file_path, file_type, file_size, language)
  on public.documents to authenticated;
grant update (title) on public.documents to authenticated;
grant insert (id, user_id, title, mode, document_id) on public.conversations to authenticated;
grant update (title) on public.conversations to authenticated;
grant delete on public.conversations to authenticated;
-- messages / message_sources / document_chunks / processing_jobs: select only; writes are backend-only.

drop policy if exists documents_select on public.documents;
create policy documents_select on public.documents for select to authenticated
  using (user_id = (select auth.uid()));
drop policy if exists documents_insert on public.documents;
create policy documents_insert on public.documents for insert to authenticated
  with check (user_id = (select auth.uid()) and status = 'uploaded');
drop policy if exists documents_update on public.documents;
create policy documents_update on public.documents for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

drop policy if exists conversations_select on public.conversations;
create policy conversations_select on public.conversations for select to authenticated
  using (user_id = (select auth.uid()));
drop policy if exists conversations_insert on public.conversations;
create policy conversations_insert on public.conversations for insert to authenticated
  with check (user_id = (select auth.uid()));
drop policy if exists conversations_update on public.conversations;
create policy conversations_update on public.conversations for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists conversations_delete on public.conversations;
create policy conversations_delete on public.conversations for delete to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists messages_select on public.messages;
create policy messages_select on public.messages for select to authenticated
  using (exists (select 1 from public.conversations c
                 where c.id = messages.conversation_id and c.user_id = (select auth.uid())));

drop policy if exists message_sources_select on public.message_sources;
create policy message_sources_select on public.message_sources for select to authenticated
  using (exists (select 1 from public.messages m join public.conversations c on c.id = m.conversation_id
                 where m.id = message_sources.message_id and c.user_id = (select auth.uid())));

drop policy if exists document_chunks_select on public.document_chunks;
create policy document_chunks_select on public.document_chunks for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists processing_jobs_select on public.processing_jobs;
create policy processing_jobs_select on public.processing_jobs for select to authenticated
  using (user_id = (select auth.uid()));

-- ------------------------------------------------------------------ storage: private bucket + policies
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('user-documents', 'user-documents', false, 52428800, array['application/pdf'])
on conflict (id) do update set public = false, file_size_limit = 52428800,
  allowed_mime_types = array['application/pdf'];

-- Path: {user_id}/{document_id}/{filename}; first folder must equal the caller's uuid.
do $$ declare op text; begin
  foreach op in array array['select','insert','update','delete'] loop
    execute format('drop policy if exists %I on storage.objects', 'user_documents_' || op);
  end loop;
end $$;
create policy user_documents_select on storage.objects for select to authenticated
  using (bucket_id = 'user-documents' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy user_documents_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'user-documents' and (storage.foldername(name))[1] = (select auth.uid())::text);
-- No UPDATE policy on purpose: objects are immutable once uploaded (clients upload with upsert=false),
-- so an indexed PDF cannot be swapped under the Chroma index.
create policy user_documents_delete on storage.objects for delete to authenticated
  using (bucket_id = 'user-documents' and (storage.foldername(name))[1] = (select auth.uid())::text);

-- ------------------------------------------------------------------ job RPCs (service_role only)
create or replace function public.enqueue_processing_job(p_document_id uuid, p_user_id uuid, p_kind text default 'ingest')
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_id uuid;
begin
  perform 1 from public.documents where id = p_document_id and user_id = p_user_id and status <> 'deleting' for update;
  if not found then raise exception 'document_not_found' using errcode = 'P0002'; end if;
  insert into public.processing_jobs (user_id, document_id, kind) values (p_user_id, p_document_id, p_kind)
    returning id into v_id;                       -- unique partial index rejects a second active job
  update public.documents set status = 'queued', error_message = null where id = p_document_id;
  return v_id;
end $$;

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
        where id = j.document_id;
      continue;
    end if;
    return query update public.processing_jobs
      set status = 'running', attempts = attempts + 1, locked_by = p_worker, started_at = coalesce(started_at, now()),
          locked_until = now() + make_interval(secs => p_lease_seconds)
      where id = j.id returning *;
    update public.documents set status = 'processing' where id = j.document_id;
    return;
  end loop;
end $$;

create or replace function public.heartbeat_job(p_job_id uuid, p_worker text, p_progress integer, p_lease_seconds integer default 300)
returns boolean language sql security definer set search_path = '' as $$
  with u as (update public.processing_jobs
    set locked_until = now() + make_interval(secs => p_lease_seconds), progress = greatest(0, least(100, p_progress))
    where id = p_job_id and locked_by = p_worker and status = 'running' returning 1)
  select exists (select 1 from u) $$;

-- p_retry_after_seconds: null = permanent failure; >0 = requeue with backoff (if attempts remain)
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
    update public.documents set status = 'failed', error_message = p_error where id = j.document_id;
  end if;
end $$;

revoke all on function public.enqueue_processing_job(uuid, uuid, text) from public, anon, authenticated;
revoke all on function public.claim_processing_job(text, integer) from public, anon, authenticated;
revoke all on function public.heartbeat_job(uuid, text, integer, integer) from public, anon, authenticated;
revoke all on function public.finish_job(uuid, boolean, text, integer) from public, anon, authenticated;
grant execute on function public.enqueue_processing_job(uuid, uuid, text) to service_role;
grant execute on function public.claim_processing_job(text, integer) to service_role;
grant execute on function public.heartbeat_job(uuid, text, integer, integer) to service_role;
grant execute on function public.finish_job(uuid, boolean, text, integer) to service_role;
