-- A stable failure code next to the user-safe message, so clients can translate it.
-- Apply before deploying the worker and the app.

alter table public.documents add column if not exists error_code text;
alter table public.processing_jobs add column if not exists error_code text;

create or replace function public.enqueue_processing_job(p_document_id uuid, p_user_id uuid, p_kind text default 'ingest')
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_id uuid;
begin
  perform 1 from public.documents where id = p_document_id and user_id = p_user_id and status <> 'deleting' for update;
  if not found then raise exception 'document_not_found' using errcode = 'P0002'; end if;
  insert into public.processing_jobs (user_id, document_id, kind) values (p_user_id, p_document_id, p_kind)
    returning id into v_id;                       -- unique partial index rejects a second active job
  update public.documents set status = 'queued', error_message = null, error_code = null where id = p_document_id;
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
        error_message = coalesce(error_message, 'Too many attempts'),
        error_code = coalesce(error_code, 'attempts_exhausted') where id = j.id;
      update public.documents set status = 'failed', error_message = coalesce(j.error_message, 'Too many attempts'),
        error_code = coalesce(j.error_code, 'attempts_exhausted')
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

-- Dropped first: a new parameter would otherwise create an ambiguous overload.
drop function if exists public.finish_job(uuid, boolean, text, text, integer);
create or replace function public.finish_job(p_job_id uuid, p_ok boolean, p_worker text, p_error text default null,
  p_retry_after_seconds integer default null, p_error_code text default null)
returns boolean language plpgsql security definer set search_path = '' as $$
declare j public.processing_jobs;
begin
  select * into j from public.processing_jobs
    where id = p_job_id and locked_by = p_worker and status = 'running' for update;
  if not found then return false; end if;
  if p_ok then
    update public.processing_jobs set status = 'completed', progress = 100, completed_at = now(), locked_by = null where id = p_job_id;
  elsif p_retry_after_seconds is not null and j.attempts < j.max_attempts then
    update public.processing_jobs set status = 'queued', locked_by = null, error_message = p_error, error_code = p_error_code,
      run_after = now() + make_interval(secs => p_retry_after_seconds) where id = p_job_id;
  else
    update public.processing_jobs set status = 'failed', completed_at = now(), locked_by = null,
      error_message = p_error, error_code = p_error_code where id = p_job_id;
    update public.documents set status = 'failed', error_message = p_error, error_code = p_error_code
      where id = j.document_id and status <> 'deleting';
  end if;
  return true;
end $$;

revoke all on function public.finish_job(uuid, boolean, text, text, integer, text) from public, anon, authenticated;
grant execute on function public.finish_job(uuid, boolean, text, text, integer, text) to service_role;
