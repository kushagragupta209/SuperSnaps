
-- ============================================================
-- SuperSnaps / Ledger - Supabase Setup
-- ============================================================
-- Run this entire script in the Supabase SQL Editor.
-- It is safe to run multiple times.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Telegram pending expenses
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pending_expenses (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id             BIGINT NOT NULL,
    raw_text            TEXT NOT NULL,
    telegram_message_id BIGINT NOT NULL,
    sent_at             TIMESTAMPTZ NOT NULL,
    processed           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_expenses_unprocessed
    ON pending_expenses (processed, sent_at);

ALTER TABLE pending_expenses ENABLE ROW LEVEL SECURITY;


-- ------------------------------------------------------------
-- 2. Telegram Sara conversation history
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS telegram_sara_messages (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id               BIGINT NOT NULL,
    telegram_message_id   BIGINT,
    role                  TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content               TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telegram_sara_messages_chat
    ON telegram_sara_messages (chat_id, id);

ALTER TABLE telegram_sara_messages ENABLE ROW LEVEL SECURITY;


-- ------------------------------------------------------------
-- 3. User profiles
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY
        REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;


-- ------------------------------------------------------------
-- 4. Profile RLS policies
-- ------------------------------------------------------------

DO $$
BEGIN

    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'profiles'
          AND policyname = 'profiles_select_own'
    ) THEN

        CREATE POLICY profiles_select_own
            ON profiles
            FOR SELECT
            USING (auth.uid() = id);

    END IF;


    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'profiles'
          AND policyname = 'profiles_insert_own'
    ) THEN

        CREATE POLICY profiles_insert_own
            ON profiles
            FOR INSERT
            WITH CHECK (auth.uid() = id);

    END IF;


    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'profiles'
          AND policyname = 'profiles_update_own'
    ) THEN

        CREATE POLICY profiles_update_own
            ON profiles
            FOR UPDATE
            USING (auth.uid() = id)
            WITH CHECK (auth.uid() = id);

    END IF;

END $$;


-- ------------------------------------------------------------
-- 5. Telegram -> Ledger user mapping
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS telegram_user_links (
    chat_id   BIGINT PRIMARY KEY,
    user_id   UUID NOT NULL
        REFERENCES auth.users(id) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE telegram_user_links ENABLE ROW LEVEL SECURITY;


-- ------------------------------------------------------------
-- 6. Useful indexes
-- ------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_telegram_user_links_user
    ON telegram_user_links (user_id);


-- ============================================================
-- Setup complete
-- ============================================================

