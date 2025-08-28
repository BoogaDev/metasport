-- External references to persist API-Sports IDs for games and teams
create table IF not exists public.external_refs (
  id uuid primary key default gen_random_uuid(),
  entity_type text not null check (entity_type in ('game','team')),
  entity_id uuid not null,
  provider text not null default 'api-sports',
  provider_key text not null,
  created_at timestamptz not null default now(),
  unique (entity_type, provider, provider_key)
);

create index IF not exists idx_external_refs_entity on public.external_refs(entity_type, entity_id);

