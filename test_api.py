import pathlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import ROOT, URI, make_user, sql


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


def test_password_reset_email_and_single_use_token(client, capsys, monkeypatch):
    _, tokens = make_user(client)
    sent = []

    def capture_email(recipient, reset_url):
        sent.append((recipient, reset_url))

    monkeypatch.setattr("app.routers.auth.send_password_reset_email", capture_email)
    request = client.post("/auth/password-reset/request", json={"email": "a@example.com"})
    assert request.status_code == 200
    assert request.json()["message"] == "If an account exists for that email, a password reset link has been sent."
    assert capsys.readouterr().out == ""
    assert sent[0][0] == "a@example.com"
    reset_url = sent[0][1]
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
    progress = client.get("/work/progress").json()
    assert progress["total_sentences"] == 4
    assert progress["finished_sentences"] == 0
    assert progress["completion_percentage"] == 0.0
    assert progress["max_budget_ugx"] is None
    assert progress["is_budget_exhausted"] is False
    h, _ = make_user(client)
    for sentence_id in (1, 2, 3):
        client.get("/work/next", headers=h)
        response = client.post(
            f"/work/{sentence_id}/submit",
            headers=h,
            json={"clean_lug": "ok"},
        )
        assert response.status_code == 200
    progress_after = client.get("/work/progress").json()
    assert progress_after["total_sentences"] == 4
    assert progress_after["finished_sentences"] == 3
    assert progress_after["completion_percentage"] == 0.03
    assert progress_after["spent_budget_ugx"] > 0
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
    assert sentence["reward_ugx"] == 1200
    submitted = client.post(
        "/work/1/submit",
        headers=h,
        json={"clean_lug": "ok"},
    )
    assert submitted.json()["reward_ugx"] == 1200
    assert float(sql("select cost from sentences where id = 1", fetch=True)[0][0]) == 0.4


def test_hard_sentence_with_two_full_stops_keeps_original_cost(client):
    h, _ = make_user(client)
    sql("update sentences set cost = 0.1 where id != 1")
    sql("update sentences set noisy_english = 'First. Second.', cost = 0.4 where id = 1")
    sentence = client.get("/work/next", headers=h).json()
    assert sentence["id"] == 1
    assert sentence["reward_ugx"] == 600


def test_next_serves_highest_cost_and_resumes_the_same_claim(client):
    h, _ = make_user(client)
    s = client.get("/work/next", headers=h).json()
    assert (s["id"], s["reward_ugx"], s["difficulty"]) == (1, 1350, "hard")
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
    assert r.json() == {"reward_ugx": 1350}   # client-supplied reward is ignored
    row = sql("select clean_lug, attempted, reward_ugx, validated_by is not null as v, claimed_by from sentences where id = 1", fetch=True)[0]
    assert row == ("Olina okuziyira?", "attempt 1", 1350, True, None)   # trimmed; original `attempted` untouched
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
    assert earned == 2100
    assert client.get("/me/balance", headers=h).json()["available_ugx"] == 2100

    assert client.post("/payouts", headers=h, json={"payment_method_id": "00000000-0000-0000-0000-000000000000"}).status_code == 400
    m = add_method(client, h).json()
    r = client.post("/payouts", headers=h, json={"payment_method_id": m["id"]})
    assert r.status_code == 201 and r.json()["amount_ugx"] == 2100 and r.json()["status"] == "pending"
    assert r.json()["account_name"] == "Ann N. Validator"
    b = client.get("/me/balance", headers=h).json()
    assert (b["total_earned_ugx"], b["available_ugx"], b["pending_ugx"], b["paid_out_ugx"]) == (2100, 0, 2100, 0)
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
    assert (b["total_earned_ugx"], b["available_ugx"], b["pending_ugx"], b["paid_out_ugx"]) == (2100, 0, 0, 2100)


