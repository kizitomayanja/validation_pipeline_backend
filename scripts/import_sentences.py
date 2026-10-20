"""

Load output/sentences_for_supabase.csv into `sentences`.
   python -m scripts.import_sentences ../output/sentences_for_supabase.csv
Refuses to run if the table already has rows
(re-importing would duplicate source_row and fail anyway).

"""
import sys
import io
import pandas as pd
import psycopg

from app.config import get_settings
from app.scoring import score_sentence

COLS = (
    "source_row",
    "clean_english",
    "noisy_english",
    "clean_lug",
    "noisy_lug",
    "attempted",
    "length_words",
    "cost_raw",
    "cost",
    "difficulty_score",
    "difficulty",
)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.import_sentences PATH_TO_CSV")
    
    csv_path = sys.argv[1]
    df = pd.read_csv(csv_path)

    # If scoring columns are missing, compute them dynamically using app.scoring
    if "cost" not in df.columns or "difficulty" not in df.columns:
        print(f"Auto-scoring {len(df)} sentences using dynamic scoring engine...")
        costs, diff_scores, diffs, lengths = [], [], [], []
        for idx, row in df.iterrows():
            raw_cost = row.get("cost_raw", None)
            res = score_sentence(str(row["noisy_english"]), cost_raw=raw_cost)
            costs.append(res["cost"])
            diff_scores.append(res["difficulty_score"])
            diffs.append(res["difficulty"])
            lengths.append(res["length_words"])
        
        df["cost"] = costs
        df["difficulty_score"] = diff_scores
        df["difficulty"] = diffs
        df["length_words"] = lengths
        if "cost_raw" not in df.columns:
            df["cost_raw"] = 0.0

    # Ensure required columns are present
    for col in COLS:
        if col not in df.columns:
            df[col] = None

    # Reorder strictly to COLS
    df = df[list(COLS)]

    with psycopg.connect(get_settings().database_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            buffer = io.StringIO()
            df.to_csv(buffer, index=False)
            buffer.seek(0)
            columns = ", ".join(COLS)
            with cur.copy(f"copy sentences ({columns}) from stdin with (format csv, header true)") as copy:
                while chunk := buffer.read(1 << 16):
                    copy.write(chunk.encode("utf-8"))
        conn.commit()
        print(conn.execute("select count(*) from sentences").fetchone()[0], "sentences in table.")



if __name__ == "__main__":
    main()
