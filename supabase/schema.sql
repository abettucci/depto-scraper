create table if not exists public.saved_searches (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80), filters jsonb not null default '{}'::jsonb,
  source_urls jsonb not null, notify_chat_id text check (notify_chat_id ~ '^[0-9]{8,20}$'),
  active boolean not null default true, baseline_completed boolean not null default false,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists public.listing_groups (
  id uuid primary key default gen_random_uuid(), search_id uuid not null references public.saved_searches(id) on delete cascade,
  fingerprint text not null, title text not null, address text, last_price numeric, currency text check (currency in ('ARS', 'USD')),
  covered_m2 numeric, rooms numeric, updated_at timestamptz not null default now(), unique (search_id, fingerprint)
);
create table if not exists public.listing_sources (
  id uuid primary key default gen_random_uuid(), search_id uuid not null references public.saved_searches(id) on delete cascade,
  group_id uuid not null references public.listing_groups(id) on delete cascade, source_key text not null,
  portal text not null check (portal in ('argenprop', 'zonaprop', 'mercadolibre', 'remax')), listing_url text not null,
  active boolean not null default true, first_seen_at timestamptz not null default now(), last_seen_at timestamptz not null default now(), unique (search_id, source_key)
);
create table if not exists public.listing_snapshots (
  id bigint generated always as identity primary key, source_id uuid not null references public.listing_sources(id) on delete cascade,
  price numeric, currency text check (currency in ('ARS', 'USD')), expenses numeric, rooms numeric, covered_m2 numeric,
  raw_title text not null, raw_address text, captured_at timestamptz not null default now()
);
alter table public.saved_searches enable row level security;
alter table public.listing_groups enable row level security;
alter table public.listing_sources enable row level security;
alter table public.listing_snapshots enable row level security;
create policy "users manage their searches" on public.saved_searches for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "users read their groups" on public.listing_groups for select using (exists (select 1 from public.saved_searches s where s.id = search_id and s.user_id = auth.uid()));
create policy "users read their sources" on public.listing_sources for select using (exists (select 1 from public.saved_searches s where s.id = search_id and s.user_id = auth.uid()));
create policy "users read their snapshots" on public.listing_snapshots for select using (exists (select 1 from public.listing_sources ls join public.saved_searches s on s.id = ls.search_id where ls.id = source_id and s.user_id = auth.uid()));
