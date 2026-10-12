-- Optional sidecar schema for Meeting Execution Agent.
-- Run in the Supabase SQL editor only when SUPABASE_ENABLED=true.
create extension if not exists vector;

create table if not exists public.rag_chunks (
  id text primary key,
  content text not null,
  embedding vector not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.result_cache (
  cache_key text primary key,
  operation text not null,
  model text not null,
  prompt_version text not null,
  result_json jsonb not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  hit_count integer not null default 0
);

create index if not exists result_cache_expires_at_idx
  on public.result_cache (expires_at);

create or replace function public.match_rag_chunks(
  query_embedding vector,
  match_count integer default 5,
  filter jsonb default '{}'::jsonb
)
returns table (id text, content text, metadata jsonb, similarity double precision)
language sql stable
as $$
  select
    rag_chunks.id,
    rag_chunks.content,
    rag_chunks.metadata,
    1 - (rag_chunks.embedding <=> query_embedding) as similarity
  from public.rag_chunks
  where rag_chunks.metadata @> filter
  order by rag_chunks.embedding <=> query_embedding
  limit match_count;
$$;

-- The backend uses REST with a server-side key. Configure RLS policies for the
-- deployment's threat model before exposing either table to browser clients.
