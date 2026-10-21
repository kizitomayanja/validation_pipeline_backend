"""
scripts/restore_database.py
---------------------------
Applies restore_original_database_scoring.sql directly to DATABASE_URL.
Restores exact pre-migration scoring (6,424 easy, 6,424 medium, 3,213 hard)
and drops the auto-scoring trigger.
"""
import sys
from pathlib import Path
import psycopg

from app.config import get_settings


def main():
    root = Path(__file__).resolve().parent.parent
    sql_file = root / "restore_original_database_scoring.sql"
    if not sql_file.exists():
        sys.exit("Error: restore_original_database_scoring.sql not found.")

    db_url = get_settings().database_url
    masked = db_url.split("@")[-1] if "@" in db_url else db_url
    print(f"Connecting to {masked} to restore original scoring...")

    sql_text = sql_file.read_text(encoding="utf-8")

    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
        conn.commit()

        # Verification query
        cur = conn.cursor()
        cur.execute("""
            select difficulty, count(*), round(avg(cost), 4) as avg_cost
            from sentences
            where validated_by is null
            group by difficulty
            order by case difficulty when 'easy' then 1 when 'medium' then 2 else 3 end;
        """)
        rows = cur.fetchall()
        print("\nRestoration successful! Current unvalidated difficulty distribution in DB:")
        for r in rows:
            print(f"  {r[0]:<8}: {r[1]:>6,} sentences (avg cost: {r[2]})")


if __name__ == "__main__":
    main()
