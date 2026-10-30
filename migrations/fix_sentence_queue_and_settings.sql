-- Diagnostic & Recovery Script: Fix Sentence Queue & Ensure Reward Settings
-- Target: Supabase SQL Editor / PostgreSQL
-- Purpose: Ensures reward_settings row exists, clears any expired/stuck test claims, 
--          and verifies sentence availability across all length tiers.

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

-- 3. Ensure length_tier is fully backfilled on all sentences
UPDATE sentences
SET length_tier = CASE
    WHEN length_words <= 30 THEN 'short'
    WHEN length_words <= 70 THEN 'medium'
    ELSE 'long'
END
WHERE length_tier IS NULL;

-- 4. Verification: Check unvalidated queue count per tier
SELECT 
    COALESCE(length_tier, 'missing') AS length_tier,
    COUNT(*) AS available_sentences,
    MIN(cost) AS min_cost,
    MAX(cost) AS max_cost
FROM sentences
WHERE validated_by IS NULL
GROUP BY length_tier
ORDER BY length_tier;

-- 5. Verification: Check budget settings
SELECT rate_ugx, min_reward_ugx, min_cashout_ugx, max_budget_ugx, updated_at 
FROM reward_settings;
