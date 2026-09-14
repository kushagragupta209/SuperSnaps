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
