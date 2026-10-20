"""
Sentence difficulty and reward scoring engine.
Dynamically calibrates:
  - length_words: human translation workload
  - cost_raw: clinical terminology density & rarity
  - cost / difficulty_score: composite score S in [0.0, 1.0]
  - difficulty: 'easy' | 'medium' | 'hard'
  - reward_ugx: fair workload-proportional reward
"""
import json
import math
import re
from pathlib import Path

_TERM_SCORES_CACHE: dict[str, float] | None = None


def get_medical_lexicon() -> dict[str, float]:
    """Load medical terms and their rarity weights from term_scores.json."""
    global _TERM_SCORES_CACHE
    if _TERM_SCORES_CACHE is not None:
        return _TERM_SCORES_CACHE

    json_path = Path(__file__).resolve().parent / "term_scores.json"
    if json_path.exists():
        with open(json_path, mode="r", encoding="utf-8") as f:
            _TERM_SCORES_CACHE = json.load(f)
            return _TERM_SCORES_CACHE

    # Fallback to output directory if running from repo root
    csv_path = Path(__file__).resolve().parent.parent / "output" / "term_scores.csv"
    if csv_path.exists():
        import csv

        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            _TERM_SCORES_CACHE = {
                row["term"].lower(): round(float(row["term_rarity"]), 4) for row in reader
            }
            return _TERM_SCORES_CACHE

    _TERM_SCORES_CACHE = {}
    return _TERM_SCORES_CACHE


def count_words(text: str) -> int:
    """Accurately count words in clinical sentence."""
    if not text:
        return 0
    return len(text.strip().split())


def count_discourse_punct(text: str) -> int:
    """Count sentence terminators and clause punctuation."""
    if not text:
        return 0
    return len(re.findall(r"[\.\?\!;:]", text))


def calculate_cost_raw(text: str) -> float:
    """Calculate raw terminology score by matching medical lexicon."""
    lexicon = get_medical_lexicon()
    if not lexicon or not text:
        return 0.0

    tokens = re.findall(r"\b[a-zA-Z0-9_-]+\b", text.lower())
    total_rarity = 0.0
    for tok in tokens:
        if tok in lexicon:
            total_rarity += lexicon[tok]

    return round(total_rarity, 4)


def score_sentence(noisy_english: str, cost_raw: float | None = None) -> dict:
    """
    Score a sentence dynamically using the balanced composite formula:
      S = 0.55 * workload + 0.35 * terminology + 0.10 * discourse_structure

    Returns:
      {
        'length_words': int,
        'cost_raw': float,
        'cost': float,             # composite score S in [0.0, 1.0]
        'difficulty_score': float, # same as cost
        'difficulty': 'easy' | 'medium' | 'hard'
      }
    """
    text = noisy_english or ""
    words = count_words(text)

    if cost_raw is None:
        cost_raw = calculate_cost_raw(text)
    else:
        cost_raw = round(float(cost_raw), 4)

    punct = count_discourse_punct(text)

    # 1. Workload / word count factor (55% weight):
    # Scales smoothly: 15 words -> 0.17, 50 words -> 0.47, 100 words -> 0.85, 150+ words -> 1.0
    workload = min(1.0, math.pow(words / 120.0, 0.85)) if words > 0 else 0.0

    # 2. Terminology factor (35% weight):
    # Normalized against standard clinical density threshold of 2.5
    term_score = min(1.0, cost_raw / 2.5) if cost_raw > 0 else 0.0

    # 3. Discourse structure factor (10% weight):
    # Multi-sentence paragraphs up to 6 punctuation marks
    punct_score = min(1.0, punct / 6.0) if punct > 0 else 0.0

    # Composite score:
    raw_score = 0.55 * workload + 0.35 * term_score + 0.10 * punct_score
    score = round(max(0.0001, min(1.0, raw_score)), 4)

    # Difficulty categorization:
    # easy: < 0.15 (typically < 15 words)
    # medium: 0.15 <= S < 0.45 (typically 15-40 words or short with complex terms)
    # hard: >= 0.45 (typically > 40 words, dense multi-sentence clinical dialogues)
    if score < 0.15:
        diff = "easy"
    elif score < 0.45:
        diff = "medium"
    else:
        diff = "hard"

    return {
        "length_words": words,
        "cost_raw": cost_raw,
        "cost": score,
        "difficulty_score": score,
        "difficulty": diff,
    }


def calculate_reward(
    score: float,
    rate_ugx: int = 2000,
    min_reward_ugx: int = 100,
    step: int = 10,
) -> int:
    """Calculate the fair UGX reward from the continuous score."""
    raw = score * rate_ugx
    if step > 1:
        rounded = round(raw / step) * step
    else:
        rounded = round(raw)
    return int(max(min_reward_ugx, rounded))