def test_cash_out_rules_and_rejection_releases_money(client, admin):
    h, _ = make_user(client)
    m = add_method(client, h).json()
    r = client.post("/payouts", headers=h, json={"payment_method_id": m["id"]})
    assert r.status_code == 400 and "UGX 1,000" in r.json()["detail"]
    earn(client, h, 1)   # 1350
    other, _ = make_user(client, "b@example.com")
    om = add_method(client, other, "0701234567", "Bee", "airtel").json()
    assert client.post("/payouts", headers=h, json={"payment_method_id": om["id"]}).status_code == 400   # not your account
    pid = client.post("/payouts", headers=h, json={"payment_method_id": m["id"]}).json()["id"]
    rej = client.post(f"/admin/payouts/{pid}/reject", headers=admin, json={"reason": "Name does not match"})
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    b = client.get("/me/balance", headers=h).json()
    assert (b["available_ugx"], b["pending_ugx"], b["paid_out_ugx"]) == (1350, 0, 0)   # released
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
    assert sql("select count(*), sum(amount_ugx) from payouts", fetch=True)[0] == (1, 2100)


def test_concurrent_next_gives_each_validator_a_different_sentence(client):
    hs = [make_user(client, f"u{i}@example.com")[0] for i in range(4)]
    with ThreadPoolExecutor(4) as ex:
        ids = list(ex.map(lambda h: client.get("/work/next", headers=h).json()["id"], hs))
    assert sorted(ids) == [1, 2, 3, 4]


# ------------------------------------------------------------------ real dataset
def test_password_reset_alias_endpoint(client):
    _, _ = make_user(client, "alias@example.com")
    res = client.post("/auth/password-reset", json={"email": "alias@example.com"})
    assert res.status_code == 200
    assert "password reset link has been sent" in res.json()["message"]


def test_submitting_identical_attempt_requires_confirmation(client):
    h, _ = make_user(client)
    s = client.get("/work/next", headers=h).json()
    # Submitting identical text without confirmation fails with 422
    fail = client.post(f"/work/{s['id']}/submit", headers=h, json={"clean_lug": s["attempted"]})
    assert fail.status_code == 422
    assert "identical to the AI attempt" in fail.json()["detail"]

    # Submitting with confirmation succeeds
    ok = client.post(f"/work/{s['id']}/submit", headers=h, json={"clean_lug": s["attempted"], "confirmed_unchanged": True})
    assert ok.status_code == 200


def test_budget_cap_exhaustion_stops_new_claims(client):
    h, _ = make_user(client)
    s = client.get("/work/next", headers=h).json()
    sql(f"update reward_settings set max_budget_ugx = {s['reward_ugx']}")
    # User can finish their current active claim
    res = client.post(f"/work/{s['id']}/submit", headers=h, json={"clean_lug": "translated sentence"})
    assert res.status_code == 200

    # Next attempt should return 204 because max_budget_ugx has been reached
    next_res = client.get("/work/next", headers=h)
    assert next_res.status_code == 204

    progress = client.get("/work/progress").json()
    assert progress["is_budget_exhausted"] is True
    assert progress["remaining_budget_ugx"] == 0


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


# ------------------------------------------------------------------ admin budget & oversight & review
def test_admin_settings_budget_control(client, admin):
    # Default settings
    st = client.get("/admin/settings", headers=admin).json()
    assert st["rate_ugx"] == 2000
    assert st["min_reward_ugx"] == 100
    assert st["min_cashout_ugx"] == 1000
    assert st["max_budget_ugx"] is None
    assert st["total_spent_ugx"] == 0
    assert st["is_budget_exhausted"] is False

    # Validator cannot read or update admin settings
    val_h, _ = make_user(client, "val@example.com")
    assert client.get("/admin/settings", headers=val_h).status_code == 403
    assert client.patch("/admin/settings", headers=val_h, json={"max_budget_ugx": 5000000}).status_code == 403

    # Admin updates budget cap and rates
    updated = client.patch(
        "/admin/settings",
        headers=admin,
        json={"max_budget_ugx": 5000000, "rate_ugx": 2500, "min_cashout_ugx": 2000},
    ).json()
    assert updated["max_budget_ugx"] == 5000000
    assert updated["rate_ugx"] == 2500
    assert updated["min_cashout_ugx"] == 2000
    assert updated["remaining_budget_ugx"] == 5000000


