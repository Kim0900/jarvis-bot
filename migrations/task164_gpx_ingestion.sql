-- task#164: GPX automatic ingestion provenance/idempotency.
-- Historical rows remain untouched (NULL provenance). New rows are protected by Drive file_id and exact content SHA-256.

alter table public.gpx_sessions
  add column if not exists source_file_id text,
  add column if not exists source_sha256 text;

create unique index if not exists uq_gpx_sessions_source_file_id
  on public.gpx_sessions(source_file_id)
  where source_file_id is not null;

create unique index if not exists uq_gpx_sessions_source_sha256
  on public.gpx_sessions(source_sha256)
  where source_sha256 is not null;
