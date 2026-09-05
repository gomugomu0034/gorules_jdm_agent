-- Training-corpus schema.
--
-- Deliberately a separate database file from `studio.db`. Three reasons, in order of
-- weight:
--
--   1. `chat_events` is ON DELETE CASCADE from `chat_threads`, so the studio's own record
--      of a run is destroyed when a user tidies up their chat list. A corpus accumulated
--      over months cannot hang off a row the UI invites people to delete.
--   2. It is the unit you hand to a training run, so it wants to be one file to copy.
--   3. It can be written with plain synchronous sqlite3 from the worker thread the LLM
--      call already runs on - no event loop, no contention with the app's aiosqlite pool.
--
-- The cost is no foreign keys across the boundary: run/thread/graph ids are opaque
-- strings here. That is the right trade. A corpus must not break because the graph it
-- describes was deleted.
--
-- Applied with executescript() on every boot, so every statement is idempotent. Later
-- phases add their tables to this same file.

PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

-- One row per agent turn. Opened when the turn starts and completed when it ends, so a
-- run left with outcome NULL is one whose process died mid-turn - itself a signal worth
-- keeping rather than repairing.
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  thread_id      TEXT NOT NULL,
  graph_id       TEXT,
  -- Hashed, never raw: a guest owner is `guest:<secrets.token_urlsafe(16)>`, which is the
  -- visitor's identity. The corpus needs to tell runs apart, not to know whose they were.
  owner_hash     TEXT NOT NULL DEFAULT '',
  intent         TEXT NOT NULL DEFAULT '',   -- CREATE | MODIFY | TEST | EXPLAIN | LINT
  mode           TEXT NOT NULL DEFAULT '',   -- NEW | EXISTING
  outcome        TEXT,                       -- completed | error | cancelled | NULL if in flight
  app_version    TEXT NOT NULL DEFAULT '',
  git_sha        TEXT NOT NULL DEFAULT '',
  final_jdm_hash TEXT,                       -- the artifact the turn settled on
  started_at     TEXT NOT NULL,
  ended_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_thread  ON runs(thread_id);

-- System prompts, content-addressed.
--
-- PROMPT_PLANNER renders to 46KB and PROMPT_BUILDER to 32KB, and a single turn can spend
-- five calls. Stored inline that is megabytes of duplication per hundred runs. Stored by
-- hash it is one row - and the hash doubles as the prompt's version, which is what lets a
-- training set be filtered to one instruction set instead of silently mixing several.
CREATE TABLE IF NOT EXISTS prompts (
  hash          TEXT PRIMARY KEY,            -- sha256 of `text`
  kind          TEXT NOT NULL DEFAULT '',    -- planner | builder | triage | patch | ...
  text          TEXT NOT NULL,
  chars         INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL
);

-- One row per model call. This is the training unit; everything else exists to group it
-- or to score it.
--
-- Columns a given provider does not report stay NULL rather than being faked: `cost` and
-- `upstream_provider` only come back from OpenRouter, and `usage` is optional in the
-- OpenAI schema, so several endpoints behind the Hugging Face router omit it entirely.
CREATE TABLE IF NOT EXISTS samples (
  sample_id         TEXT PRIMARY KEY,
  run_id            TEXT,                    -- NULL for a call made outside an agent turn
  seq               INTEGER NOT NULL DEFAULT 0,
  node              TEXT NOT NULL,           -- the attribution that did not exist before
  attempt           INTEGER NOT NULL DEFAULT 1,
  purpose           TEXT NOT NULL DEFAULT '',
  prompt_hash       TEXT,                    -- -> prompts.hash
  messages_json     TEXT NOT NULL,           -- the request, system prompt excluded
  completion        TEXT,                    -- raw, before extraction strips it
  reasoning_json    TEXT,                    -- the model's thinking, when the provider returns it
  provider          TEXT NOT NULL DEFAULT '',   -- the gateway called: openrouter, gemini, ...
  upstream_provider TEXT,                    -- who actually ran it behind a gateway
  model_requested   TEXT NOT NULL DEFAULT '',
  model_served      TEXT,                    -- differs under OpenRouter routing
  temperature       REAL,
  reasoning_enabled INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  reasoning_tokens  INTEGER,
  generation_id     TEXT,
  -- What the call actually cost, when the provider says. OpenRouter returns this in the
  -- response body; the others do not report cost at all and leave it NULL.
  cost              REAL,
  latency_ms        INTEGER,
  error             TEXT,                    -- set when the call raised; completion is then NULL
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_run     ON samples(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_samples_node    ON samples(node, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_samples_prompt  ON samples(prompt_hash);

-- What the deterministic tools decided about a model's output.
--
-- This is what turns a run from one training example into several with a machine-checkable
-- verdict on each. The builder's repair loop produces a failing attempt and then a passing
-- one against the same task; kept apart with the reason each was rejected, that is a
-- preference pair with no human labelling in it.
--
-- `sample_id` is the model call being judged, which is not always the call made in the
-- same breath: the builder's first attempt validates the *planner's* DSL without asking
-- the model anything, so its verdicts attribute to the planner's sample. That falls out of
-- pointing at whichever sample the run produced most recently.
--
-- `diagnostics_json` holds structured `Diagnostic` records rather than `format_for_llm`'s
-- prose. The prose is written for the model to read; the structure is what a corpus can be
-- filtered and counted on.
CREATE TABLE IF NOT EXISTS tool_results (
  tool_result_id   TEXT PRIMARY KEY,
  run_id           TEXT,
  sample_id        TEXT,                     -- -> samples.sample_id
  seq              INTEGER NOT NULL DEFAULT 0,
  node             TEXT NOT NULL,
  attempt          INTEGER NOT NULL DEFAULT 1,
  -- extract_plan | parse_dsl | check_format | lint | run_tests | apply_patch
  tool             TEXT NOT NULL,
  ok               INTEGER NOT NULL,
  diagnostics_json TEXT,
  output_json      TEXT,                     -- test summary, patch log, counts
  error            TEXT,
  duration_ms      INTEGER,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_results_run    ON tool_results(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tool_results_sample ON tool_results(sample_id);
CREATE INDEX IF NOT EXISTS idx_tool_results_tool   ON tool_results(tool, ok);
