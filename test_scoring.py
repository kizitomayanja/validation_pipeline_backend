import pytest
from app.scoring import (
    calculate_cost_raw,
    calculate_reward,
    count_discourse_punct,
    count_words,
    score_sentence,
)


def test_count_words():
    assert count_words("Take two tablets once daily.") == 5
    assert count_words("   ") == 0
    assert count_words("") == 0
    assert count_words("Do you experience shortness of breath?") == 6


def test_count_discourse_punct():
    assert count_discourse_punct("Take two tablets. Once daily.") == 2
    assert count_discourse_punct("What is your name? Are you dizzy? Yes; no.") == 4
    assert count_discourse_punct("Simple phrase") == 0


def test_cost_raw_medical_lexicon():
    # Terms known to be in lexicon: inhaler, statin, ramipril, orthopnea
    cost = calculate_cost_raw("The patient has orthopnea and is on ramipril and a statin.")
    assert cost > 1.0


def test_short_sentence_is_easy():
    # Simple routine sentence without rare clinical terms
    res = score_sentence("Are you currently taking any vitamins?")
    assert res["length_words"] == 6
    assert res["difficulty"] == "easy"
    assert res["cost"] < 0.15

    reward = calculate_reward(res["cost"], rate_ugx=2000, min_reward_ugx=100)
    assert reward >= 100
    assert reward < 400


def test_short_medication_sentence_differentiated_to_medium():
    # 16-word sentence with ACE inhibitor & statin
    res = score_sentence("Yeah, I take uhm, an ACE inhibitor, statin, uhm, and an ASA.")
    assert res["length_words"] == 12
    assert res["cost_raw"] > 0.5
    # Must NOT be clamped to 1.0 or hard!
    assert res["difficulty"] == "medium"
    assert 0.15 <= res["cost"] < 0.45

    reward = calculate_reward(res["cost"], rate_ugx=2000, min_reward_ugx=100)
    assert 400 <= reward <= 1000


def test_long_clinical_monologue_is_hard_and_top_ranked():
    # 150+ word paragraph
    text = (
        "Yeah, so definitely one thing we need to rule out with the symptoms you're describing, "
        "including the progressive shortness of breath, chronic cough, and peripheral edema, is "
        "an acute exacerbation of COPD or underlying congestive heart failure. We will need to "
        "perform diagnostic spirometry, a chest radiograph to evaluate cardiomegaly or pulmonary "
        "vascular congestion, and order blood tests including a complete blood count and B-type "
        "natriuretic peptide. If your oxygen saturation remains below ninety-two percent on room "
        "air, supplemental oxygen therapy will be initiated immediately alongside nebulized "
        "bronchodilators and systemic corticosteroids."
    )
    res = score_sentence(text)
    assert res["length_words"] > 80
    assert res["difficulty"] == "hard"
    assert res["cost"] >= 0.70

    reward = calculate_reward(res["cost"], rate_ugx=2000, min_reward_ugx=100)
    assert reward >= 1400


def test_calculate_reward_bounds_and_step():
    # Minimum floor
    assert calculate_reward(0.01, rate_ugx=2000, min_reward_ugx=100) == 100
    # Step rounding to 10
    assert calculate_reward(0.355, rate_ugx=2000, min_reward_ugx=100, step=10) == 710
    # Ceiling at 1.0
    assert calculate_reward(1.0, rate_ugx=2000, min_reward_ugx=100) == 2000
