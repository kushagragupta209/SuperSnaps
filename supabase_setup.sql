-- Run this once in your Supabase project's SQL editor.
-- This is the durable "global inbox" the Cloudflare Worker writes into
-- the instant a Telegram message arrives, and that Ledger drains
-- whenever it's launched.

create table if not exists pending_expenses (
    id                    bigint generated always as identity primary key,
    chat_id               bigint not null,
    raw_text              text not null,
    telegram_message_id   bigint not null,
    sent_at               timestamptz not null,
    processed             boolean not null default false,
    created_at            timestamptz not null default now()
);

-- Speeds up "give me everything unprocessed, oldest first" (what Ledger asks for).
create index if not exists idx_pending_expenses_unprocessed
    on pending_expenses (processed, sent_at);

-- Row Level Security: locked down by default. Only the service_role key
-- (used server-side by the Worker and by Ledger) can read/write — never
-- expose the anon/public key for this table.
alter table pending_expenses enable row level security;


-- Persistent Sara conversation history for Telegram.
-- One chat can have many turns; keeping only the latest 12 messages in
-- the application prompt prevents the context from growing indefinitely.
create table if not exists telegram_sara_messages (
    id                    bigint generated always as identity primary key,
    chat_id               bigint not null,
    telegram_message_id   bigint,
    role                  text not null check (role in ('user', 'assistant')),
    content               text not null,
    created_at             timestamptz not null default now()
);

create index if not exists idx_telegram_sara_messages_chat
    on telegram_sara_messages (chat_id, id);

alter table telegram_sara_messages enable row level security;


-- Multi-user foundation: application profile linked to Supabase Auth.
-- Financial tables remain untouched in Phase 1 until ownership migration is verified.
create table if not exists profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    email text,
    display_name text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table profiles enable row level security;

do $
begin
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'profiles' and policyname = 'profiles_select_own') then
        create policy profiles_select_own on profiles for select using (auth.uid() = id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'profiles' and policyname = 'profiles_insert_own') then
        create policy profiles_insert_own on profiles for insert with check (auth.uid() = id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'profiles' and policyname = 'profiles_update_own') then
        create policy profiles_update_own on profiles for update using (auth.uid() = id) with check (auth.uid() = id);
    end if;
end $;

create policy if not exists profiles_update_own on profiles
    for update using (auth.uid() = id) with check (auth.uid() = id);
