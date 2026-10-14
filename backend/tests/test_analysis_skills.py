"""Offline native workspace skill tests; no model, network, or live storage."""

import asyncio
import io
import tempfile
import zipfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from agentscope.app._service._workspace import WorkspaceService
from agentscope.skill import Skill
from agentscope.tool import Toolkit
from agentscope.workspace import LocalWorkspace

from finagent import analysis_skills
from finagent.analysis_skills import AnalysisSkills, SKILL_NAME
from finagent.contracts import AnalyzeRequest, FinanceError
from finagent.finance_middleware import FinanceMiddleware, finance_factory
from finagent.tools import FinanceTools, REQUESTS, ToolContext
from tests.test_finance_runtime import FakeRepo


class AnalysisSkillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bundle = root / "bundled"
        self.bundle.mkdir()
        self.markdown = "---\nname: financial-analysis\ndescription: Analysis method\n---\nOriginal method.\n"
        (self.bundle / "SKILL.md").write_text(self.markdown, encoding="utf-8")
        self.original = LocalWorkspace(workdir=str(root / "custom-bound-workspace"))
        self.other = LocalWorkspace(workdir=str(root / "other-owner-workspace"))
        await self.original.initialize()
        await self.other.initialize()
        self.addAsyncCleanup(self.original.close)
        self.addAsyncCleanup(self.other.close)
        records = {("u", "a", "s"): "custom", ("u", "a", "s2"): "custom",
                   ("u", "b", "s"): "custom", ("v", "a", "s"): "other"}

        async def get_session(user_id, agent_id, session_id):
            binding = records.get((user_id, agent_id, session_id))
            return SimpleNamespace(config=SimpleNamespace(workspace_id=binding)) if binding else None

        async def get_workspace(user_id, agent_id, session_id, workspace_id):
            return {"custom": self.original, "other": self.other}[workspace_id]

        self.storage = SimpleNamespace(get_session=AsyncMock(side_effect=get_session))
        self.manager = SimpleNamespace(get_workspace=AsyncMock(side_effect=get_workspace))
        self.native = WorkspaceService(self.storage, self.manager, "offline-test-secret")
        self.source = AnalysisSkills(self.native, self.bundle)
        self.scope = dict(user_id="u", agent_id="a", session_id="s")

    async def test_seed_once_and_copy_fresh_native_snapshot(self):
        await asyncio.gather(*(self.source.ensure_analysis_skill(**self.scope) for _ in range(3)))
        native_skills = await self.original.list_skills(agent_id="a")
        self.assertEqual([s.name for s in native_skills], [SKILL_NAME])
        first = await self.source.load_analysis_skill(**self.scope)
        self.assertIsInstance(first, Skill)
        self.assertEqual(first.markdown, "Original method.")
        # Native list returns native values, not objects retained by our source.
        with patch.object(self.original, "list_skills", AsyncMock(return_value=native_skills)):
            snapshot = await self.source.load_analysis_skill(**self.scope)
        self.assertIsNot(snapshot, native_skills[0])
        native_skills[0].markdown = "mutation after snapshot"
        self.assertEqual(snapshot.markdown, "Original method.")
        edited = self.markdown.replace("Original method.", "Owner's revised method.")
        await self.original.get_backend().write_file(str(Path(first.dir) / "SKILL.md"), edited.encode())
        second = await self.source.load_analysis_skill(**self.scope)
        self.assertEqual(second.markdown, "Owner's revised method.")
        self.assertEqual(first.markdown, "Original method.")
        self.assertIsNot(first, second)

    async def test_native_remove_and_upload_edited_archive_changes_next_snapshot(self):
        await self.source.ensure_analysis_skill(**self.scope)
        old = await self.source.load_analysis_skill(**self.scope)
        archive = io.BytesIO()
        edited = self.markdown.replace("Original method.", "Updated analyst method: emphasize liquidity.")
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("financial-analysis/SKILL.md", edited)
        async def stream():
            yield archive.getvalue()
        await self.original.remove_skill(SKILL_NAME, agent_id="a")
        await self.original.add_skill_archive(stream(), "zip", SKILL_NAME, agent_id="a")
        await self.source.ensure_analysis_skill(**self.scope)
        new = await self.source.load_analysis_skill(**self.scope)
        self.assertEqual(new.markdown, "Updated analyst method: emphasize liquidity.")
        self.assertEqual(old.markdown, "Original method.")
        self.assertEqual([skill.name for skill in await self.original.list_skills(agent_id="a")], [SKILL_NAME])

    async def test_existing_edited_skill_is_not_overwritten_or_suffixed(self):
        await self.original.add_skill(str(self.bundle), agent_id="a")
        existing = (await self.original.list_skills(agent_id="a"))[0]
        edited = self.markdown.replace("Original method.", "Existing deployed user's edits.")
        await self.original.get_backend().write_file(str(Path(existing.dir) / "SKILL.md"), edited.encode())
        with patch.object(self.original, "add_skill", wraps=self.original.add_skill) as add:
            await self.source.ensure_analysis_skill(**self.scope)
            add.assert_not_called()
        self.assertEqual((await self.source.load_analysis_skill(**self.scope)).markdown, "Existing deployed user's edits.")
        self.assertEqual(len(await self.original.list_skills(agent_id="a")), 1)

    async def test_removal_survives_new_source_and_workspace_instance(self):
        await self.source.ensure_analysis_skill(**self.scope)
        await self.original.remove_skill(SKILL_NAME, agent_id="a")
        reloaded = LocalWorkspace(workdir=self.original.workdir)
        await reloaded.initialize()
        self.addAsyncCleanup(reloaded.close)
        source = AnalysisSkills(SimpleNamespace(resolve=AsyncMock(return_value=reloaded)), self.bundle)
        await source.ensure_analysis_skill(**self.scope)
        self.assertIsNone(await source.load_analysis_skill(**self.scope))
        markers = list(Path(reloaded.workdir).glob(".financeagent/skill-initialized/*"))
        self.assertEqual(len(markers), 1)

    async def test_agent_markers_and_skill_partitions_are_independent(self):
        await self.source.ensure_analysis_skill(**self.scope)
        other_agent = dict(self.scope, agent_id="b")
        self.assertIsNone(await self.source.load_analysis_skill(**other_agent))
        await self.original.remove_skill(SKILL_NAME, agent_id="a")
        await self.source.ensure_analysis_skill(**other_agent)
        await self.source.ensure_analysis_skill(**self.scope)
        self.assertIsNone(await self.source.load_analysis_skill(**self.scope))
        self.assertIsNotNone(await self.source.load_analysis_skill(**other_agent))
        self.assertEqual(len(list(Path(self.original.workdir).glob(".financeagent/skill-initialized/*"))), 2)

    async def test_owner_and_custom_session_binding_use_native_resolve(self):
        await self.source.ensure_analysis_skill(**self.scope)
        self.manager.get_workspace.assert_awaited_with("u", "a", "s", "custom")
        first = await self.source.load_analysis_skill(**self.scope)
        second = await self.source.load_analysis_skill(**dict(self.scope, session_id="s2"))
        self.assertEqual(first.dir, second.dir)
        self.manager.get_workspace.assert_awaited_with("u", "a", "s2", "custom")
        other_owner = dict(self.scope, user_id="v")
        self.assertIsNone(await self.source.load_analysis_skill(**other_owner))
        await self.source.ensure_analysis_skill(**other_owner)
        self.assertNotEqual(first.dir, (await self.source.load_analysis_skill(**other_owner)).dir)
        for action in (self.source.ensure_analysis_skill, self.source.load_analysis_skill):
            with self.assertRaises(HTTPException) as error:
                await action(**dict(self.scope, user_id="unauthorized"))
            self.assertEqual(error.exception.status_code, 404)

    async def test_absent_skill_load_does_not_seed(self):
        self.assertIsNone(await self.source.load_analysis_skill(**self.scope))
        self.assertFalse((Path(self.original.workdir) / ".financeagent").exists())

    async def test_failed_seed_does_not_mark_initialized(self):
        source = AnalysisSkills(self.native, Path(self.temp.name) / "missing-bundle")
        with self.assertRaises(ValueError):
            await source.ensure_analysis_skill(**self.scope)
        self.assertFalse(list(Path(self.original.workdir).glob(".financeagent/skill-initialized/*")))
        await self.source.ensure_analysis_skill(**self.scope)
        self.assertIsNotNone(await self.source.load_analysis_skill(**self.scope))

    async def test_lifecycle_unavailable_is_explicit_not_missing_skill(self):
        with patch.object(analysis_skills, "_service", None):
            with self.assertRaises(FinanceError) as error:
                await analysis_skills.load_analysis_skill(**self.scope)
            self.assertEqual(error.exception.code, "workspace_unavailable")
            async with analysis_skills.analysis_skills_lifespan(self.native) as source:
                self.assertIs(analysis_skills.get_analysis_skills(), source)
                self.assertIsNone(await analysis_skills.load_analysis_skill(**self.scope))
            with self.assertRaises(FinanceError):
                await analysis_skills.ensure_analysis_skill(**self.scope)

    async def test_reply_initializes_skill_but_only_advertises_business_tools(self):
        middleware = FinanceMiddleware("u", "a", "s", repo=None, financials=None,
                                       calculations=None, reports=None, registry=None,
                                       skill_source=self.source)
        agent = SimpleNamespace(state=SimpleNamespace(session_id="s"), model=None, toolkit=Toolkit())

        async def reply(**kwargs):
            yield "reply"

        self.assertEqual([event async for event in middleware.on_reply(agent, {}, reply)], ["reply"])
        self.assertIsNotNone(await self.source.load_analysis_skill(**self.scope))
        self.assertIsNone(await agent.toolkit.get_tool("Skill"))
        self.assertEqual({tool.name for tool in middleware.runtime.tools}, set(REQUESTS))
        agent.state.session_id = "wrong"
        with patch.object(self.source, "ensure_analysis_skill", AsyncMock()) as ensure:
            with self.assertRaises(FinanceError):
                _ = [event async for event in middleware.on_reply(agent, {}, reply)]
            ensure.assert_not_called()

    async def test_production_factory_injects_source_without_seeding_ordinary_agents(self):
        with patch("finagent.repository.is_finance_agent", AsyncMock(return_value=True)):
            middlewares = await finance_factory("u", "a", "s")
        self.assertIs(middlewares[0].runtime.skill_source, analysis_skills)
        with patch("finagent.repository.is_finance_agent", AsyncMock(return_value=False)):
            middlewares = await finance_factory("u", "ordinary", "s")
        self.assertEqual(len(middlewares), 1)
        self.assertNotIsInstance(middlewares[0], FinanceMiddleware)

    async def test_runtime_passes_new_snapshot_on_each_analysis(self):
        repo = FakeRepo()
        runtime = FinanceTools("u", "a", "s", repo=repo, financials=None,
                               calculations=None, reports=None, skill_source=self.source)
        context = ToolContext("u", "a", "s", "reply", "call", "data_analyze", "run")
        request = AnalyzeRequest(data_ids=["input"], question="Analyze the saved revenue.")
        await self.source.ensure_analysis_skill(**self.scope)
        result = {"text": "Offline report", "warnings": []}
        with patch("finagent.tools.analyze_data", AsyncMock(return_value=result)) as analyze:
            await runtime.execute("data_analyze", request, context, AsyncMock())
            snapshot = analyze.await_args.kwargs["skill"]
            self.assertEqual(snapshot.markdown, "Original method.")
            await self.original.remove_skill(SKILL_NAME, agent_id="a")
            await runtime.execute("data_analyze", request, context, AsyncMock())
            self.assertIsNone(analyze.await_args.kwargs["skill"])
            self.assertEqual(snapshot.markdown, "Original method.")


if __name__ == "__main__":
    unittest.main()
