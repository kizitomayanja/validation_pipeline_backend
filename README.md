# Ekyama validation API (FastAPI)

Serves the portal in `../portal`. Plain SQL via psycopg 3 against the schema in `../schema.sql`; one transaction per request.
The endpoint contract is in `../portal/README.md`; interactive docs are at `/docs` when running.

## Setup
```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # fill in DATABASE_URL and JWT_SECRET
```
1. Run `../schema.sql` in the Supabase SQL editor.
2. Load the sentences: `python -m scripts.import_sentences ../output/sentences_for_supabase.csv`
3. Create your admin account: `python -m scripts.create_admin you@example.com "Your Name"`
4. Start: `uvicorn app.main:app --reload` (http://localhost:8000). In the portal set `VITE_USE_MOCK=false`.

Use Supabase's **Session pooler** or direct connection string. The pool disables prepared statements, so the transaction pooler works too.

## Paying people (your manual flow)
`GET /admin/payouts?status=pending` lists who to pay, with the amount, network, number and registered name.
Send the money in MTN MoMo / Airtel Money, then `POST /admin/payouts/{id}/mark-paid {"provider_reference": "<transaction id>"}`.
To refuse, `POST /admin/payouts/{id}/reject {"reason": "..."}`: the sentences are released and the money returns to the user's balance.
Use `/docs` (log in via "Authorize" with an admin access token) until an admin screen exists. Rates and minimums:
`update reward_settings set rate_ugx = 2500, min_reward_ugx = 150, min_cashout_ugx = 10000;` (applies to future validations; stored rewards never change).

## How the important rules are enforced
| Rule | Where |
|---|---|
| Reward = `greatest(min_reward, round(cost * rate))`, computed server-side at submit and stored; hard sentences with >2 full stops use double cost, capped at 1 | `routers/work.py` |
| Nobody else gets a sentence you hold (15-min claim, `FOR UPDATE SKIP LOCKED`); an expired claim can be taken over, and the late submit then gets 409 | `routers/work.py` |
| Skipped sentences never return to that person (`sentence_skips`) | `routers/work.py` |
| `GET /work/progress` reports finished and total sentences with the dataset completion percentage | `routers/work.py` |
| Cash-out takes the whole balance in one transaction under a lock on the user row; one pending payout per user (also a unique index) | `routers/payouts.py` |
| Payout copies network/number/name at request time, so later edits or deletions don't alter history | `routers/payouts.py` |
| Refresh tokens rotate; replaying a used token revokes the whole session family | `routers/auth.py` |
| Password-reset links are short-lived, single-use, stored hashed, and revoke refresh sessions | `routers/auth.py` |
| Passwords hashed with argon2; unknown email and wrong password are indistinguishable (same message and timing) | `security.py` |
| Admin routes need `role = admin`; user and active status are re-checked from the DB on every request | `deps.py` |

## Password recovery
`POST /auth/password-reset/request {"email":"you@example.com"}` sends a single-use reset link to the configured frontend's
`/reset-password` page. The frontend should read the `token` query parameter and submit it to
`POST /auth/password-reset/confirm {"token":"<link token>","new_password":"..."}` to consume the link
and updates the password. Responses to reset requests do not disclose whether an account exists. Password reset tokens expire
after 30 minutes by default and are stored as hashes. Set `FRONTEND_URL` to the portal origin. To send real mail, configure
`SMTP_HOST`, `SMTP_PORT` (STARTTLS, default 587), and `SMTP_FROM_EMAIL`; SMTP authentication can be enabled with both
`SMTP_USERNAME` and `SMTP_PASSWORD`. The full outgoing message, including its link, is also printed to the API console for debugging.
For an existing database, apply the `password_reset_tokens` table and index DDL from `schema.sql` before deploying this version.

## Tests
```bash
python -m pytest tests -q
```
16 tests run against a real Postgres (bundled by `pgserver`, no install needed) with `../schema.sql` applied. They cover the auth and token
lifecycle, claims and skips, reward rules, the payment-method rules, the full cash-out / paid / rejected lifecycle, concurrency
(8 simultaneous cash-outs pay once; 4 simultaneous `/work/next` calls get 4 different sentences) and the real 16,061-sentence import.

## Before going live
* Serve over HTTPS and set `CORS_ORIGINS` to the portal's real origin.
* The `/auth/*` rate limiter is in memory and per IP: with several workers, or behind a proxy that hides client IPs, enforce limits at the proxy.
* If the database is Supabase, keep these tables off the public Data API (enable RLS with no policies); only this service should touch them.
* Two browser tabs refreshing the same token at once can trip reuse detection and sign the user out; they just sign in again.
* Not built: admin screens, email verification, user cancellation of a pending cash-out, a reviewer endpoint for `verified`, and automated mobile money disbursement.