def test_admin_overview_metrics(client, admin):
    val_h, _ = make_user(client, "val2@example.com")
    # Initial overview
    ov = client.get("/admin/overview", headers=admin).json()
    assert ov["total_sentences"] == 4
    assert ov["validated_sentences"] == 0
    assert ov["verified_sentences"] == 0
    assert ov["completion_percentage"] == 0.0

    # Validator validates a sentence
    client.get("/work/next", headers=val_h)
    client.post("/work/1/submit", headers=val_h, json={"clean_lug": "okutereeza 1"})

    ov_after = client.get("/admin/overview", headers=admin).json()
    assert ov_after["validated_sentences"] == 1
    assert ov_after["unvalidated_sentences"] == 3
    assert ov_after["completion_percentage"] == 25.0
    assert ov_after["total_earned_ugx"] == 1350

    # Non-admin rejected
    assert client.get("/admin/overview", headers=val_h).status_code == 403


def test_admin_user_oversight_and_role_management(client, admin):
    val_h, _ = make_user(client, "val3@example.com", name="Val Three")
    user_id = client.get("/me", headers=val_h).json()["id"]

    # Admin lists users
    users = client.get("/admin/users", headers=admin).json()
    target = next((u for u in users if u["id"] == user_id), None)
    assert target is not None
    assert target["role"] == "validator"
    assert target["is_active"] is True

    # Admin promotes user to reviewer
    promoted = client.patch(f"/admin/users/{user_id}", headers=admin, json={"role": "reviewer"}).json()
    assert promoted["role"] == "reviewer"

    # Promoted user now has reviewer access
    rev_queue = client.get("/review/queue", headers=val_h)
    assert rev_queue.status_code == 200

    # Admin deactivates user
    deactivated = client.patch(f"/admin/users/{user_id}", headers=admin, json={"is_active": False}).json()
    assert deactivated["is_active"] is False

    # Deactivated user is blocked from API
    assert client.get("/me", headers=val_h).status_code == 401


def test_review_pipeline_flow(client, admin):
    val_h, _ = make_user(client, "val4@example.com")
    val5_h, _ = make_user(client, "specialuser@example.com")

    s1 = client.get("/work/next", headers=val_h).json()
    client.post(f"/work/{s1['id']}/submit", headers=val_h, json={"clean_lug": "okulongoosa"})

    s2 = client.get("/work/next", headers=val5_h).json()
    client.post(f"/work/{s2['id']}/submit", headers=val5_h, json={"clean_lug": "okukola obulungi"})

    # Regular validator cannot access review queue
    assert client.get("/review/queue", headers=val_h).status_code == 403

    # Reviewer checks validators waiting in queue
    validators = client.get("/review/validators", headers=admin).json()
    assert len(validators) >= 2
    emails = [v["email"] for v in validators]
    assert "val4@example.com" in emails
    assert "specialuser@example.com" in emails

    # Filter queue by specific username / email
    filtered = client.get("/review/queue?username=specialuser", headers=admin).json()
    assert len(filtered) == 1
    assert filtered[0]["id"] == s2["id"]
    assert filtered[0]["validator_email"] == "specialuser@example.com"

    # Filter by non-existent username returns 0 items
    assert len(client.get("/review/queue?username=unknown_person", headers=admin).json()) == 0

    # Unfiltered queue returns all
    all_queue = client.get("/review/queue", headers=admin).json()
    assert len(all_queue) == 2

    # Test edit and approve on s1
    edited = client.post(
        f"/review/{s1['id']}/edit",
        headers=admin,
        json={"clean_lug": "okulongoosa obulungi"},
    ).json()
    assert edited["verified"] is True
    assert edited["clean_lug"] == "okulongoosa obulungi"

    # Reject sentence s2
    rej = client.post(f"/review/{s2['id']}/reject", headers=admin, json={"rejection_reason": "Inaccurate"})
    assert rej.status_code == 204

    # Sentence is cleared and ready to be re-served
    row = sql("select clean_lug, validated_by, verified from sentences where id = %s", (s2["id"],), fetch=True)[0]
    assert row == (None, None, False)


