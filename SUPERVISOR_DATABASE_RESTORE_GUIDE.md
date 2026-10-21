# Database Rollback & Restoration Guide

**Target Audience:** Supervisor / Database Administrator  
**Database Target:** Production PostgreSQL / Supabase  
**Execution Time:** Under 10 seconds  
**Downtime:** 0 seconds

---

## 1. What This Restoration Does

This script **completely restores your database back to its exact pre-migration state**:
1. **Drops the trigger and scoring function** (`trigger_auto_score_sentence`, `calculate_sentence_scoring`).
2. **Restores exact pre-migration values** (`length_words`, `cost_raw`, `cost`, `difficulty_score`, `difficulty`) for all 16,061 sentences using the exact original authoritative dataset.
3. **Restores original difficulty counts:**
   - **`easy`**: **6,424 sentences**
   - **`medium`**: **6,424 sentences**
   - **`hard`**: **3,213 sentences**
4. **Restores the planned budget allocation:** Reverts the average cost back to `0.1174` and median to `0.0365`, fixing the budget inflation so total fund spending matches your planned financial limits.
5. **Preserves user work:** Any already completed/validated sentences (`WHERE validated_by IS NOT NULL`) and payouts remain 100% untouched.

---

## 2. Step-by-Step Restoration Instructions

### Method A: Via Supabase SQL Editor (Recommended)

1. Open your **Supabase Dashboard**: [https://supabase.com/dashboard](https://supabase.com/dashboard)
2. Select your project and navigate to the **SQL Editor** on the left menu.
3. Open the file [`restore_original_database_scoring.sql`](./restore_original_database_scoring.sql).
4. Copy the entire contents of `restore_original_database_scoring.sql` and paste them into the Supabase SQL Editor.
5. Click **Run**.
6. The script runs inside a single transaction (`BEGIN ... COMMIT`) and will complete in ~3–5 seconds.

---

### Method B: Via CLI (If `DATABASE_URL` is configured in `.env`)

Run this single command from the project root:
```bash
python -m scripts.restore_database
```

---

## 3. Verification Query

Run this query in the SQL Editor immediately after running the restore script to confirm everything is back to normal:

```sql
select difficulty,
       count(*) as sentence_count,
       round(avg(cost), 4) as avg_cost,
       min(cost) as min_cost,
       max(cost) as max_cost
from sentences
where validated_by is null
group by difficulty
order by case difficulty when 'easy' then 1 when 'medium' then 2 else 3 end;
```

### Expected Output:
```
difficulty | sentence_count | avg_cost | min_cost | max_cost
-----------+----------------+----------+----------+---------
easy       |          6,424 |   0.0040 |   0.0000 |   0.0987
medium     |          6,424 |   0.0911 |   0.0000 |   0.3212
hard       |          3,213 |   0.3970 |   0.0000 |   1.0000
```

When you see **6,424 easy**, **6,424 medium**, and **3,213 hard**, the database and budget allocation are 100% restored.
