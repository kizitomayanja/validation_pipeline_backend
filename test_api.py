import pathlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest

from .conftest import ROOT, URI, make_user, sql


def add_method(client, h, phone="0772 123456", name="Ann N. Validator", provider="mtn"):
    return client.post("/me/payment-methods", headers=h, json={"provider": provider, "phone_number": phone, "account_name": name})


def earn(client, h, n=2):
    """Validate n sentences; returns total reward."""
    total = 0
    for _ in range(n):
        s = client.get("/work/next", headers=h).json()
        r = client.post(f"/work/{s['id']}/submit", headers=h, json={"clean_lug": "okutereeza  "})
        assert r.status_code == 200, r.text
        total += r.json()["reward_ugx"]
    return total


# ------------------------------------------------------------------ auth
def test_register_login_me(client):
    h, _ = make_user(client)
    assert client.get("/me", headers=h).json()["email"] == "a@example.com"
    assert client.post("/auth/register", json={"full_name": "X", "email": "A@Example.com", "password": "12345678"}).status_code == 409
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever1"}).status_code == 401
    assert client.post("/auth/login", json={"email": " A@EXAMPLE.com ", "password": "correct horse"}).status_code == 200
    assert client.post("/auth/register", json={"full_name": "X", "email": "b@example.com", "password": "short"}).status_code == 422


def test_protected_routes_need_a_valid_token(client):
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    _, t = make_user(client)
    # a refresh token is not an access token
    assert client.get("/me", headers={"Authorization": f"Bearer {t['refresh_token']}"}).status_code == 401


def test_refresh_rotates_and_reuse_revokes_family(client):
    _, t1 = make_user(client)
    r = client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    assert client.get("/me", headers={"Authorization": f"Bearer {t2['access_token']}"}).status_code == 200
    # replaying the old token = theft signal: rejected AND the newer token in that family dies too
    assert client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": "garbage"}).status_code == 401


def test_logout_revokes_session_and_deactivated_users_are_locked_out(client):
    h, t = make_user(client)
    assert client.post("/auth/logout", json={"refresh_token": t["refresh_token"]}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    sql("update profiles set is_active = false")
    assert client.get("/me", headers=h).status_code == 401
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "correct horse"}).status_code == 403


def test_password_reset_email_and_single_use_token(client, capsys):
    _, tokens = make_user(client)
    request = client.post("/auth/password-reset/request", json={"email": "a@example.com"})
    assert request.status_code == 200
    assert request.json()["message"] == "If an account exists for that email, a password reset link has been sent."
    output = capsys.readouterr().out
    assert "To: a@example.com" in output
    reset_url = next(line for line in output.splitlines() if "token=" in line)
    reset_token = parse_qs(urlparse(reset_url).query)["token"][0]

    assert client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "a brand new password"},
    ).status_code == 200
    assert client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "another new password"},
    ).status_code == 400
    assert client.post(
        "/auth/login",
        json={"email": "a@example.com", "password": "correct horse"},
    ).status_code == 401
    assert client.post(
        "/auth/login",
        json={"email": "a@example.com", "password": "a brand new password"},
    ).status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_password_reset_request_does_not_reveal_unknown_email(client, capsys):
    response = client.post("/auth/password-reset/request", json={"email": "missing@example.com"})
    assert response.status_code == 200
    assert response.json()["message"] == "If an account exists for that email, a password reset link has been sent."
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------ work
def test_dataset_progress_reports_finished_sentence_percentage(client):
    assert client.get("/work/progress").json() == {
        "total_sentences": 4,
        "finished_sentences": 0,
        "completion_percentage": 0.0,
    }
    h, _ = make_user(client)
    for sentence_id in (1, 2, 3):
        client.get("/work/next", headers=h)
        response = client.post(
            f"/work/{sentence_id}/submit",
            headers=h,
            json={"clean_lug": "ok"},
        )
        assert response.status_code == 200
    assert client.get("/work/progress").json() == {
        "total_sentences": 4,
        "finished_sentences": 3,
        "completion_percentage": 0.03,
    }
    finished = client.get("/work/finished", headers=h)
    assert finished.status_code == 200
    assert [sentence["id"] for sentence in finished.json()] == [1, 2, 3]
    assert all(sentence["clean_lug"] == "ok" for sentence in finished.json())