def test_password_reset_handles_mailer_failure_gracefully_without_503(client, monkeypatch):
    from unittest.mock import MagicMock
    import app.routers.auth as auth_module

    _, _ = make_user(client, 'mailerfail@example.com')
    monkeypatch.setattr(auth_module, 'send_password_reset_email', MagicMock(side_effect=OSError('Connection refused to mail server')))

    res = client.post('/auth/password-reset', json={'email': 'mailerfail@example.com'})
    assert res.status_code == 200
    assert 'password reset link has been sent' in res.json()['message']

    tokens = sql('select id, user_id from password_reset_tokens where used_at is null', fetch=True)
    assert len(tokens) > 0


def test_admin_export_stats_and_csv_download(client, admin):
    # Ordinary user gets 403 on admin export
    user_h, _ = make_user(client, "normal_validator@example.com")
    assert client.get("/admin/export/stats", headers=user_h).status_code == 403
    assert client.get("/admin/export/sentences.csv", headers=user_h).status_code == 403

    # Check stats before validations
    stats = client.get("/admin/export/stats", headers=admin).json()
    assert stats["total_validated"] == 0
    assert stats["unexported_verified"] == 0

    # User submits two translations
    s1 = client.get("/work/next", headers=user_h).json()
    client.post(f"/work/{s1['id']}/submit", headers=user_h, json={"clean_lug": "okukola obulungi"})

    s2 = client.get("/work/next", headers=user_h).json()
    client.post(f"/work/{s2['id']}/submit", headers=user_h, json={"clean_lug": "eddwaaliro ly'e Mulago"})

    # Check stats: 2 validated, 0 verified
    stats = client.get("/admin/export/stats", headers=admin).json()
    assert stats["total_validated"] == 2
    assert stats["total_verified"] == 0
    assert stats["unexported_validated"] == 2
    assert stats["unexported_verified"] == 0

    # Reviewer/admin approves sentence s1
    client.post(f"/review/{s1['id']}/approve", headers=admin)

    # Check stats: 2 validated, 1 verified
    stats = client.get("/admin/export/stats", headers=admin).json()
    assert stats["total_validated"] == 2
    assert stats["total_verified"] == 1
    assert stats["unexported_verified"] == 1
    assert stats["unexported_validated"] == 2

    # Download verified only as CSV with mark_exported=True
    res = client.get("/admin/export/sentences.csv?scope=verified_only&only_new=true&mark_exported=true", headers=admin)
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "ekyama_dataset_verified_only_" in res.headers["content-disposition"]
    csv_text = res.text
    assert csv_text.startswith("\ufeffsentence_id,english_prompt")
    assert "okukola obulungi" in csv_text
    assert "eddwaaliro ly'e Mulago" not in csv_text  # s2 not yet verified

    # Check stats after export: unexported_verified is now 0!
    stats_after = client.get("/admin/export/stats", headers=admin).json()
    assert stats_after["unexported_verified"] == 0
    assert stats_after["total_exports_count"] >= 1
    assert len(stats_after["recent_exports"]) >= 1
    assert stats_after["recent_exports"][0]["sentence_count"] == 1

    # Requesting only_new=true now returns 0 data rows (header only)
    res_empty = client.get("/admin/export/sentences.csv?scope=verified_only&only_new=true", headers=admin)
    lines = res_empty.text.strip().splitlines()
    assert len(lines) == 1  # only header row

    # Requesting cumulative (only_new=false) returns s1 again
    res_cumulative = client.get("/admin/export/sentences.csv?scope=verified_only&only_new=false", headers=admin)
    assert "okukola obulungi" in res_cumulative.text


def test_custom_partial_cashout_and_bounds(client, admin):
    h, _ = make_user(client, "partial_cashout@example.com")
    m = add_method(client, h).json()
    earn(client, h, 2)  # earns 2100
    bal = client.get("/me/balance", headers=h).json()
    assert bal["available_ugx"] == 2100

    # Below minimum (500 < 1000)
    r_low = client.post("/payouts", headers=h, json={"payment_method_id": m["id"], "amount_ugx": 500})
    assert r_low.status_code in (400, 422)

    # Exceeds available (5000 > 2800)
    r_high = client.post("/payouts", headers=h, json={"payment_method_id": m["id"], "amount_ugx": 5000})
    assert r_high.status_code == 400

    # Valid partial amount (1000 UGX)
    r_valid = client.post("/payouts", headers=h, json={"payment_method_id": m["id"], "amount_ugx": 1000})
    assert r_valid.status_code == 201
    assert r_valid.json()["amount_ugx"] == 1000
    assert r_valid.json()["status"] == "pending"

    # Remaining balance check
    b = client.get("/me/balance", headers=h).json()
    assert b["available_ugx"] == 1100
    assert b["pending_ugx"] == 1000


