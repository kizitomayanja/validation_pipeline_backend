-- Migration: Create Support Tickets and Messages Tables
-- Target: Supabase SQL Editor / PostgreSQL
-- Purpose: Ensures support_tickets and ticket_messages tables exist with required indexes.

CREATE TABLE IF NOT EXISTS support_tickets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_num  serial UNIQUE,
  author_id   uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  category    text NOT NULL CHECK (category IN ('clinical_terminology', 'review_dispute', 'payout_issue', 'role_request', 'general')),
  target_role text NOT NULL CHECK (target_role IN ('reviewer', 'admin')),
  subject     text NOT NULL,
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved')),
  sentence_id bigint REFERENCES sentences(id) ON DELETE SET NULL,
  payout_id   uuid REFERENCES payouts(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS support_tickets_author_idx ON support_tickets (author_id);
CREATE INDEX IF NOT EXISTS support_tickets_target_role_idx ON support_tickets (target_role, status);

CREATE TABLE IF NOT EXISTS ticket_messages (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id  uuid NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
  sender_id  uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  message    text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ticket_messages_ticket_idx ON ticket_messages (ticket_id, created_at ASC);
