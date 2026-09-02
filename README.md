# GoRules JDM Studio

A web studio for authoring, testing and versioning decision graphs for the
[GoRules Zen](https://gorules.io) business rule engine — with an AI assistant
that can create and modify them.

- **Author** graphs by hand on a full JDM canvas, or upload existing ones.
- **Ask** the assistant to build or change a policy; review its proposal as a
  diff on the canvas and accept or reject it.
- **Simulate** a single payload with per-node execution traces.
- **Test** against a saved suite with a real pass/fail verdict.
- **Version** every change, restore any point in history.
- **Download** the graph, the test suite, or both as a bundle.

## Running it

```bash
docker compose up --build
```

- UI: <http://localhost:3000>
- API: <http://localhost:8000> (OpenAPI docs at `/docs`)

The two policies in `backend/jdm_graphs/` are imported into the database on
first boot. The first load of the editor route compiles ~17k modules and takes
around 20 seconds; subsequent loads are instant.

The database lives in the `studio_data` named volume, so it survives
`docker compose down`. To start completely fresh:

```bash
docker compose down -v
```

### Without Docker

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload            # from the repository root
```

```bash
cd frontend && npm install && npm run dev
```

The browser talks to the API **directly** rather than through Next's rewrite,
because that proxy buffers responses and would defeat the assistant's live
progress stream. Point it with `NEXT_PUBLIC_API_URL` (see `frontend/.env.local`)
and allow the origin with `CORS_ORIGINS` on the backend.

## Configuration

LLM credentials live in `backend/.env`. Pick a provider with `LLM_PROVIDER`:

| Provider | Variables |
|---|---|
| `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL_NAME`, `OPENROUTER_BASE_URL`, `OPENROUTER_REASONING_ENABLED` |
| `huggingface` | `HF_TOKEN`, `HF_MODEL_NAME`, `HF_INFERENCE_PROVIDER` (routed through HF, so this must be an HF token) |
| `gemini` | `GOOGLE_AI_API_KEY`, `GOOGLE_MODEL_NAME` |
| `litellm` | `LITELLM_API_KEY`, `LITELLM_BASE_URL`, `LITELLM_MODEL_NAME` |

Other settings (all optional): `DB_PATH`, `CORS_ORIGINS`, `LLM_TIMEOUT`
(default 120s per call), `AGENT_RUN_TIMEOUT` (default 900s per run),
`RATE_LIMIT_DEFAULT`, `DEBUG_DIR`.

If no LLM is configured the studio still runs — the editor, simulator, test
runner and import/export do not need one; only the assistant does.

## How it works

```
browser ──REST──▶ FastAPI ──▶ SQLite (graphs, versions, tests, chat)
        ──SSE───▶ /api/chat  ──▶ LangGraph agent (checkpoints in the same file)
                  /api/simulate ▶ zen-engine
```

The agent is a LangGraph state machine. The UI owns policy selection, so a run
starts from the user's message and an `intent_router_node` sends it to one of
create / modify / test / explain. Approval gates pause the graph with
`interrupt()`; the UI renders those as chips and echoes the chosen label back
verbatim.

Long runs never block a request: `POST /messages` returns `202` and the work
continues in the background, streaming `node_start` / `progress` / `interrupt`
events over SSE. Every event is persisted with a sequence number, so a reload or
a dropped connection replays from `?from_seq=` instead of losing the run. The
LangGraph checkpointer is SQLite, so a paused conversation survives a restart.

### A note on the bundled example policies

`RefundPolicy` and `LoanApprovalPolicy` **fail every one of their own tests**,
and the studio shows that. They produce none of their declared output fields
(`decision`, `riskTier`, `refundType`, …).

This is pre-existing breakage that the previous evaluator hid: it reported
success for any evaluation that did not raise, and delegated the pass/fail
verdict to an LLM. The test runner now compares actual output against
`expectedOutput`, which is also what lets the agent's build loop retry until the
logic is genuinely correct rather than merely non-crashing.

## Development

```bash
pytest backend/tests -q          # agent flow, chat API, SSE, proposals
cd frontend && npx tsc --noEmit  # typecheck
cd frontend && npm run build
```

`legacy/streamlit_app.py` is the original prototype, kept as a protocol
reference. It is unmaintained and does not run against the refactored agent.

## Layout

```
backend/
  main.py            FastAPI app
  config.py          settings
  api/               routes: graphs, versions, import, export, simulate, tests, chat
  db/                schema, connection, DAO, first-boot import
  services/          chat runner, event bus, test generation
  tools/             zen evaluation, markdown DSL parser
  prompts/           node prompts and JDM domain knowledge
  lang_graph_agent.py  the agent
frontend/
  app/               routes
  components/        shell, editor, chat, UI primitives
  lib/               API client, SSE, theme bridge, downloads
  stores/            zustand state
```
