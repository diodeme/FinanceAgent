# FinanceAgent

Agentic harness for financial analysts: public-filings retrieval, cross-quarter
comparison tables with cell-level provenance. Interview take-home; design docs
in `docs/` (Chinese).

## Stack

- **AgentScope 2.0.10** (pinned) — Agent Service (FastAPI) + web UI
- **PostgreSQL 16 + pgvector** — AgentScope storage (`public`) + domain tables (`fin` schema, later)
- **Redis** — AgentScope message bus
- **DeepSeek** (OpenAI-compatible) as test LLM; swap via env later

## Run

```bash
cp .env.example .env   # fill in DEEPSEEK_API_KEY (and pg password)
docker compose up -d --build
```

Then:

1. Open http://localhost:8080
2. On the setup page enter server URL `http://localhost:8000` and any username
   (sent as `X-User-ID`; placeholder auth until JWT)

Seed a ready-to-use agent + session (needs `jq`):

```bash
./scripts/seed.sh
```

## Ports

| Service  | Address                  |
|----------|--------------------------|
| web UI   | http://localhost:8080    |
| API      | http://localhost:8000    |
| postgres | 127.0.0.1:5432           |
| redis    | 127.0.0.1:6379           |

## Known limitations (by design, for now)

- **Knowledge base disabled**: no embedding provider yet (DeepSeek has no
  embedding endpoint). The web UI's Knowledge tab is hidden; enable steps
  are documented in `backend/finagent/app.py` and `docs/数据结构.md` §8.

## Layout

```
backend/   AgentScope service wiring (thin adapter; domain logic added later)
webui/     fork of AgentScope examples/web_ui/frontend @v2.0.10 + nginx
db/        postgres init (fin schema)
docs/      设计.md / 数据结构.md
scripts/   seed.sh (credential/agent/session bootstrap)
```
