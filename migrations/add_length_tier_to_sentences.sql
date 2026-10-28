-- Migration: Add length_tier to sentences table
-- Description: Non-destructively classifies sentences into 'short', 'medium', 'long' tiers based on length_words.
-- Preserves all existing difficulty rankings, cost fields, and budget allocations.

-- Step 1: Add length_tier column with check constraint
ALTER TABLE sentences 
ADD COLUMN IF NOT EXISTS length_tier text 
CHECK (length_tier IN ('short', 'medium', 'long'));

-- Step 2: Backfill existing sentences using length_words
-- short: <= 15 words
-- medium: 16-40 words
-- long: > 40 words
UPDATE sentences
SET length_tier = CASE
    WHEN length_words <= 15 THEN 'short'
    WHEN length_words <= 40 THEN 'medium'
    ELSE 'long'
END
WHERE length_tier IS NULL;

-- Step 3: Partial index for fast unvalidated queue retrieval by tier
CREATE INDEX IF NOT EXISTS idx_sentences_tier_unvalidated 
ON sentences (length_tier, cost DESC) 
WHERE validated_by IS NULL;
