-- Web trial: per-IP and global daily quotas plus funnel events.
-- Only the server (service_role via the secret key) may touch these objects.

create table if not exists public.trial_quota (
  day      date not null,
  subject  text not null,
  used     integer not null default 0 check (used >= 0),
  primary key (day, subject)
);

create table if not exists public.trial_events (
  id             bigint generated always as identity primary key,
  created_at     timestamptz not null default now(),
  kind           text not null check (kind in ('parse', 'popup')),
  status         integer,
  reject_code    text,
  feature        text,
  action         text check (action is null or action in ('open', 'inquiry')),
  source_pages   smallint,
  pages          smallint,
  problems       smallint,
  risk_problems  smallint,
  bytes          integer,
  elapsed_ms     integer,
  ip_hash        text
);

create index if not exists trial_events_created_at_idx on public.trial_events (created_at);

alter table public.trial_quota enable row level security;
alter table public.trial_events enable row level security;

create or replace function public.trial_consume(p_day date, p_subject text, p_limit integer, p_global_limit integer)
returns table (allowed boolean, remaining integer, reason text)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_global_used integer;
  v_used integer;
begin
  insert into public.trial_quota (day, subject, used)
  values (p_day, '__global__', 0), (p_day, p_subject, 0)
  on conflict (day, subject) do nothing;

  -- Lock the global row first so every consumer serializes on the same row.
  select q.used into v_global_used
  from public.trial_quota q
  where q.day = p_day and q.subject = '__global__'
  for update;

  select q.used into v_used
  from public.trial_quota q
  where q.day = p_day and q.subject = p_subject
  for update;

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

  return query select true, p_limit - v_used - 1, null::text;
end;
$$;

create or replace function public.trial_refund(p_day date, p_subject text)
returns void
language sql
security invoker
set search_path = ''
as $$
  update public.trial_quota q
  set used = greatest(q.used - 1, 0)
  where q.day = p_day and q.subject in (p_subject, '__global__');
$$;

create or replace function public.trial_cleanup(p_today date)
returns void
language sql
security invoker
set search_path = ''
as $$
  delete from public.trial_quota q where q.day < p_today - 7;
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

revoke all on table public.trial_quota, public.trial_events from public, anon, authenticated;
revoke all on public.trial_weekly_funnel from public, anon, authenticated;
revoke all on function public.trial_consume(date, text, integer, integer) from public, anon, authenticated;
revoke all on function public.trial_refund(date, text) from public, anon, authenticated;
revoke all on function public.trial_cleanup(date) from public, anon, authenticated;

grant select, insert, update, delete on table public.trial_quota to service_role;
grant select, insert, delete on table public.trial_events to service_role;
grant select on public.trial_weekly_funnel to service_role;
grant execute on function public.trial_consume(date, text, integer, integer) to service_role;
grant execute on function public.trial_refund(date, text) to service_role;
grant execute on function public.trial_cleanup(date) to service_role;
