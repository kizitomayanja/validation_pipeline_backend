"""Tests run against a REAL Postgres (bundled by the `pgserver` package) with ../schema.sql applied,
so constraints, partial indexes, FOR UPDATE SKIP LOCKED and the user_balances view are all exercised."""
import os
import pathlib
import tempfile

import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parent
URI = os.getenv("DATABASE_URL")
if not URI:
    try:
        import pgserver
        _server = pgserver.get_server(pathlib.Path(tempfile.mkdtemp()) / "pg", cleanup_mode="stop")
        URI = _server.get_uri()
    except (ImportError, Exception):
        URI = "postgresql://ekyama:ekyama_password@localhost:5432/ekyama_test"

os.environ.update(DATABASE_URL=URI, JWT_SECRET="test-secret-test-secret-test-secret-123", AUTH_RATE_LIMIT="1000")

with psycopg.connect(URI, autocommit=True) as c:
    exists = c.execute("select exists (select from information_schema.tables where table_name = 'profiles')").fetchone()[0]
    if not exists:
        c.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
    else:
        # Ensure user_balances view supports custom partial cashouts
        c.execute("""
            create or replace view user_balances as
            select p.id as user_id,
                   coalesce(sent.total_earned, 0) as total_earned_ugx,
                   greatest(0, coalesce(sent.total_earned, 0) - coalesce(pay.pending_amount, 0) - coalesce(pay.paid_amount, 0)) as available_ugx,
                   coalesce(pay.pending_amount, 0) as pending_ugx,
                   coalesce(pay.paid_amount, 0) as paid_out_ugx,
                   coalesce(sent.validated_count, 0) as validated_count
            from profiles p
            left join (
                select validated_by, sum(reward_ugx) as total_earned, count(id) as validated_count
                from sentences
                where validated_by is not null
                group by validated_by
            ) sent on sent.validated_by = p.id
            left join (
                select
                    user_id,
                    sum(amount_ugx) filter (where status = 'pending') as pending_amount,
                    sum(amount_ugx) filter (where status = 'paid') as paid_amount
                from payouts
                group by user_id
            ) pay on pay.user_id = p.id;
        """)
        # Ensure support_tickets exists
        has_tickets = c.execute("select exists (select from information_schema.tables where table_name = 'support_tickets')").fetchone()[0]
        if not has_tickets:
            c.execute("""
                create table if not exists support_tickets (
                  id          uuid primary key default gen_random_uuid(),
                  ticket_num  serial unique,
                  author_id   uuid not null references profiles(id) on delete cascade,
                  category    text not null check (category in ('clinical_terminology', 'review_dispute', 'payout_issue', 'role_request', 'general')),
                  target_role text not null check (target_role in ('reviewer', 'admin')),
                  subject     text not null,
                  status      text not null default 'open' check (status in ('open', 'in_progress', 'resolved')),
                  sentence_id bigint references sentences(id) on delete set null,
                  payout_id   uuid references payouts(id) on delete set null,
                  created_at  timestamptz not null default now(),
                  updated_at  timestamptz not null default now()
                );
                create table if not exists ticket_messages (
                  id         uuid primary key default gen_random_uuid(),
                  ticket_id  uuid not null references support_tickets(id) on delete cascade,
                  sender_id  uuid not null references profiles(id) on delete cascade,
                  message    text not null,
                  created_at timestamptz not null default now()
                );
            """)

SEED = [  # (noisy_english, attempted, cost, difficulty) -> rewards with rate 2000 / min 100: 1800, 1000, 100, 100
    ("Do you have shortness of breath?", "attempt 1", 0.9, "hard"),
    ("Any sputum with the cough?", "attempt 2", 0.5, "medium"),
    ("And do you smoke?", "attempt 3", 0.05, "easy"),
    ("No, nothing like that.", "attempt 4", 0.0, "easy"),
]


def sql(query, params=None, fetch=False):
    with psycopg.connect(URI, autocommit=True) as c:
        cur = c.execute(query, params)
        return cur.fetchall() if fetch else None


@pytest.fixture(autouse=True)
def fresh_db():
    sql("truncate sentence_skips, sentences, payouts, payment_methods, refresh_tokens, profiles restart identity cascade")
    sql("update reward_settings set rate_ugx = 2000, min_reward_ugx = 100, min_cashout_ugx = 1000, max_budget_ugx = null")
    for i, (en, lug, cost, diff) in enumerate(SEED, 1):
        sql("insert into sentences (source_row, noisy_english, attempted, length_words, cost_raw, cost, difficulty_score, difficulty) "
            "values (%s,%s,%s,5,%s,%s,%s,%s)", (i, en, lug, cost, cost, cost, diff))


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_user(client, email="a@example.com", name="Ann Validator"):
    r = client.post("/auth/register", json={"full_name": name, "email": email, "password": "correct horse"})
    assert r.status_code == 201, r.text
    t = r.json()
    return {"Authorization": f"Bearer {t['access_token']}"}, t


@pytest.fixture
def admin(client):
    from app.security import hash_password
    sql("insert into profiles (email, password_hash, full_name, role) values ('boss@example.com', %s, 'Boss', 'admin')", (hash_password("admin-password"),))
    r = client.post("/auth/login", json={"email": "boss@example.com", "password": "admin-password"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
