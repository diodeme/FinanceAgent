"""Owner-scoped snapshots of the original workspace's native analysis skill."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from agentscope.skill import Skill

from .contracts import FinanceError

SKILL_NAME = "financial-analysis"
BUNDLED_SKILL_DIR = Path(__file__).parent / "skills" / SKILL_NAME
_service = None


class AnalysisSkills:
    def __init__(self, workspace_service, skill_path=BUNDLED_SKILL_DIR):
        self.workspace_service = workspace_service
        self.skill_path = str(skill_path)
        self._seed_lock = asyncio.Lock()

    async def ensure_analysis_skill(self, *, user_id: str, agent_id: str, session_id: str) -> None:
        """Seed once on a verified finance reply, preserving edits and removals."""
        workspace = await self.workspace_service.resolve(user_id, agent_id, session_id)
        backend = workspace.get_backend()
        # The authorized native workspace supplies the root, including custom
        # session bindings. Hash only the marker name, never infer a user path.
        marker = backend.join_path(
            workspace.workdir, ".financeagent", "skill-initialized",
            hashlib.sha256(agent_id.encode("utf-8")).hexdigest(),
        )
        async with self._seed_lock:
            try:
                await backend.read_file(marker)
                return
            except FileNotFoundError:
                pass
            skills = await workspace.list_skills(agent_id=agent_id)
            if not any(skill.name == SKILL_NAME for skill in skills):
                await workspace.add_skill(self.skill_path, agent_id=agent_id)
            # Outside the skill directory: removing the skill must not seed it again.
            await backend.write_file(marker, b"financial-analysis initialized\n")

    async def load_analysis_skill(self, *, user_id: str, agent_id: str, session_id: str) -> Skill | None:
        """Resolve and reread every analysis; no seed or cached user content here."""
        workspace = await self.workspace_service.resolve(user_id, agent_id, session_id)
        for skill in await workspace.list_skills(agent_id=agent_id):
            if skill.name == SKILL_NAME:
                # Strings are immutable. A separate native value prevents later
                # workspace edits/mutations from changing an in-flight snapshot.
                return Skill(name=skill.name, description=skill.description, dir=skill.dir,
                             markdown=skill.markdown, updated_at=skill.updated_at)
        return None


def get_analysis_skills() -> AnalysisSkills:
    if _service is None:
        raise FinanceError("workspace_unavailable", "The native analysis skill source is not running.")
    return _service


async def ensure_analysis_skill(*, user_id: str, agent_id: str, session_id: str) -> None:
    await get_analysis_skills().ensure_analysis_skill(
        user_id=user_id, agent_id=agent_id, session_id=session_id,
    )


async def load_analysis_skill(*, user_id: str, agent_id: str, session_id: str) -> Skill | None:
    return await get_analysis_skills().load_analysis_skill(
        user_id=user_id, agent_id=agent_id, session_id=session_id,
    )


@asynccontextmanager
async def analysis_skills_lifespan(workspace_service):
    global _service
    _service = AnalysisSkills(workspace_service)
    try:
        yield _service
    finally:
        _service = None