def test_tickets_role_routing_and_messaging(client, admin):
    # Setup users
    val_h, _ = make_user(client, "validator_ticket@example.com")
    rev_h, _ = make_user(client, "doctor_reviewer@example.com")
    rev_user = client.get("/me", headers=rev_h).json()
    client.patch(f"/admin/users/{rev_user['id']}", headers=admin, json={"role": "reviewer"})

    # 1. Validator creates a clinical terminology question
    r_clin = client.post("/tickets", headers=val_h, json={
        "category": "clinical_terminology",
        "subject": "Help translating hypertension symptoms",
        "message": "Is puleesa appropriate for acute arterial hypertension?",
        "sentence_id": 1,
    })
    assert r_clin.status_code == 201
    clin_data = r_clin.json()
    assert clin_data["category"] == "clinical_terminology"
    assert clin_data["target_role"] == "reviewer"
    assert clin_data["status"] == "open"
    assert clin_data["message_count"] == 1

    # 2. Validator creates a payment method, requests payout, and creates a payout issue ticket
    m = add_method(client, val_h).json()
    earn(client, val_h, 1)
    payout = client.post("/payouts", headers=val_h, json={"payment_method_id": m["id"]}).json()

    r_pay = client.post("/tickets", headers=val_h, json={
        "category": "payout_issue",
        "subject": "MTN MoMo withdrawal status",
        "message": "When will my pending cashout be sent?",
        "payout_id": payout["id"],
    })
    assert r_pay.status_code == 201
    assert r_pay.json()["target_role"] == "admin"

    # 3. Check scope routing
    my_tickets = client.get("/tickets?scope=my", headers=val_h).json()
    assert len(my_tickets) >= 2

    # Doctor checks clinical queue
    clin_queue = client.get("/tickets?scope=clinical", headers=rev_h).json()
    assert any(t["id"] == clin_data["id"] for t in clin_queue)
    assert not any(t["category"] == "payout_issue" for t in clin_queue)

    # Admin checks ops queue
    ops_queue = client.get("/tickets?scope=ops", headers=admin).json()
    assert any(t["id"] == r_pay.json()["id"] for t in ops_queue)

    # 4. Doctor sends a reply message to the clinical ticket
    reply = client.post(f"/tickets/{clin_data['id']}/messages", headers=rev_h, json={
        "message": "Yes, 'puleesa' is culturally recognized, but 'omusuwa gw'omusaayi ogulinnye' is more clinical.",
    })
    assert reply.status_code == 201
    assert reply.json()["sender_role"] == "reviewer"

    # 5. Doctor marks ticket resolved
    patch_res = client.patch(f"/tickets/{clin_data['id']}", headers=rev_h, json={"status": "resolved"})
    assert patch_res.status_code == 200
    assert patch_res.json()["status"] == "resolved"

    # Check ticket detail has both messages
    detail = client.get(f"/tickets/{clin_data['id']}", headers=val_h).json()
    assert detail["status"] == "resolved"
    assert len(detail["messages"]) == 2

    # 6. Privacy & Isolation Rules:
    # A different validator cannot see or access another validator's inquiries
    other_val_h, _ = make_user(client, "other_validator@example.com")
    other_tickets = client.get("/tickets?scope=my", headers=other_val_h).json()
    assert len(other_tickets) == 0
    # Trying to fetch someone else's ticket yields 403 Forbidden
    assert client.get(f"/tickets/{clin_data['id']}", headers=other_val_h).status_code == 403
    assert client.post(f"/tickets/{clin_data['id']}/messages", headers=other_val_h, json={"message": "intruder"}).status_code == 403

    # Reviewer cannot access confidential Admin ops / payout tickets
    assert client.get(f"/tickets/{r_pay.json()['id']}", headers=rev_h).status_code == 403

