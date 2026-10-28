-- Verification Script: Verify sentence counts and ensure 0 distortion to original dataset
-- Run this in Supabase SQL Editor after running add_length_tier_to_sentences.sql

-- 1. Check breakdown of difficulty and length_tier
SELECT 
    difficulty, 
    length_tier, 
    COUNT(*) AS count, 
    ROUND(MIN(cost), 4) AS min_cost, 
    ROUND(MAX(cost), 4) AS max_cost
FROM sentences
GROUP BY difficulty, length_tier
ORDER BY difficulty, length_tier;

-- 2. Verify total row count and that zero rows are missing a length_tier
SELECT 
    COUNT(*) AS total_sentences,
    COUNT(length_tier) AS sentences_with_tier,
    COUNT(*) - COUNT(length_tier) AS missing_tier
FROM sentences;

-- 3. Verify that campaign budget allocation and cost distribution remain untouched
SELECT 
    difficulty,
    COUNT(*) AS total_sentences,
    ROUND(SUM(cost), 2) AS sum_cost
FROM sentences
GROUP BY difficulty
ORDER BY difficulty;
