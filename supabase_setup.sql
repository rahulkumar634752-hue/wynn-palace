-- Run this in Supabase SQL Editor

CREATE TABLE wynn_settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- No RLS needed (server-side only access via service key)