def test_finished_sentences_requires_authentication(client):
    assert client.get("/work/finished").status_code == 401


def test_hard_sentence_with_more_than_two_full_stops_has_doubled_cost(client):
    h, _ = make_user(client)
    sql("update sentences set noisy_english = 'First. Second. Third.', cost = 0.4 where id = 1")
    sentence = client.get("/work/next", headers=h).json()
    assert sentence["id"] == 1
    assert sentence["reward_ugx"] == 1600
    submitted = client.post(
        "/work/1/submit",
        headers=h,
        json={"clean_lug": "ok"},
    )
    assert submitted.json()["reward_ugx"] == 1600
    assert sql("select cost from sentences where id = 1", fetch=True)[0][0] == 0.4


def test_hard_sentence_with_two_full_stops_keeps_original_cost(client):
    h, _ = make_user(client)
    sql("update sentences set noisy_english = 'First. Second.', cost = 0.4 where id = 1")
    sentence = client.get("/work/next", headers=h).json()
    assert sentence["id"] == 1
    assert sentence["reward_ugx"] == 800


def test_next_serves_highest_cost_and_resumes_the_same_claim(client):
    h, _ = make_user(client)
    s = client.get("/work/next", headers=h).json()
    assert (s["id"], s["reward_ugx"], s["difficulty"]) == (1, 1800, "hard")
    assert client.get("/work/next", headers=h).json()["id"] == 1   # page refresh gets the same sentence


def test_two_validators_never_get_the_same_sentence(client):
    ha, _ = make_user(client, "a@example.com")
    hb, _ = make_user(client, "b@example.com")
    a, b = client.get("/work/next", headers=ha).json(), client.get("/work/next", headers=hb).json()
    assert a["id"] != b["id"]
    # B can't submit A's sentence
    assert client.post(f"/work/{a['id']}/submit", headers=hb, json={"clean_lug": "x"}).status_code == 409


def test_expired_claim_can_be_taken_over(client):
    ha, _ = make_user(client, "a@example.com")
    hb, _ = make_user(client, "b@example.com")
    assert client.get("/work/next", headers=ha).json()["id"] == 1
    sql("update sentences set claim_expires_at = now() - interval '1 minute' where id = 1")
    assert client.get("/work/next", headers=hb).json()["id"] == 1
    assert client.post("/work/1/submit", headers=ha, json={"clean_lug": "late"}).status_code == 409   # A lost it


def test_submit_stores_translation_and_server_side_reward(client):
    h, _ = make_user(client)
    s = client.get("/work/next", headers=h).json()
    r = client.post(f"/work/{s['id']}/submit", headers=h, json={"clean_lug": "  Olina okuziyira?  ", "reward_ugx": 999999})
    assert r.json() == {"reward_ugx": 1800}   # client-supplied reward is ignored
    row = sql("select clean_lug, attempted, reward_ugx, validated_by is not null as v, claimed_by from sentences where id = 1", fetch=True)[0]
    assert row == ("Olina okuziyira?", "attempt 1", 1800, True, None)   # trimmed; original `attempted` untouched
    assert client.post("/work/1/submit", headers=h, json={"clean_lug": "again"}).status_code == 409
    assert client.post(f"/work/{s['id']}/submit", headers=h, json={"clean_lug": "   "}).status_code == 422


def test_minimum_reward_applies_to_low_cost_sentences(client):
    h, _ = make_user(client)
    sql("update sentences set validated_by = null where false")
    for sid in (1, 2):   # burn the two costly ones so a low-cost sentence is next
        client.get("/work/next", headers=h); client.post(f"/work/{sid}/submit", headers=h, json={"clean_lug": "x"})
    s = client.get("/work/next", headers=h).json()
    assert s["id"] == 3 and s["reward_ugx"] == 100   # round(0.05*2000)=100
    sql("update sentences set cost = 0 where id = 3")
    sql("update sentences set claimed_by = null where id = 3")
    assert client.get("/work/next", headers=h).json()["reward_ugx"] == 100   # zero cost still pays the minimum


