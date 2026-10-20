-- ==============================================================================
-- Migration: Recalibrate Translation Difficulty & Word-Count Proportional Rewards
-- ==============================================================================
-- 1. Updates unvalidated sentences (validated_by IS NULL) with balanced scores.
-- 2. Preserves already validated/verified sentences and historical payouts intact.
-- 3. Installs a dynamic auto-scoring trigger for any newly added sentences.
-- ==============================================================================

-- Step 1: Ensure word count and cost_raw are clean for unvalidated sentences
update sentences
   set length_words = coalesce(array_length(regexp_split_to_array(trim(noisy_english), '\s+'), 1), 1),
       cost_raw = coalesce(cost_raw, 0.0)
 where validated_by is null;

-- Step 2: Recalibrate composite difficulty score S in [0.0, 1.0]
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

-- Step 4: Install auto-scoring trigger for all future sentence additions / updates
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
