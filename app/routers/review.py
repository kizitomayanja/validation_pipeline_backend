import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..db import get_conn
from ..deps import reviewer_user
from ..schemas import ReviewActionIn, ReviewSentenceOut, ReviewValidatorOut

router = APIRouter(prefix="/review", tags=["review"])

_SELECT_REVIEW = """
select s.id, s.noisy_english, s.attempted, s.clean_lug, s.difficulty, s.reward_ugx,
       s.validated_by as validated_by_id, p.full_name as validator_name, p.email as validator_email,
       s.validated_at, s.verified
  from sentences s
  join profiles p on p.id = s.validated_by
"""


@router.get("/validators", response_model=list[ReviewValidatorOut])
def review_validators(reviewer: dict = Depends(reviewer_user),
                      conn: psycopg.Connection = Depends(get_conn)):
    """Fetch all validators who currently have sentences waiting in the review queue."""
    return conn.execute(
        """select p.id, p.full_name as name, p.email, count(s.id)::int as count
             from sentences s
             join profiles p on p.id = s.validated_by
            where s.validated_by is not null and s.verified = false
            group by p.id, p.full_name, p.email
            order by count desc, p.full_name asc"""
    ).fetchall()


@router.get("/queue", response_model=list[ReviewSentenceOut])
def review_queue(limit: int = Query(20, ge=1, le=100),
                 username: str | None = Query(None, description="Filter queue by validator full name or email"),
                 reviewer: dict = Depends(reviewer_user),
                 conn: psycopg.Connection = Depends(get_conn)):
    """Fetch sentences submitted by validators that await clinical/linguistic verification."""
    query = f"""{_SELECT_REVIEW}
           where s.validated_by is not null and s.verified = false"""
    params: list = []
    if username and username.strip():
        pattern = f"%{username.strip()}%"
        query += " and (p.full_name ilike %s or p.email ilike %s)"
        params.extend([pattern, pattern])

    query += " order by s.validated_at asc limit %s"
    params.append(limit)
    return conn.execute(query, tuple(params)).fetchall()


@router.get("/{sentence_id}", response_model=ReviewSentenceOut)
def get_sentence_for_review(sentence_id: int, reviewer: dict = Depends(reviewer_user),
                            conn: psycopg.Connection = Depends(get_conn)):
    row = conn.execute(f"{_SELECT_REVIEW} where s.id = %s", (sentence_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Sentence not found.")
    return row


@router.post("/{sentence_id}/approve", response_model=ReviewSentenceOut)
def approve_sentence(sentence_id: int, reviewer: dict = Depends(reviewer_user),
                     conn: psycopg.Connection = Depends(get_conn)):
    """Mark a validator's translation as clinically and linguistically verified."""
    row = conn.execute(
        """update sentences
              set verified = true, verified_by = %s, verified_at = now()
            where id = %s and validated_by is not null
        returning id""",
        (reviewer["id"], sentence_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Sentence not found or not yet validated.")
    return conn.execute(f"{_SELECT_REVIEW} where s.id = %s", (sentence_id,)).fetchone()


@router.post("/{sentence_id}/edit", response_model=ReviewSentenceOut)
def edit_and_approve_sentence(sentence_id: int, body: ReviewActionIn,
                              reviewer: dict = Depends(reviewer_user),
                              conn: psycopg.Connection = Depends(get_conn)):
    """Reviewer refines the Luganda translation (e.g. correcting a clinical term) and marks verified."""
    if not body.clean_lug or not body.clean_lug.strip():
        raise HTTPException(422, "The translation cannot be empty.")

    row = conn.execute(
        """update sentences
              set clean_lug = %s, verified = true, verified_by = %s, verified_at = now()
            where id = %s and validated_by is not null
        returning id""",
        (body.clean_lug.strip(), reviewer["id"], sentence_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Sentence not found or not yet validated.")
    return conn.execute(f"{_SELECT_REVIEW} where s.id = %s", (sentence_id,)).fetchone()


@router.post("/{sentence_id}/reject", status_code=204)
def reject_sentence(sentence_id: int, body: ReviewActionIn,
                    reviewer: dict = Depends(reviewer_user),
                    conn: psycopg.Connection = Depends(get_conn)):
    """Rejects inaccurate translation, revoking reward and returning sentence to validation queue."""
    # Sentence must be un-paid to be rejected
    sentence = conn.execute(
        "select paid_status, payout_id from sentences where id = %s and validated_by is not null for update",
        (sentence_id,)
    ).fetchone()
    if sentence is None:
        raise HTTPException(404, "Sentence not found or not validated.")
    if sentence["paid_status"]:
        raise HTTPException(409, "Cannot reject a sentence whose reward has already been paid out.")
    if sentence["payout_id"] is not None:
        raise HTTPException(409, "This sentence is currently locked in a pending payout. Reject the payout first.")

    conn.execute(
        """update sentences
              set clean_lug = null, validated_by = null, validated_at = null,
                  reward_ugx = null, claimed_by = null, claim_expires_at = null,
                  verified = false, verified_by = null, verified_at = null
            where id = %s""",
        (sentence_id,)
    )
    return Response(status_code=204)
