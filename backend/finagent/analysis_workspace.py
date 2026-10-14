"""Run-owned integration with the original AgentScope Docker workspace lifecycle."""

import asyncio
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from agentscope.app.workspace_manager import DockerWorkspaceManager

from .contracts import FinanceError

WORKSPACE_SETUP_SECONDS = 900
ANALYSIS_WORKSPACE_DIR = os.getenv("ANALYSIS_WORKSPACE_DIR", "/tmp/financeagent-analysis")
_service = None


class AnalysisWorkspaces:
    def __init__(self, manager, basedir: str):
        self.manager = manager
        self.root = Path(basedir).absolute()

    @asynccontextmanager
    async def open(self, *, user_id: str, agent_id: str, session_id: str):
        # Never accept a caller-provided workspace id or reuse another run's state.
        workspace_id = "finance-analysis-" + uuid.uuid4().hex
        path = self.root / workspace_id
        # Reserve the directory ourselves to avoid the manager's legacy-path fallback.
        path.mkdir(parents=True, exist_ok=False)
        try:
            async with asyncio.timeout(WORKSPACE_SETUP_SECONDS):
                workspace = await self.manager.get_workspace(
                    user_id=user_id, agent_id=agent_id, session_id=session_id,
                    workspace_id=workspace_id,
                )
            yield workspace
        finally:
            async def cleanup():
                await self.manager.close(workspace_id)
                # close() owns container cleanup, but deliberately leaves host files.
                # Delete only this generated, reserved directory, never the base.
                if path.is_dir() and not path.is_symlink() and path.parent == self.root:
                    await asyncio.to_thread(shutil.rmtree, path)
            closing = asyncio.create_task(cleanup(), name=f"analysis-close:{workspace_id}")
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await closing
                raise


def get_analysis_workspaces() -> AnalysisWorkspaces:
    if _service is None:
        raise FinanceError("workspace_unavailable", "The native analysis workspace manager is not running.")
    return _service


@asynccontextmanager
async def analysis_workspace_lifespan():
    global _service
    if not Path(ANALYSIS_WORKSPACE_DIR).is_absolute():
        raise ValueError("ANALYSIS_WORKSPACE_DIR must be an absolute same-path Docker bind mount.")
    async with DockerWorkspaceManager(
        basedir=ANALYSIS_WORKSPACE_DIR, env={}, default_mcps=[], skill_paths=[],
    ) as manager:
        _service = AnalysisWorkspaces(manager, ANALYSIS_WORKSPACE_DIR)
        try:
            yield _service
        finally:
            _service = None
