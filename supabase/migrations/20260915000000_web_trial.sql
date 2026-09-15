-- Web trial: per-IP and global daily quotas plus funnel events.
-- Only the server (service_role via the secret key) may touch these objects.
--
-- Lock order everywhere: the '__global__' quota row, then the subject quota
-- row, then the charge row. Keeping one order is what prevents deadlocks
-- between concurrent consumes and refunds.

create table if not exists public.trial_quota (
  day      date not null,
  subject  text not null,
  used     integer not null default 0 check (used >= 0),
  primary key (day, subject)
);

-- One row per charged parse, so a refund can only undo a charge that happened,
-- exactly once, even when the client never learned whether consume committed.
create table if not exists public.trial_charges (
  request_id  uuid primary key,
  day         date not null,
  subject     text not null,
  refunded    boolean not null default false,
  created_at  timestamptz not null default now()
);

create table if not exists public.trial_events (
  id             bigint generated always as identity primary key,
  created_at     timestamptz not null default now(),
  kind           text not null check (kind in ('parse', 'popup')),
  status         integer,
  reject_code    text,
  feature        text,
  action         text check (action is null or action in ('open', 'inquiry')),
  source_pages   integer,
  pages          integer,
  problems       integer,
  risk_problems  integer,
  bytes          integer,
  elapsed_ms     integer,
  ip_hash        text
);

-- Earlier drafts used smallint counts; widen them when re-running on such a table.
alter table public.trial_events
  alter column source_pages type integer,
  alter column pages type integer,
  alter column problems type integer,
  alter column risk_problems type integer;

-- 2026-09-15 quality/load design: stage timings, instance id, busy reason, page complexity.
alter table public.trial_events
  add column if not exists timing jsonb,
  add column if not exists instance_id text,
  add column if not exists reject_detail text,
  add column if not exists complexity jsonb;

create index if not exists trial_events_created_at_idx on public.trial_events (created_at);
create index if not exists trial_charges_day_idx on public.trial_charges (day);

alter table public.trial_quota enable row level security;
alter table public.trial_charges enable row level security;
alter table public.trial_events enable row level security;

-- Earlier signatures without request ids.
drop function if exists public.trial_consume(date, text, integer, integer);
drop function if exists public.trial_refund(date, text);

create or replace function public.trial_consume(
  p_request_id uuid,
  p_day date,
  p_subject text,
  p_limit integer,
  p_global_limit integer
)
returns table (allowed boolean, remaining integer, reason text)
language plpgsql
security invoker
set search_path = ''
-- Fail fast instead of committing after the 5 s HTTP client has given up.
set lock_timeout = '3s'
as $$
declare
  v_global_used integer;
  v_used integer;
begin
  insert into public.trial_quota (day, subject, used)
  values (p_day, '__global__', 0), (p_day, p_subject, 0)
  on conflict (day, subject) do nothing;

  select q.used into v_global_used
  from public.trial_quota q
  where q.day = p_day and q.subject = '__global__'
  for update;

  select q.used into v_used
  from public.trial_quota q
  where q.day = p_day and q.subject = p_subject
  for update;

  if exists (select 1 from public.trial_charges c where c.request_id = p_request_id) then
    return query select true, greatest(p_limit - v_used, 0), 'duplicate'::text;
    return;
  end if;
  if v_global_used >= p_global_limit then
    return query select false, 0, 'global'::text;
    return;
  end if;
  if v_used >= p_limit then
    return query select false, 0, 'ip'::text;
    return;
  end if;

  update public.trial_quota q
  set used = q.used + 1
  where q.day = p_day and q.subject in (p_subject, '__global__');

  insert into public.trial_charges (request_id, day, subject)
  values (p_request_id, p_day, p_subject);

  return query select true, p_limit - v_used - 1, null::text;
end;
$$;

create or replace function public.trial_refund(p_request_id uuid)
returns boolean
language plpgsql
security invoker
set search_path = ''
set lock_timeout = '3s'
as $$
declare
  v_day date;
  v_subject text;
begin
  -- Read without locking so the quota rows can be locked first, in the same
  -- order trial_consume uses.
  select c.day, c.subject into v_day, v_subject
  from public.trial_charges c
  where c.request_id = p_request_id and not c.refunded;
  if not found then
    return false;
  end if;

  perform 1 from public.trial_quota q
  where q.day = v_day and q.subject = '__global__'
  for update;

  perform 1 from public.trial_quota q
  where q.day = v_day and q.subject = v_subject
  for update;

  update public.trial_charges c
  set refunded = true
  where c.request_id = p_request_id and not c.refunded;
  if not found then
    -- A concurrent refund of the same request won the race.
    return false;
  end if;

  update public.trial_quota q
  set used = greatest(q.used - 1, 0)
  where q.day = v_day and q.subject in (v_subject, '__global__');
  return true;
end;
$$;

create or replace function public.trial_cleanup(p_today date)
returns void
language sql
security invoker
set search_path = ''
as $$
  delete from public.trial_quota q where q.day < p_today - 7;
  delete from public.trial_charges c where c.day < p_today - 7;
  delete from public.trial_events e where e.created_at < (p_today - 180)::timestamptz;
$$;

create or replace view public.trial_weekly_funnel
with (security_invoker = true)
as
select
  date_trunc('week', e.created_at) as week,
  count(*) filter (where e.kind = 'parse') as parses,
  count(*) filter (where e.kind = 'parse' and e.status = 200) as parses_ok,
  count(*) filter (where e.kind = 'parse' and e.reject_code = 'no_text_layer') as rejected_scan,
  count(*) filter (where e.kind = 'parse' and e.reject_code = 'daily_limit') as rejected_daily,
  count(*) filter (where e.kind = 'popup' and e.action = 'open') as popup_opens,
  count(*) filter (where e.kind = 'popup' and e.action = 'inquiry') as inquiries
from public.trial_events e
group by 1;

revoke all on table public.trial_quota, public.trial_charges, public.trial_events from public, anon, authenticated;
revoke all on public.trial_weekly_funnel from public, anon, authenticated;
revoke all on function public.trial_consume(uuid, date, text, integer, integer) from public, anon, authenticated;
revoke all on function public.trial_refund(uuid) from public, anon, authenticated;
revoke all on function public.trial_cleanup(date) from public, anon, authenticated;

grant select, insert, update, delete on table public.trial_quota to service_role;
grant select, insert, update, delete on table public.trial_charges to service_role;
grant select, insert, delete on table public.trial_events to service_role;
grant select on public.trial_weekly_funnel to service_role;
grant execute on function public.trial_consume(uuid, date, text, integer, integer) to service_role;
grant execute on function public.trial_refund(uuid) to service_role;
grant execute on function public.trial_cleanup(date) to service_role;
