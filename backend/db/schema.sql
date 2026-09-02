-- GoRules JDM Studio schema.
-- LangGraph's AsyncSqliteSaver creates its own `checkpoints`/`writes` tables in
-- this same file; the names below do not collide with them.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS graphs (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL UNIQUE,
  description     TEXT NOT NULL DEFAULT '',
  current_version INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  archived_at     TEXT
);

CREATE TABLE IF NOT EXISTS graph_versions (
  graph_id    TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  content     TEXT NOT NULL,                 -- JDM JSON
  message     TEXT NOT NULL DEFAULT '',
  author      TEXT NOT NULL DEFAULT 'user',  -- user | agent | import
  is_autosave INTEGER NOT NULL DEFAULT 0,
  thread_id   TEXT,                          -- provenance when author='agent'
  created_at  TEXT NOT NULL,
  PRIMARY KEY (graph_id, version)
);
CREATE INDEX IF NOT EXISTS idx_versions_graph ON graph_versions(graph_id, version DESC);

CREATE TABLE IF NOT EXISTS test_cases (
  id            TEXT PRIMARY KEY,
  graph_id      TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  input_json    TEXT NOT NULL,
  expected_json TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tests_graph ON test_cases(graph_id, sort_order);

CREATE TABLE IF NOT EXISTS test_runs (
  id           TEXT PRIMARY KEY,
  graph_id     TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  version      INTEGER,
  summary_json TEXT NOT NULL,
  results_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_graph ON test_runs(graph_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_threads (
  id         TEXT PRIMARY KEY,               -- == LangGraph thread_id
  graph_id   TEXT REFERENCES graphs(id) ON DELETE SET NULL,
  title      TEXT NOT NULL DEFAULT 'New chat',
  status     TEXT NOT NULL DEFAULT 'idle',   -- idle | running | awaiting_input | error
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_events (
  thread_id  TEXT NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
  seq        INTEGER NOT NULL,
  run_id     TEXT NOT NULL,
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, seq)
);

CREATE TABLE IF NOT EXISTS proposals (
  thread_id    TEXT PRIMARY KEY REFERENCES chat_threads(id) ON DELETE CASCADE,
  graph_id     TEXT,
  jdm_json     TEXT NOT NULL,
  tests_json   TEXT NOT NULL,
  usecase_name TEXT NOT NULL,
  base_version INTEGER,
  report_json  TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
