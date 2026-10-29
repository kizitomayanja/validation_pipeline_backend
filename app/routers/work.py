import psycopg
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..config import get_settings
from ..db import get_conn
from ..deps import current_user
from ..schemas import DatasetProgressOut, FinishedSentenceOut, LengthTier, SubmitIn, SubmitOut, WorkOut

router = APIRouter(prefix="/work", tags=["work"])

# Reward formula, single source of truth (see README): greatest(min_reward, round(cost * rate)).
EFFECTIVE_COST_SQL = """case
    when s.difficulty = 'hard'
     and length(s.noisy_english) - length(replace(s.noisy_english, '.', '')) > 2
    then least(s.cost * 2, 1)
    else s.cost
end"""

DYNAMIC_TIER_SQL = """case
    when s.length_words <= 15 then 'short'
    when s.length_words <= 40 then 'medium'
    else 'long'
end"""

VALIDATION_TARGET = 10_000

NEXT_SQL = """
with pick as (
  select s.id from sentences s
  where s.validated_by is null
    and (s.claimed_by is null or s.claim_expires_at < now() or s.claimed_by = %(me)s)
    and not exists (select 1 from sentence_skips k where k.sentence_id = s.id and k.user_id = %(me)s)
    and (
      %(tier)s::text is null
      or (%(tier)s::text = 'short' and s.length_words <= 15)
      or (%(tier)s::text = 'medium' and s.length_words > 15 and s.length_words <= 40)
      or (%(tier)s::text = 'long' and s.length_words > 40)
      or (s.claimed_by = %(me)s and s.claim_expires_at >= now())
    )
  order by coalesce(s.claimed_by = %(me)s and s.claim_expires_at >= now(), false) desc,  -- resume my own open claim first
           {effective_cost} desc, s.id
  limit 1
  for update skip locked
)
update sentences s
   set claimed_by = %(me)s, claim_expires_at = now() + make_interval(mins => %(mins)s)
  from pick, reward_settings rs
 where s.id = pick.id
returning s.id, s.noisy_english, s.attempted, s.difficulty,
          ({dynamic_tier}) as length_tier,
          s.length_words,
          greatest(rs.min_reward_ugx, round(({effective_cost}) * rs.rate_ugx))::int as reward_ugx
""".format(effective_cost=EFFECTIVE_COST_SQL, dynamic_tier=DYNAMIC_TIER_SQL)


def _is_budget_exhausted(conn: psycopg.Connection) -> bool:
    rs = conn.execute("select max_budget_ugx from reward_settings").fetchone()
    if not rs or rs["max_budget_ugx"] is None:
        return False
    spent = conn.execute(
        "select coalesce(sum(reward_ugx), 0)::int as spent from sentences where validated_by is not null"
    ).fetchone()["spent"]
    return spent >= rs["max_budget_ugx"]


@router.get("/progress", response_model=DatasetProgressOut)
def dataset_progress(conn: psycopg.Connection = Depends(get_conn)):
    row = conn.execute(
        """select count(*)::int as total_sentences,
                  count(*) filter (where validated_by is not null)::int as finished_sentences,
                  coalesce(
                      round(
                          100.0 * count(*) filter (where validated_by is not null)
                          / {target},
                          2
                      ),
                      0
                  )::float8 as completion_percentage
           from sentences""".format(target=VALIDATION_TARGET)
    ).fetchone()
    rs = conn.execute("select max_budget_ugx from reward_settings").fetchone()
    max_budget = rs["max_budget_ugx"] if rs else None
    spent_row = conn.execute("select coalesce(sum(reward_ugx), 0)::int as spent from sentences where validated_by is not null").fetchone()
    spent = spent_row["spent"] if spent_row else 0
    remaining = max(0, max_budget - spent) if max_budget is not None else None
    is_exhausted = (max_budget is not None) and (spent >= max_budget)
    return {
        **row,
        "max_budget_ugx": max_budget,
        "spent_budget_ugx": spent,
        "remaining_budget_ugx": remaining,
        "is_budget_exhausted": is_exhausted,
    }


@router.get("/finished", response_model=list[FinishedSentenceOut])
def finished_sentences(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(
        """select id, noisy_english, attempted, clean_lug, validated_at
           from sentences
           where validated_by is not null
           order by validated_at, id"""
    ).fetchall()


@router.get("/next", response_model=WorkOut, responses={204: {"description": "Nothing left to validate or budget reached"}})
def next_sentence(
    tier: Optional[LengthTier] = Query(None, description="Optional length tier filter: short, medium, long"),
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    try:
        # If budget is exhausted, allow completing only an active claim already held by the user
        has_active_claim = conn.execute(
            "select 1 from sentences where claimed_by = %s and claim_expires_at >= now() and validated_by is null limit 1",
            (user["id"],)
        ).fetchone()
        if not has_active_claim and _is_budget_exhausted(conn):
            return Response(status_code=204)

        row = conn.execute(
            NEXT_SQL,
            {"me": user["id"], "mins": get_settings().claim_minutes, "tier": tier}
        ).fetchone()
        return row if row else Response(status_code=204)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"WORK_NEXT_ERROR: {type(exc).__name__}: {str(exc)}")


@router.post("/{sentence_id}/submit", response_model=SubmitOut)
def submit(sentence_id: int, body: SubmitIn, user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    # Check claim ownership and detect unchanged AI output
    existing = conn.execute(
        "select attempted from sentences where id = %s and claimed_by = %s and validated_by is null",
        (sentence_id, user["id"])
    ).fetchone()
    if existing is None:
        raise HTTPException(409, "This sentence is no longer assigned to you. Load the next one.")
    if existing["attempted"].strip() == body.clean_lug.strip() and not body.confirmed_unchanged:
        raise HTTPException(
            422,
            "The translation is identical to the AI attempt. If the AI was completely accurate, confirm unchanged, or fix any translation errors."
        )

    # The reward is computed here from reward_settings, never taken from the client.
    row = conn.execute(
        """update sentences s
              set clean_lug = %s, validated_by = %s, validated_at = now(), claimed_by = null, claim_expires_at = null,
                    reward_ugx = greatest(rs.min_reward_ugx, round(({effective_cost}) * rs.rate_ugx))::int
             from reward_settings rs
            where s.id = %s and s.validated_by is null and s.claimed_by = %s
        returning s.reward_ugx""".format(effective_cost=EFFECTIVE_COST_SQL),
        (body.clean_lug, user["id"], sentence_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(409, "This sentence is no longer assigned to you. Load the next one.")
    return row


@router.post("/{sentence_id}/skip", status_code=204)
def skip(sentence_id: int, user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    released = conn.execute(
        "update sentences set claimed_by = null, claim_expires_at = null "
        "where id = %s and claimed_by = %s and validated_by is null returning id", (sentence_id, user["id"])).fetchone()
    if released:   # only record a skip for a sentence the user actually held
        conn.execute("insert into sentence_skips (user_id, sentence_id) values (%s, %s) on conflict do nothing", (user["id"], sentence_id))
    return Response(status_code=204)
