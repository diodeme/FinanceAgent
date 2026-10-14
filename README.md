# FinanceAgent

**English** | [简体中文](README.zh-CN.md)

A financial analysis assistant for SEC filings. Compare quarterly results, trace figures back to their sources, analyze original filings, and export tables and reports to Excel.

[Quick start](#quick-start) · [Usage](#usage) · [Common commands](#common-commands) · [Troubleshooting](#troubleshooting) · [Documentation](#documentation)

## Features

- **Quarterly comparisons** — retrieve reported financials and calculate metrics across companies and fiscal quarters.
- **Source tracing** — inspect the period, unit, filing, and calculation behind a result cell or report citation.
- **Filing analysis** — combine structured financial data with original SEC documents using a Docker-backed analysis agent and an editable analysis Skill.
- **Saved results and exports** — revisit completed results in chat and download tables and narrative reports as Excel files.

Built with **AgentScope 2.0.10**, **Python / FastAPI**, **React / TypeScript**, **PostgreSQL 16**, and **Redis**.

> [!WARNING]
> For trusted local development and demos only. `X-User-ID` is a placeholder identity mechanism, not secure authentication. The backend mounts the Docker socket, which grants Docker management privileges. UI and API ports are not restricted to loopback; do not expose this stack directly to the public internet.

## Quick start

### Prerequisites

- Git, Docker Engine, and Docker Compose v2, using Linux containers.
- On Windows, use WSL2 and ensure `docker compose` works inside WSL.
- Network access to SEC, your model provider, and the image/package registries needed for builds.
- A tool-calling model API credential. Development uses DeepSeek; configure your credential in the web UI.

### 1. Configure

```bash
git clone https://github.com/diodeme/FinanceAgent.git
cd FinanceAgent
cp .env.example .env
```

Edit `.env` using [`.env.example`](.env.example) as a reference:

| Variable | Configuration |
| --- | --- |
| `POSTGRES_USER` | Database user; the example value can be kept. |
| `POSTGRES_PASSWORD` | Replace the example password. It is interpolated into a connection URL, so avoid unencoded URL-special characters. |
| `POSTGRES_DB` | Database name; the example value can be kept. |
| `SEC_USER_AGENT` | Application name and a **real contact email**, e.g. `"FinanceAgent/0.1 your-name@your-domain.com"`. Replace the address with your own. |
| `CORS_ORIGINS` | Optional, comma-separated allowed origins. For local use, set `http://localhost:8080`. |
| `ANALYSIS_WORKSPACE_DIR` | Optional, defaults to `/tmp/financeagent-analysis`. See the path requirement below. |

> [!IMPORTANT]
> Analysis requires a Linux Docker daemon and access to `/var/run/docker.sock`. The analysis workspace must be the **same absolute Linux path, pointing to the same files**, on the Docker daemon host and inside the backend container. Keep the default unless you need to change it. Verify bind-mount behavior with Docker Desktop or a remote daemon; do not use a Windows path such as `E:\...`.

The `DEEPSEEK_*` variables are only used by the optional [`scripts/seed.sh`](scripts/seed.sh) development helper. They are not required for browser setup and do not automatically create UI credentials. The knowledge base is disabled; no embedding configuration is needed.

### 2. Start the services

```bash
docker compose up -d --build
docker compose ps
```

The backend applies database migrations automatically before starting the API. A fresh installation needs no manual table creation.

| Service | Address |
| --- | --- |
| Web UI | http://localhost:8080 |
| Backend API | http://localhost:8000 |
| PostgreSQL | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` |

Check backend readiness:

```bash
curl -H "X-User-ID: dev" http://localhost:8000/health
```

### 3. Connect a model

1. Open **http://localhost:8080**.
2. On the setup page, enter `http://localhost:8000` as the server URL and a username such as `dev`. Use the same username to return to your existing conversations.
3. In **Credentials**, add your model API credential. For DeepSeek, use `https://api.deepseek.com` as the Base URL.
4. Open chat, select the automatically provisioned **FinanceAgent**, create a conversation, and select the credential and an available model.
5. Send a question. The first analysis may take several minutes to prepare its Docker workspace image.

## Usage

Try these as successive turns in one conversation:

```text
What was Apple's revenue in its latest reported fiscal quarter?
Compare revenue and gross margin for Apple and Microsoft over the last four reported fiscal quarters.
Use the corresponding original filings to analyze the reasons for these changes.
Export the comparison table and analysis to Excel.
```

Click a result cell or report citation to inspect its source. Open an analysis card to read the report, and use the export card to download Excel files.

**Current coverage:** consolidated US-GAAP financials for AAPL, MSFT, NVDA, AMZN, GOOGL (with GOOG as an alias), and META; up to 8 fiscal quarters per request. Unsupported metrics and missing data are shown as gaps. There is no general web search or RAG pipeline.

**Customize the analysis:** edit a copy of [`financial-analysis/SKILL.md`](backend/finagent/skills/financial-analysis/SKILL.md), keeping the `financial-analysis` name. In the chat workspace's **Skills** panel, remove the old Skill and upload the edited folder. The next analysis uses the updated methodology without a restart; saved reports remain unchanged.

## Common commands

Run from the repository root:

| Task | Command |
| --- | --- |
| Check service status | `docker compose ps` |
| Follow backend logs | `docker compose logs -f backend` |
| Rebuild after code changes | `docker compose up -d --build` |
| Apply backend `.env` changes, when no analysis is running | `docker compose up -d --force-recreate backend` |
| Stop services, keeping data volumes | `docker compose down` |

> [!CAUTION]
> Database contents, conversations, and persistent files live in Docker volumes. `docker compose down -v` deletes those volumes. Keep `.env`, local data, and exported reports out of Git.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Backend does not start | Run `docker compose logs --tail=100 backend`. Check database configuration and migration errors. For an old, unversioned database, follow the [migration guide](docs/数据库迁移.md) (Chinese); do not delete its volume or force a migration stamp. |
| UI cannot connect | Check that the server URL is reachable **from your browser**. A browser on another machine cannot use your server's `localhost`. Check `CORS_ORIGINS` as well. |
| SEC filing download fails | Set a real contact email in `SEC_USER_AGENT`, then recreate the backend. Placeholder addresses are rejected; a compliant User-Agent does not guarantee access or prevent HTTP 403 responses. |
| Analysis workspace fails or starts slowly | Check Docker socket access and the shared absolute workspace path. The first image build needs network access and can take several minutes. |

## Project layout

```text
backend/   API, financial tools, analysis agent, migrations, and tests
webui/     React frontend, nginx configuration, and browser acceptance tests
db/        PostgreSQL initialization
scripts/   Optional development setup helper
docs/      Design, data model, migrations, and acceptance records
```

## Documentation

The detailed project documents are currently in Chinese:

- [Design](docs/设计.md)
- [Data model](docs/数据结构.md)
- [Database migrations](docs/数据库迁移.md)
- [Acceptance records](docs/验收.md)
