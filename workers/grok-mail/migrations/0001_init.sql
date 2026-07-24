PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mailboxes (
  id TEXT PRIMARY KEY,
  address TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mailboxes_expires_at
  ON mailboxes (expires_at);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  mailbox_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  subject TEXT NOT NULL,
  text_body TEXT NOT NULL,
  html_body TEXT NOT NULL,
  raw_size INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (mailbox_id) REFERENCES mailboxes (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_mailbox_created
  ON messages (mailbox_id, created_at DESC);