def test_length_tier_filtering_and_fields(client):
    from conftest import sql
    h, _ = make_user(client, "tier_validator@example.com")
    
    # Insert sentences across short, medium, and long tiers with distinct costs
    sql("truncate sentence_skips, sentences restart identity cascade")
    sql(
        "insert into sentences (source_row, noisy_english, attempted, length_words, cost_raw, cost, difficulty_score, difficulty, length_tier) "
        "values (101, 'Short clinical prompt', 'hint 1', 12, 0.9, 0.9, 0.9, 'hard', 'short'),"
        "       (102, 'Medium clinical prompt with multiple symptoms reported by patient', 'hint 2', 45, 0.85, 0.85, 0.85, 'hard', 'medium'),"
        "       (103, 'A very long in-depth clinical paragraph detailing continuous abdominal pain along with persistent fever and dehydration over past four weeks requiring immediate IV fluids', 'hint 3', 85, 0.95, 0.95, 0.95, 'hard', 'long')"
    )

    # 1. Default request without tier returns highest cost overall (sentence 103, cost 0.95)
    r_default = client.get("/work/next", headers=h)
    assert r_default.status_code == 200
    data_default = r_default.json()
    assert data_default["id"] == 1
    assert data_default["length_tier"] == "long"
    assert data_default["length_words"] == 85

    # Release claim via skip
    client.post(f"/work/{data_default['id']}/skip", headers=h)

    # 2. Filter for 'short' tier
    r_short = client.get("/work/next?tier=short", headers=h)
    assert r_short.status_code == 200
    data_short = r_short.json()
    assert data_short["id"] == 101 or data_short["length_tier"] == "short"
    assert data_short["length_tier"] == "short"
    assert data_short["length_words"] == 12

    # Release claim via skip
    client.post(f"/work/{data_short['id']}/skip", headers=h)

    # 3. Filter for 'medium' tier
    r_med = client.get("/work/next?tier=medium", headers=h)
    assert r_med.status_code == 200
    data_med = r_med.json()
    assert data_med["length_tier"] == "medium"
    assert data_med["length_words"] == 45

    # Release claim via skip
    client.post(f"/work/{data_med['id']}/skip", headers=h)

    # 4. Query with tier=all functions like no tier
    r_all = client.get("/work/next?tier=all", headers=h)
    assert r_all.status_code == 200
    assert r_all.json()["id"] in (1, 101, 102, 103)

    # 5. Invalid tier returns 422 validation error
    r_invalid = client.get("/work/next?tier=super_hard", headers=h)
    assert r_invalid.status_code == 422



