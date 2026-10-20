"""
scripts/recalculate_scores.py
-----------------------------
Recalculates length_words, cost, difficulty_score, and difficulty
across output/sentences_for_supabase.csv, sentences_for_supabase.csv,
and optionally the live database if DATABASE_URL is set.

Usage:
    python -m scripts.recalculate_scores [--apply-db]
"""
import argparse
import sys
from pathlib import Path
import pandas as pd

from app.scoring import score_sentence


def recalculate_csv(csv_path: Path) -> pd.DataFrame:
    print(f"Reading {csv_path}...")
    df = pd.read_csv(csv_path)
    n = len(df)
    print(f"Recalculating scores for {n:,} sentences...")

    costs, diff_scores, diffs, lengths = [], [], [], []
    for idx, row in df.iterrows():
        raw_cost = row.get("cost_raw", None)
        text = str(row["noisy_english"])
        res = score_sentence(text, cost_raw=raw_cost)
        costs.append(res["cost"])
        diff_scores.append(res["difficulty_score"])
        diffs.append(res["difficulty"])
        lengths.append(res["length_words"])

    df["cost"] = costs
    df["difficulty_score"] = diff_scores
    df["difficulty"] = diffs
    df["length_words"] = lengths

    df.to_csv(csv_path, index=False)
    print(f"Saved {csv_path} successfully.")
    print("New difficulty distribution:\n", df["difficulty"].value_counts().to_string())
    return df


def apply_to_database() -> None:
    import psycopg
    from app.config import get_settings

    db_url = get_settings().database_url
    print(f"Applying score recalculation to database: {db_url.split('@')[-1] if '@' in db_url else db_url}...")
    migration_file = Path(__file__).resolve().parent.parent / "migration_recalculate_difficulty_and_rewards.sql"
    sql = migration_file.read_text(encoding="utf-8")

    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        count = conn.execute("select count(*) from sentences where validated_by is null").fetchone()[0]
        print(f"Successfully updated unvalidated sentences ({count:,} rows) and installed database trigger.")


def main():
    parser = argparse.ArgumentParser(description="Recalculate difficulty and workload scores.")
    parser.add_argument("--apply-db", action="store_true", help="Execute SQL migration on DATABASE_URL")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    csv1 = root / "output" / "sentences_for_supabase.csv"
    csv2 = root / "sentences_for_supabase.csv"

    if csv1.exists():
        recalculate_csv(csv1)
    if csv2.exists():
        recalculate_csv(csv2)

    if args.apply_db:
        apply_to_database()


if __name__ == "__main__":
    main()
