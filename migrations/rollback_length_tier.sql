-- Rollback Migration: Remove length_tier from sentences table
-- Safe instant rollback: Drops the tier index and the tier column.

DROP INDEX IF EXISTS idx_sentences_tier_unvalidated;
ALTER TABLE sentences DROP COLUMN IF EXISTS length_tier;