def test_tier_switching_without_manual_skip_e2e(client):
    from conftest import sql
    user_h, user_info = make_user(client, "mood_switcher@example.com")
    other_h, _ = make_user(client, "second_validator@example.com")

    # Reset queue with known short, medium, and long sentences
    sql("truncate sentence_skips, sentences restart identity cascade")
    sql(
        "insert into sentences (source_row, noisy_english, attempted, length_words, cost_raw, cost, difficulty_score, difficulty, length_tier) "
        "values (201, 'Short prompt for headache', 'hint 1', 15, 0.8, 0.8, 0.8, 'easy', 'short'),"
        "       (202, 'Medium prompt clinical consultation describing acute respiratory discomfort and high fever', 'hint 2', 45, 0.85, 0.85, 0.85, 'hard', 'medium'),"
        "       (203, 'Long detailed triage monologue detailing chronic cardiovascular conditions with pedal edema lasting more than thirty days needing urgent ultrasound and ECG analysis', 'hint 3', 88, 0.9, 0.9, 0.9, 'hard', 'long')"
    )

    # 1. User requests short sentence
    r_short = client.get("/work/next?tier=short", headers=user_h)
    assert r_short.status_code == 200
    s_short = r_short.json()
    assert s_short["length_tier"] == "short"
    assert s_short["id"] == 1

    # Verify sentence 1 is actively claimed by this user
    rows = sql("select id, claimed_by, claim_expires_at > now() as active from sentences where id = 1", fetch=True)
    assert rows[0][1] == user_info["id"]
    assert rows[0][2] is True

    # 2. CRITICAL BUG REGRESSION: User switches mood to 'medium' WITHOUT skipping sentence 1
    r_med = client.get("/work/next?tier=medium", headers=user_h)
    assert r_med.status_code == 200
    s_med = r_med.json()
    assert s_med["length_tier"] == "medium"
    assert s_med["id"] == 2
    assert s_med["id"] != s_short["id"], "Switching tier must NOT return the previous claimed sentence of a different tier"

    # Verify sentence 1 claim was released so another worker can take it
    s1_status = sql("select id, claimed_by, claim_expires_at from sentences where id = 1", fetch=True)
    assert s1_status[0][1] is None, "Previous disparate claim must be released to pool"
    assert s1_status[0][2] is None

    # Verify other validator can now immediately claim sentence 1
    other_r = client.get("/work/next?tier=short", headers=other_h)
    assert other_r.status_code == 200
    assert other_r.json()["id"] == 1

    # 3. Verify requesting the same tier ('medium') again resumes the existing active claim
    r_med_resume = client.get("/work/next?tier=medium", headers=user_h)
    assert r_med_resume.status_code == 200
    assert r_med_resume.json()["id"] == 2

    # 4. User switches to 'long' without skipping medium
    r_long = client.get("/work/next?tier=long", headers=user_h)
    assert r_long.status_code == 200
    s_long = r_long.json()
    assert s_long["length_tier"] == "long"
    assert s_long["id"] == 3

    # Medium claim was released
    s2_status = sql("select id, claimed_by from sentences where id = 2", fetch=True)
    assert s2_status[0][1] is None

    # 5. User switches back to 'all' tasks - should resume active 'long' claim without releasing it
    r_all = client.get("/work/next?tier=all", headers=user_h)
    assert r_all.status_code == 200
    assert r_all.json()["id"] == 3


def test_option_b_calibrated_tier_reward_multipliers(client):
    from conftest import sql
    h, _ = make_user(client, "tier_pricing_user@example.com")
    sql("update reward_settings set rate_ugx = 200, min_reward_ugx = 100")
    sql("truncate sentence_skips, sentences restart identity cascade")

    # Insert 3 sentences with identical base cost = 1.0 (one in each tier)
    sql(
        "insert into sentences (source_row, noisy_english, attempted, length_words, cost_raw, cost, difficulty_score, difficulty, length_tier) "
        "values (401, 'Short prompt for headache', 'hint 1', 15, 1.0, 1.0, 1.0, 'hard', 'short'),"
        "       (402, 'Medium prompt clinical consultation describing acute respiratory discomfort and high fever', 'hint 2', 45, 1.0, 1.0, 1.0, 'hard', 'medium'),"
        "       (403, 'Long detailed triage monologue detailing chronic cardiovascular conditions with pedal edema lasting more than thirty days needing urgent ultrasound and ECG analysis', 'hint 3', 88, 1.0, 1.0, 1.0, 'hard', 'long')"
    )

    # 1. Short sentence pays: round(1.0 * 200 * 0.75) = 150 UGX
    r_short = client.get("/work/next?tier=short", headers=h)
    assert r_short.status_code == 200
    assert r_short.json()["reward_ugx"] == 150
    assert r_short.json()["length_tier"] == "short"

    # 2. Medium sentence pays: round(1.0 * 200 * 1.25) = 250 UGX
    r_med = client.get("/work/next?tier=medium", headers=h)
    assert r_med.status_code == 200
    assert r_med.json()["reward_ugx"] == 250
    assert r_med.json()["length_tier"] == "medium"

    # 3. Long sentence pays: round(1.0 * 200 * 2.00) = 400 UGX
    r_long = client.get("/work/next?tier=long", headers=h)
    assert r_long.status_code == 200
    assert r_long.json()["reward_ugx"] == 400
    assert r_long.json()["length_tier"] == "long"

    # 4. Verify submit credits the exact same 400 UGX upon validation
    submitted = client.post("/work/3/submit", headers=h, json={"clean_lug": "okukola obulungi"})
    assert submitted.status_code == 200
    assert submitted.json()["reward_ugx"] == 400
