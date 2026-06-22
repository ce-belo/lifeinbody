-- Life in Body tracker schema. Populated in Step 3.
-- Kept here in Step 1 so the layout matches the spec.

CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY,
  sender_email TEXT,
  sender_name TEXT,
  subject TEXT,
  last_message_at TIMESTAMP,
  last_message_from TEXT,        -- 'us' | 'them'
  status TEXT,                   -- 'new' | 'replied' | 'waiting' | 'closed'
  needs_followup INTEGER DEFAULT 0,
  followup_reason TEXT,
  message_count INTEGER,
  last_synced_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  thread_id TEXT REFERENCES threads(thread_id),
  sent_at TIMESTAMP,
  from_email TEXT,
  to_emails TEXT,
  snippet TEXT,
  body_plain TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT REFERENCES threads(thread_id),
  detected_amount REAL,
  detected_currency TEXT,
  detected_client TEXT,
  status TEXT,                   -- 'mentioned' | 'sent' | 'paid' | 'overdue' | 'followup_needed'
  notes TEXT,
  detected_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY,     -- Gmail draft id
  thread_id TEXT REFERENCES threads(thread_id),
  created_at TIMESTAMP,
  approved INTEGER DEFAULT 0
);
