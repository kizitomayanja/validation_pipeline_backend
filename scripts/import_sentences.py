"""

Load output/sentences_for_supabase.csv into `sentences`.
   python -m scripts.import_sentences ../output/sentences_for_supabase.csv
Refuses to run if the table already has rows
(re-importing would duplicate source_row and fail anyway).

"""
import sys

import psycopg

from app.config import get_settings

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
    with psycopg.connect(get_settings().database_url, prepare_threshold=None) as conn:
        if conn.execute("select count(*) from sentences").fetchone()[0]:
            sys.exit("`sentences` is not empty; nothing imported.")
        with conn.cursor() as cur, open(sys.argv[1], "rb") as f:
            columns = ", ".join(COLS)
            with cur.copy(f"copy sentences ({columns}) from stdin with (format csv, header true)") as copy:
                while chunk := f.read(1 << 16):
                    copy.write(chunk)
        print(conn.execute("select count(*) from sentences").fetchone()[0], "sentences imported.")


if __name__ == "__main__":
    main()
