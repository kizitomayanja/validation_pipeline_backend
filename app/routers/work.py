import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from ..config import get_settings
from ..db import get_conn
from ..deps import current_user
from ..schemas import DatasetProgressOut, FinishedSentenceOut, SubmitIn, SubmitOut, WorkOut

router = APIRouter(prefix="/work", tags=["work"])

# Reward formula, single source of truth (see README): greatest(min_reward, round(cost * rate)).
EFFECTIVE_COST_SQL = """case
    when s.difficulty = 'hard'
     and length(s.noisy_english) - length(replace(s.noisy_english, '.', '')) > 2
    then least(s.cost * 2, 1)
    else s.cost
end"""

VALIDATION_TARGET = 10_000

NEXT_SQL = """
with pick as (
  select s.id from sentences s
  where s.validated_by is null
    and (s.claimed_by is null or s.claim_expires_at < now() or s.claimed_by = %(me)s)
    and not exists (select 1 from sentence_skips k where k.sentence_id = s.id and k.user_id = %(me)s)
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
          greatest(rs.min_reward_ugx, round(({effective_cost}) * rs.rate_ugx))::int as reward_ugx
""".format(effective_cost=EFFECTIVE_COST_SQL)


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
    return row


@router.get("/finished", response_model=list[FinishedSentenceOut])
def finished_sentences(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(
        """select id, noisy_english, attempted, clean_lug, validated_at
           from sentences
           where validated_by is not null
           order by validated_at, id"""
    ).fetchall()


@router.get("/next", response_model=WorkOut, responses={204: {"description": "Nothing left to validate"}})
def next_sentence(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    row = conn.execute(NEXT_SQL, {"me": user["id"], "mins": get_settings().claim_minutes}).fetchone()
    return row if row else Response(status_code=204)


@router.post("/{sentence_id}/submit", response_model=SubmitOut)
def submit(sentence_id: int, body: SubmitIn, user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
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