def test_skip_never_returns_the_same_sentence_and_queue_ends_with_204(client):
    h, _ = make_user(client)
    assert client.get("/work/next", headers=h).json()["id"] == 1
    assert client.post("/work/1/skip", headers=h).status_code == 204
    assert client.get("/work/next", headers=h).json()["id"] == 2
    for sid in (2, 3, 4):
        client.post(f"/work/{sid}/skip", headers=h)
        client.get("/work/next", headers=h)
    assert client.get("/work/next", headers=h).status_code == 204   # only skipped sentences remain
    h2, _ = make_user(client, "b@example.com")
    assert client.get("/work/next", headers=h2).json()["id"] == 1    # skips are per user


# ------------------------------------------------------------------ payment methods
def test_payment_methods(client):
    h, _ = make_user(client)
    a = add_method(client, h)
    assert a.status_code == 201 and a.json()["phone_number"] == "+256772123456" and a.json()["is_default"] is True
    b = add_method(client, h, "+256751000111", "Someone Else", "airtel")
    assert b.json()["is_default"] is False   # account name may differ from the user's name
    assert add_method(client, h, "0772123456").status_code == 409                # same number, different format
    assert add_method(client, h, "12345").status_code == 422
    assert client.delete(f"/me/payment-methods/{a.json()['id']}", headers=h).status_code == 204
    left = client.get("/me/payment-methods", headers=h).json()
    assert len(left) == 1 and left[0]["is_default"] is True                      # default promoted
    assert client.delete(f"/me/payment-methods/{a.json()['id']}", headers=h).status_code == 404
    assert client.delete("/me/payment-methods/not-a-uuid", headers=h).status_code == 404
    for i in range(4):
        assert add_method(client, h, f"07720000{i:02d}").status_code == 201
    assert add_method(client, h, "0772999999").status_code == 400                # max 5
    full = client.get("/me/payment-methods", headers=h).json()
    assert client.delete(f"/me/payment-methods/{full[-1]['id']}", headers=h).status_code == 204   # free a slot
    assert add_method(client, h, "0772123456").status_code == 201                # a previously removed number can be re-added


# ------------------------------------------------------------------ money
def test_balance_and_full_cash_out_lifecycle(client, admin):
    h, _ = make_user(client)
    assert client.get("/me/balance", headers=h).json() == dict(total_earned_ugx=0, available_ugx=0, pending_ugx=0, paid_out_ugx=0, validated_count=0, min_cashout_ugx=1000)
    earned = earn(client, h, 2)
    assert earned == 2800
    assert client.get("/me/balance", headers=h).json()["available_ugx"] == 2800

    assert client.post("/payouts", headers=h, json={"payment_method_id": "00000000-0000-0000-0000-000000000000"}).status_code == 400
    m = add_method(client, h).json()
    r = client.post("/payouts", headers=h, json={"payment_method_id": m["id"]})
    assert r.status_code == 201 and r.json()["amount_ugx"] == 2800 and r.json()["status"] == "pending"
    assert r.json()["account_name"] == "Ann N. Validator"
    b = client.get("/me/balance", headers=h).json()
    assert (b["total_earned_ugx"], b["available_ugx"], b["pending_ugx"], b["paid_out_ugx"]) == (2800, 0, 2800, 0)
    assert client.post("/payouts", headers=h, json={"payment_method_id": m["id"]}).status_code == 409   # one pending at a time

    # editing/removing the method later doesn't change the payout record
    client.delete(f"/me/payment-methods/{m['id']}", headers=h)
    pid = client.get("/payouts", headers=h).json()[0]["id"]
    pend = client.get("/admin/payouts?status=pending", headers=admin).json()
    assert len(pend) == 1 and pend[0]["user_email"] == "a@example.com" and pend[0]["sentence_count"] == 2 and pend[0]["phone_number"] == "+256772123456"

    assert client.post(f"/admin/payouts/{pid}/mark-paid", headers=h, json={"provider_reference": "MP123456"}).status_code == 403
    assert client.post(f"/admin/payouts/{pid}/mark-paid", headers=admin, json={"provider_reference": "x"}).status_code == 422
    ok = client.post(f"/admin/payouts/{pid}/mark-paid", headers=admin, json={"provider_reference": "MP123456"})
    assert ok.status_code == 200 and ok.json()["status"] == "paid" and ok.json()["provider_reference"] == "MP123456"
    assert client.post(f"/admin/payouts/{pid}/mark-paid", headers=admin, json={"provider_reference": "MP999"}).status_code == 409
    assert sql("select count(*) from sentences where paid_status and paid_at is not null and payout_id = %s", (pid,), fetch=True)[0][0] == 2
    b = client.get("/me/balance", headers=h).json()
    assert (b["total_earned_ugx"], b["available_ugx"], b["pending_ugx"], b["paid_out_ugx"]) == (2800, 0, 0, 2800)


