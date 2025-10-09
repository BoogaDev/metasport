alter table if exists public.teams
  add column if not exists api_league_number integer null,
  add column if not exists api_team_number integer null,
  add column if not exists api_team_name text null;

create index if not exists idx_teams_api_team_number on public.teams(api_team_number);

