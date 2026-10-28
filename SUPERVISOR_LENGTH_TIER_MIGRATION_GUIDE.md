# Supervisor Guide: Sentence Length Tier Migration & Verification

## 1. Context & Purpose
Validators often get fatigued when served dense clinical paragraphs back-to-back. To improve validator throughput and retention, we added a **Task Size / Mood Selector** in the validation portal. This allows validators to select:
- **🌟 All Tasks** (Default: Hardest tasks first)
- **⚡ Short Tasks** ($\le 15$ words — Quick validation, low cognitive strain)
- **📄 Standard Tasks** ($16\text{–}40$ words — Standard clinical sentences)
- **📚 Long Tasks** ($> 40$ words — In-depth clinical records, maximum reward)

---

## 2. Invariant & Safety Guarantees (Zero Disruption)
> [!IMPORTANT]
> **Total Campaign Budget & Scoring Invariant:**
> - **Zero changes to `cost`, `difficulty_score`, or `difficulty`**.
> - **Zero changes to budget or reward formulas** (`reward_settings` remains intact).
> - **The queue priority order (`cost DESC`) is completely preserved**: selecting a tier strictly picks the highest-priority/hardest sentence within that chosen tier.

---

## 3. Database Execution Steps (Supabase SQL Editor)

### Step 1: Execute the Migration
In the **Supabase Dashboard -> SQL Editor**, execute the contents of [`migrations/add_length_tier_to_sentences.sql`](./migrations/add_length_tier_to_sentences.sql):

```sql
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

-- Step 3: High-performance partial index for unvalidated queue
CREATE INDEX IF NOT EXISTS idx_sentences_tier_unvalidated 
ON sentences (length_tier, cost DESC) 
WHERE validated_by IS NULL;
```

---

### Step 2: Run Verification Query
Execute [`migrations/verify_length_tier.sql`](./migrations/verify_length_tier.sql) in SQL Editor:

```sql
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

-- 3. Verify that total cost sums remain 100% identical
SELECT 
    difficulty,
    COUNT(*) AS total_sentences,
    ROUND(SUM(cost), 2) AS sum_cost
FROM sentences
GROUP BY difficulty
ORDER BY difficulty;
```

**Expected Results:**
- `total_sentences`: 16,061
- `missing_tier`: 0
- Hard: 3,213 rows (989 short, 1,492 medium, 732 long)
- Medium: 6,424 rows
- Easy: 6,424 rows

---

## 4. Instant Rollback (If Ever Needed)
If you ever need to revert the database change, run [`migrations/rollback_length_tier.sql`](./migrations/rollback_length_tier.sql):

```sql
DROP INDEX IF EXISTS idx_sentences_tier_unvalidated;
ALTER TABLE sentences DROP COLUMN IF EXISTS length_tier;
```
This safely removes the column and index in milliseconds with zero data loss to the rest of the database.