def test_cash_out_rules_and_rejection_releases_money(client, admin):
    h, _ = make_user(client)
    m = add_method(client, h).json()
    r = client.post("/payouts", headers=h, json={"payment_method_id": m["id"]})
    assert r.status_code == 400 and "UGX 1,000" in r.json()["detail"]
    earn(client, h, 1)   # 1800
    other, _ = make_user(client, "b@example.com")
    om = add_method(client, other, "0701234567", "Bee", "airtel").json()
    assert client.post("/payouts", headers=h, json={"payment_method_id": om["id"]}).status_code == 400   # not your account
    pid = client.post("/payouts", headers=h, json={"payment_method_id": m["id"]}).json()["id"]
    rej = client.post(f"/admin/payouts/{pid}/reject", headers=admin, json={"reason": "Name does not match"})
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    b = client.get("/me/balance", headers=h).json()
    assert (b["available_ugx"], b["pending_ugx"], b["paid_out_ugx"]) == (1800, 0, 0)   # released
    assert client.post("/payouts", headers=h, json={"payment_method_id": m["id"]}).status_code == 201   # can ask again
    assert client.get("/admin/payouts", headers=admin).json()[0]["status"] in ("rejected", "pending")
    assert client.get("/admin/payouts", headers=h).status_code == 403


def test_concurrent_cash_outs_pay_out_exactly_once(client):
    h, _ = make_user(client)
    m = add_method(client, h).json()
    earn(client, h, 2)
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: client.post("/payouts", headers=h, json={"payment_method_id": m["id"]}).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7
    assert sql("select count(*), sum(amount_ugx) from payouts", fetch=True)[0] == (1, 2800)


def test_concurrent_next_gives_each_validator_a_different_sentence(client):
    hs = [make_user(client, f"u{i}@example.com")[0] for i in range(4)]
    with ThreadPoolExecutor(4) as ex:
        ids = list(ex.map(lambda h: client.get("/work/next", headers=h).json()["id"], hs))
    assert sorted(ids) == [1, 2, 3, 4]


# ------------------------------------------------------------------ real dataset
CSV = ROOT / "output" / "sentences_for_supabase.csv"


@pytest.mark.skipif(not CSV.exists(), reason="dataset CSV not present")
def test_real_dataset_imports_and_serves(client):
    import psycopg
    sql("truncate sentence_skips, sentences restart identity cascade")
    with psycopg.connect(URI, autocommit=True) as c, c.cursor() as cur, open(CSV, "rb") as f:
        with cur.copy("copy sentences (source_row, clean_english, noisy_english, clean_lug, noisy_lug, attempted, length_words, "
                      "cost_raw, cost, difficulty_score, difficulty) from stdin with (format csv, header true)") as cp:
            while chunk := f.read(1 << 16):
                cp.write(chunk)
    assert sql("select count(*) from sentences", fetch=True)[0][0] == 16061
    h, _ = make_user(client)
    s = client.get("/work/next", headers=h).json()
    top = sql("select max(cost) from sentences", fetch=True)[0][0]
    assert s["reward_ugx"] == round(float(top) * 2000) and s["noisy_english"] and s["attempted"]
