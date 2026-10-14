-- Validation portal schema (Supabase / Postgres). Supersedes the earlier schema.sql.
-- Auth is handled by the FastAPI service (not Supabase Auth), so `profiles` stores password hashes.
-- The API connects with a server-side role; do NOT expose these tables to the browser via Supabase's public API
-- (enable RLS with no policies on every table below if the Data API is on).

create table profiles (
  id            uuid primary key default gen_random_uuid(),
  email         text not null unique,
  password_hash text not null,                      -- argon2/bcrypt hash, never plaintext
  full_name     text not null,
  role          text not null default 'validator' check (role in ('validator','reviewer','admin')),
  is_active     boolean not null default true,
  created_at    timestamptz not null default now()
);

-- Rotating refresh tokens. Store only a hash. Reusing a revoked token => revoke the whole family.
create table refresh_tokens (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  family_id   uuid not null,
  token_hash  text not null unique,
  expires_at  timestamptz not null,
  revoked_at  timestamptz,
  created_at  timestamptz not null default now()
);
create index on refresh_tokens (user_id);

-- Password-reset tokens are random, single-use credentials; only their SHA-256 hashes are stored.
create table password_reset_tokens (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  token_hash  text not null unique,
  expires_at  timestamptz not null,
  used_at     timestamptz,
  created_at  timestamptz not null default now()
);
create index password_reset_tokens_user_id on password_reset_tokens (user_id);

-- Mobile money accounts. account_name is the name the network registered for the number
-- and may differ from profiles.full_name.
create table payment_methods (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references profiles(id) on delete cascade,
  provider     text not null check (provider in ('mtn','airtel')),
  phone_number text not null check (phone_number ~ '^\+256[0-9]{9}$'),   -- E.164, Uganda
  account_name text not null,
  is_default   boolean not null default false,
  deleted_at   timestamptz,                         -- soft delete: payouts keep their snapshot anyway
  created_at   timestamptz not null default now()
);
create unique index payment_methods_unique_number on payment_methods (user_id, phone_number) where deleted_at is null;
create unique index payment_methods_one_default   on payment_methods (user_id) where is_default and deleted_at is null;

-- Single-row config. reward_ugx = greatest(min_reward_ugx, round(cost * rate_ugx)), snapshotted at validation time.
create table reward_settings (
  id              boolean primary key default true check (id),
  rate_ugx        integer not null default 2000 check (rate_ugx > 0),   -- UGX paid for a sentence with cost = 1.0
  min_reward_ugx  integer not null default 100  check (min_reward_ugx >= 0),
  min_cashout_ugx integer not null default 5000 check (min_cashout_ugx > 0),
  max_budget_ugx  integer check (max_budget_ugx is null or max_budget_ugx > 0), -- Total campaign payout budget cap in UGX (null = unlimited)
  updated_at      timestamptz not null default now()
);
insert into reward_settings default values;

-- A cash-out request. Manual flow: pending -> (admin pays by mobile money) -> paid, or rejected / cancelled.
-- Provider/phone/name are copied from the chosen payment method so history survives edits and deletions.
create table payouts (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references profiles(id),
  amount_ugx         integer not null check (amount_ugx > 0),
  provider           text not null,
  phone_number       text not null,
  account_name       text not null,
  status             text not null default 'pending' check (status in ('pending','paid','rejected','cancelled')),
  requested_at       timestamptz not null default now(),
  processed_by       uuid references profiles(id),
  processed_at       timestamptz,
  provider_reference text,                          -- mobile money transaction id the admin enters
  rejection_reason   text,
  constraint paid_has_reference check (status <> 'paid' or provider_reference is not null)
);
create unique index payouts_one_pending_per_user on payouts (user_id) where status = 'pending';

create table sentences (
  id               bigint generated always as identity primary key,
  source_row       integer unique,
  clean_english    text,
  noisy_english    text not null,
  clean_lug        text,                            -- the validator's corrected Luganda translation
  noisy_lug        text,
  attempted        text not null,                   -- original attempted translation (never overwritten)
  length_words     integer not null,
  cost_raw         numeric(8,4) not null,
  cost             numeric(5,4) not null check (cost between 0 and 1),
  difficulty_score numeric(5,4) not null check (difficulty_score between 0 and 1),
  difficulty       text not null check (difficulty in ('easy','medium','hard')),

  -- soft lock while someone works on it (e.g. 15 min), so two people don't validate the same sentence
  claimed_by       uuid references profiles(id),
  claim_expires_at timestamptz,

  -- validation + payout. A cash-out takes every validated sentence with payout_id null.
  validated_by     uuid references profiles(id),
  validated_at     timestamptz,
  reward_ugx       integer check (reward_ugx >= 0),
  payout_id        uuid references payouts(id),     -- set on cash-out request; cleared if the payout is rejected/cancelled
  paid_status      boolean not null default false,  -- set true (with paid_at) when the payout is marked paid
  paid_at          timestamptz,

  verified         boolean not null default false,  -- second-pass linguistic/grammar check
  verified_by      uuid references profiles(id),
  verified_at      timestamptz,

  constraint validated_has_reward  check (validated_by is null or (reward_ugx is not null and clean_lug is not null)),
  constraint paid_requires_payout  check (paid_status = false or (validated_by is not null and payout_id is not null)),
  constraint verified_has_verifier check (verified = false or verified_by is not null)
);
create index on sentences (validated_by);
create index on sentences (payout_id);
-- fast "next sentence to validate": highest cost first among unvalidated
create index sentences_queue on sentences (cost desc) where validated_by is null;

-- Sentences a validator skipped; /work/next never serves them to that person again.
create table sentence_skips (
  user_id     uuid   not null references profiles(id) on delete cascade,
  sentence_id bigint not null references sentences(id) on delete cascade,
  skipped_at  timestamptz not null default now(),
  primary key (user_id, sentence_id)
);

-- What the profile page shows. Rejected/cancelled payouts release their sentences (payout_id = null), so they count as available again.
create view user_balances as
select p.id as user_id,
       coalesce(sum(s.reward_ugx), 0)                                         as total_earned_ugx,
       coalesce(sum(s.reward_ugx) filter (where s.payout_id is null), 0)       as available_ugx,
       coalesce(sum(s.reward_ugx) filter (where po.status = 'pending'), 0)     as pending_ugx,
       coalesce(sum(s.reward_ugx) filter (where po.status = 'paid'), 0)        as paid_out_ugx,
       count(s.id)                                                             as validated_count
from profiles p
left join sentences s  on s.validated_by = p.id
left join payouts   po on po.id = s.payout_id
group by p.id;

-- Security: Enable Row Level Security (RLS) on all tables with no policies.
-- Because FastAPI connects directly via a server-side connection, RLS blocks direct client queries via Supabase PostgREST Data API.
alter table profiles enable row level security;
alter table refresh_tokens enable row level security;
alter table password_reset_tokens enable row level security;
alter table payment_methods enable row level security;
alter table reward_settings enable row level security;
alter table payouts enable row level security;
alter table sentences enable row level security;
alter table sentence_skips enable row level security;

