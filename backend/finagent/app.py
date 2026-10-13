# -*- coding: utf-8 -*-
"""FinanceAgent backend: AgentScope Agent Service wiring.

Thin adapter layer only. All domain logic (EDGAR ingestion, registries,
fact table, report tooling) lives in plain Python modules and is attached
later via tools / middlewares. See docs/设计.md and docs/数据结构.md.
"""

import logging
import os

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware import Middleware

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from .routes import fin_router
from .runsteps import runsteps_factory

logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/data/workspaces")

# The web UI is a static SPA talking to this API from the browser
# (user enters the server URL on the setup page), so CORS must be open.
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

app = create_app(
    storage=AsyncSQLAlchemyStorage(
        DATABASE_URL,
        # Alembic packaged with agentscope brings the schema up to date
        # on boot. Single-node dev deployment; fine per upstream docs.
        create_tables=False,
        auto_migrate=True,
    ),
    message_bus=RedisMessageBus(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
    ),
    workspace_manager=LocalWorkspaceManager(basedir=WORKSPACE_DIR),
    # ── Knowledge base: DISABLED by decision (2026-10-13) ──────────────
    # No embedding provider yet (DeepSeek has no embedding endpoint).
    # To enable later:
    #   1. pick an embedding provider (DashScope text-embedding-v4 is
    #      first-class in AgentScope; SiliconFlow bge-m3 via
    #      OpenAICredential+base_url also works), store the key as a
    #      credential via POST /credential/
    #   2. implement finagent.pgvector_store.PgVectorStore
    #      (VectorStoreBase → fin schema tables)
    #   3. pass here:
    #        knowledge_base_manager=CollectionPerKbManager(
    #            storage=<AsyncSQLAlchemyStorage>,
    #            vector_store=PgVectorStore(...),
    #        ),
    #        blob_store=LocalFilesystemBlobStore(...),   # doc uploads
    #   4. re-enable the /knowledge nav item in
    #      webui/frontend/src/components/layout/AppSidebar.tsx
    # KB embedding models are resolved per-KB from stored credentials
    # (EmbeddingModelConfig: type + credential_id + model + dimensions).
    # fin.run_steps logging on every LLM/tool call (framework plumbing;
    # run_id stays NULL until the orchestrator business logic sets it)
    extra_agent_middlewares=runsteps_factory,
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in CORS_ORIGINS.split(",")],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ],
    title="FinanceAgent",
)

# Our own routes on the same app — one origin, no extra service needed.
app.include_router(fin_router)
