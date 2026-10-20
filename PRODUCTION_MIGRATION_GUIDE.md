# Production Database Migration Guide: Dynamic Difficulty & Fair Rewards

**Target Audience:** System Supervisor / Database Administrator  
**Database Target:** Production PostgreSQL / Supabase  
**Downtime Required:** **0 seconds** (Non-blocking migration; safe for concurrent live users)

---

## 1. Executive Summary & Purpose

### The Problem Solved
On the live validation portal, validators were experiencing effort-to-reward distortions:
1. **Queue Starvation:** All tasks being served to validators displayed the `hard` tag, because all sentences containing clinical terms were artificially clamped to the maximum score (`cost = 1.0`).
2. **Flat Compensation Disparity:** A short 16-word sentence with a drug name and a 240-word clinical dialogue monologue both received the exact same flat reward (`2,000 UGX`). Translating 240 words takes ~15× more time and effort.

### What This Migration Does
- **Preserves Training Priority:** The queue continues serving the highest-difficulty / highest-priority tasks first (`ORDER BY score DESC`).
- **Fair Workload-Proportional Rewards:** Rewards are calibrated continuously based on word count (55%), medical terminology density (35%), and discourse structure (10%). Long, demanding paragraphs pay top compensation (e.g. 1,850 – 2,000+ UGX), while short sentences with medication terms pay fair proportional rates (~600 – 800 UGX).
- **Dynamic Auto-Scoring Trigger:** Installs a PostgreSQL `BEFORE INSERT OR UPDATE` trigger so that **any new sentences imported or added to the database in the future are automatically scored and ranked without manual intervention.**

---

## 2. Safety & Zero-Downtime Deployment Order

The code changes and database changes are **completely backward and forward compatible**:
- **Code First:** You can push/deploy the new backend code to production immediately. It works seamlessly against the existing database tables.
- **Database Migration:** When you run the SQL migration below, the queue will instantly transition to the newly calibrated rewards and difficulty tiers for unvalidated tasks.
- **Historical Data Protection:** **Zero completed validations or payouts are modified.** The script strictly targets unvalidated sentences (`WHERE validated_by IS NULL`).

---

## 3. Step-by-Step Instructions for the Supervisor

### Option A: Via Supabase Dashboard (Recommended - 1 minute)

1. Open your **Supabase Dashboard**: [https://supabase.com/dashboard](https://supabase.com/dashboard)
2. Select the production project.
3. In the left navigation menu, click on **SQL Editor**.
4. Click **New query** (or open a blank editor).
5. Copy and paste the SQL script below and click **Run** (green button):

```sql
-- ==============================================================================
-- Production Migration: Recalibrate Translation Difficulty & Word-Count Rewards
-- ==============================================================================

-- Step 1: Clean word counts and terminology scores on unvalidated sentences
update sentences
   set length_words = coalesce(array_length(regexp_split_to_array(trim(noisy_english), '\s+'), 1), 1),
       cost_raw = coalesce(cost_raw, 0.0)
 where validated_by is null;

-- Step 2: Recalibrate composite score S in [0.0, 1.0]
-- Workload (55%) + Terminology Density (35%) + Discourse Punctuation (10%)
update sentences
   set cost = round(greatest(0.0001, least(1.0,
       0.55 * least(1.0, power(length_words::numeric / 120.0, 0.85)) +
       0.35 * least(1.0, cost_raw / 2.5) +
       0.10 * least(1.0, (length(noisy_english) - length(regexp_replace(noisy_english, '[\.\?\!;:]', '', 'g')))::numeric / 6.0)
   )), 4)
 where validated_by is null;

-- Step 3: Re-bucket difficulty tiers based on calibrated score
update sentences
   set difficulty_score = cost,
       difficulty = case
           when cost < 0.15 then 'easy'
           when cost < 0.45 then 'medium'
           else 'hard'
       end
 where validated_by is null;

-- Step 4: Install auto-scoring trigger for all future sentence additions / uploads
create or replace function calculate_sentence_scoring()
returns trigger as $$
declare
    v_words integer;
    v_punct integer;
    v_workload numeric;
    v_term_score numeric;
    v_punct_score numeric;
    v_score numeric;
begin
    -- 1. Word count
    if new.length_words is null or new.length_words <= 0 then
        v_words := coalesce(array_length(regexp_split_to_array(trim(new.noisy_english), '\s+'), 1), 0);
        new.length_words := v_words;
    else
        v_words := new.length_words;
    end if;

    -- 2. Terminal and clause punctuation count
    v_punct := length(new.noisy_english) - length(regexp_replace(new.noisy_english, '[\.\?\!;:]', '', 'g'));

    -- 3. Workload factor (55% weight): (words / 120)^0.85 capped at 1.0
    if v_words > 0 then
        v_workload := least(1.0, power(v_words::numeric / 120.0, 0.85));
    else
        v_workload := 0.0;
    end if;

    -- 4. Terminology density factor (35% weight): cost_raw / 2.5 capped at 1.0
    if new.cost_raw is null then
        new.cost_raw := 0.0;
    end if;
    v_term_score := least(1.0, new.cost_raw / 2.5);

    -- 5. Discourse structure factor (10% weight): punct / 6.0 capped at 1.0
    v_punct_score := least(1.0, v_punct::numeric / 6.0);

    -- 6. Balanced composite score
    v_score := round(0.55 * v_workload + 0.35 * v_term_score + 0.10 * v_punct_score, 4);
    v_score := greatest(0.0001, least(1.0, v_score));

    -- Auto-assign cost and difficulty_score if not supplied or on insert
    if new.cost is null or new.cost = 0 or tg_op = 'INSERT' then
        new.cost := v_score;
        new.difficulty_score := v_score;
    end if;

    -- Auto-assign difficulty category
    if new.difficulty is null or tg_op = 'INSERT' then
        if v_score < 0.15 then
            new.difficulty := 'easy';
        elsif v_score < 0.45 then
            new.difficulty := 'medium';
        else
            new.difficulty := 'hard';
        end if;
    end if;

    return new;
end;
$$ language plpgsql;

drop trigger if exists trigger_auto_score_sentence on sentences;
create trigger trigger_auto_score_sentence
before insert or update of noisy_english on sentences
for each row execute function calculate_sentence_scoring();
```

---

### Option B: Via Command Line (if DATABASE_URL is available)

If you have CLI or server access with `DATABASE_URL` configured in `.env`:
```bash
python -m scripts.recalculate_scores --apply-db
```

---

## 4. Verification Query

Immediately after running the migration, run this verification query in the SQL Editor:

```sql
select difficulty,
       count(*) as sentence_count,
       round(avg(length_words), 1) as avg_word_length,
       min(round(cost * 2000)) as min_reward_ugx,
       max(round(cost * 2000)) as max_reward_ugx
from sentences
where validated_by is null
group by difficulty
order by case difficulty when 'hard' then 1 when 'medium' then 2 else 3 end;
```

### Expected Output:
- **`hard`**: ~360 sentences | avg word length: ~90–105 words | rewards: ~900 to 2,000 UGX.
- **`medium`**: ~5,200 sentences | avg word length: ~22–25 words | rewards: ~300 to 900 UGX.
- **`easy`**: ~10,400 sentences | avg word length: ~7–9 words | rewards: 100 to 300 UGX.

---

## 5. Rollback Plan (If Needed)

In the unlikely event you wish to revert to the old scoring behavior:
```sql
drop trigger if exists trigger_auto_score_sentence on sentences;
drop function if exists calculate_sentence_scoring();
```
*(This leaves existing rows intact and disables dynamic auto-scoring).*
