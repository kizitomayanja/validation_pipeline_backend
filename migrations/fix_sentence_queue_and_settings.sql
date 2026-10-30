-- Diagnostic & Recovery Script: Fix Sentence Queue, Reward Settings, & Support Tables
-- Target: Supabase SQL Editor / PostgreSQL
-- Purpose: Ensures reward_settings row exists, clears expired claims, ensures length_tier backfill,
--          and verifies support_tickets & ticket_messages exist for inquiries.

-- 1. Ensure single-row reward_settings configuration exists
INSERT INTO reward_settings (id, rate_ugx, min_reward_ugx, min_cashout_ugx, max_budget_ugx)
VALUES (true, 2000, 100, 5000, NULL)
ON CONFLICT (id) DO UPDATE 
SET rate_ugx = COALESCE(reward_settings.rate_ugx, 2000),
    min_reward_ugx = COALESCE(reward_settings.min_reward_ugx, 100),
    min_cashout_ugx = COALESCE(reward_settings.min_cashout_ugx, 5000);

-- 2. Release any stuck, orphaned, or expired claims from earlier testing
UPDATE sentences 
SET claimed_by = NULL, claim_expires_at = NULL 
WHERE validated_by IS NULL AND (claim_expires_at IS NULL OR claim_expires_at < now());

-- 3. Ensure length_tier is fully backfilled on all sentences (30w / 70w thresholds)
UPDATE sentences
SET length_tier = CASE
    WHEN length_words <= 30 THEN 'short'
    WHEN length_words <= 70 THEN 'medium'
    ELSE 'long'
END
WHERE length_tier IS NULL;

-- 4. Ensure support_tickets & ticket_messages tables exist for the Inquiries page
CREATE TABLE IF NOT EXISTS support_tickets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_num  serial UNIQUE,
  author_id   uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  category    text NOT NULL CHECK (category IN ('clinical_terminology', 'review_dispute', 'payout_issue', 'role_request', 'general')),
  target_role text NOT NULL CHECK (target_role IN ('reviewer', 'admin')),
  subject     text NOT NULL,
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved')),
  sentence_id bigint REFERENCES sentences(id) ON DELETE SET NULL,
  payout_id   uuid REFERENCES payouts(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS support_tickets_author_idx ON support_tickets (author_id);
CREATE INDEX IF NOT EXISTS support_tickets_target_role_idx ON support_tickets (target_role, status);

CREATE TABLE IF NOT EXISTS ticket_messages (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id  uuid NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
  sender_id  uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  message    text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ticket_messages_ticket_idx ON ticket_messages (ticket_id, created_at ASC);

-- 5. Verification: Check unvalidated queue count per tier
SELECT 
    COALESCE(length_tier, 'missing') AS length_tier,
    COUNT(*) AS available_sentences,
    MIN(cost) AS min_cost,
    MAX(cost) AS max_cost
FROM sentences
WHERE validated_by IS NULL
GROUP BY length_tier
ORDER BY length_tier;

-- 6. Verification: Check budget settings
SELECT rate_ugx, min_reward_ugx, min_cashout_ugx, max_budget_ugx, updated_at 
FROM reward_settings;
