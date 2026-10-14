"""Opt-in native Docker integration; scripted model, no provider/SEC/database calls.

Run with FIN_TEST_NATIVE_DOCKER=1 and mount the Docker socket plus the same
absolute FIN_TEST_WORKSPACE_DIR on both the test runner and daemon host.
"""

import asyncio
import json
import os
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiodocker
from agentscope.app.workspace_manager import DockerWorkspaceManager
from agentscope.message import ToolCallBlock
from agentscope.skill import Skill

from finagent.analysis import analyze_data, document_citation_url
from finagent.analysis_workspace import AnalysisWorkspaces
from finagent.contracts import AnalyzeRequest
from tests.test_analyst import RAW, ScriptModel, document, response


class ObservedWorkspaces:
    def __init__(self, service, docker):
        self.service = service
        self.docker = docker
        self.paths = []
        self.container_ids = []

    @asynccontextmanager
    async def open(self, **scope):
        async with self.service.open(**scope) as workspace:
            path = self.service.root / workspace.workspace_id
            self.paths.append(path)
            for container in await self.docker.containers.list():
                if any(mount.get("Source") == str(path) for mount in container["Mounts"]):
                    self.container_ids.append(container["Id"])
            yield workspace


@unittest.skipUnless(os.getenv("FIN_TEST_NATIVE_DOCKER") == "1", "native Docker integration is opt-in")
class NativeDockerAnalystTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(os.environ["FIN_TEST_WORKSPACE_DIR"]) / ("test-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.root.rmdir)
        self.manager = await self.enterAsyncContext(DockerWorkspaceManager(
            basedir=str(self.root), env={}, default_mcps=[], skill_paths=[],
        ))
        self.docker = aiodocker.Docker()
        self.addAsyncCleanup(self.docker.close)
        self.workspace = ObservedWorkspaces(AnalysisWorkspaces(self.manager, str(self.root)), self.docker)
        self.logs = AsyncMock()
        for target in ("finagent.runsteps.log_step", "finagent.analysis.log_step"):
            patcher = patch(target, self.logs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.body = AsyncMock()
        self.progress = AsyncMock()
        self.link = document_citation_url("doc/owned")

    async def analyze(self, model, skill=None):
        return await analyze_data(
            [document()], AnalyzeRequest(document_ids=["doc/owned"], question="Explain demand briefly."),
            model=model, progress=self.body, research_progress=self.progress,
            actor="docker-test", session_id="offline", run_id="offline", user_id="fixture-owner",
            read_document=AsyncMock(return_value=RAW), workspaces=self.workspace, skill=skill,
        )

    async def assert_cleaned(self):
        self.assertTrue(self.workspace.container_ids, "Must observe an actual native workspace container")
        for container_id in self.workspace.container_ids:
            with self.assertRaises(aiodocker.DockerError) as error:
                await self.docker.containers.get(container_id)
            self.assertEqual(error.exception.status, 404)
        self.assertTrue(all(not path.exists() for path in self.workspace.paths))
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_actual_read_grep_bash_and_fresh_state(self):
        calls = [
            ("Read", {"file_path": "/workspace/inputs/manifest.json"}),
            ("Read", {"file_path": "/workspace/inputs/document-0.html"}),
            ("Grep", {"pattern": "Demand", "path": "/workspace/inputs/document-0.html", "output_mode": "content"}),
            ("Bash", {"command": "python -c \"from pathlib import Path; print('BASH_BYTES=' + str(len(Path('/workspace/inputs/document-0.html').read_bytes())))\""}),
        ]
        script = [response(tool=ToolCallBlock(id=str(i), name=name, input=json.dumps(arguments)))
                  for i, (name, arguments) in enumerate(calls)]
        script += [response("Native-only research finding."), response(f"Demand increased [filing]({self.link})."),
                   response("Fresh research."), response(f"Second report [filing]({self.link}).")]
        model = ScriptModel(script)
        result = await self.analyze(model)
        evidence = str(model.calls[4]["messages"])
        self.assertIn("Demand increased in the Americas.", evidence)
        self.assertIn(f"BASH_BYTES={len(RAW)}", evidence)
        self.assertEqual(result["text"], f"Demand increased [filing]({self.link}).")
        tool_logs = [call.kwargs for call in self.logs.await_args_list if call.kwargs["step_type"] == "tool"]
        self.assertEqual([log["name"] for log in tool_logs], [name for name, _ in calls])
        self.assertTrue(all(log["status"] == "ok" for log in tool_logs), tool_logs)
        self.assertNotIn("Native-only", "".join(call.args[0] for call in self.body.await_args_list))
        await self.assert_cleaned()
        await self.analyze(model)
        self.assertNotIn("Native-only", str(model.calls[6]["messages"]))
        self.assertEqual(len(set(self.workspace.paths)), 2)
        await self.assert_cleaned()

    async def test_native_skill_and_staged_methodology(self):
        skill = Skill("financial-analysis", "Cash quality method", "/private/source", "Native-method: use cash quality.", 1.0)
        model = ScriptModel([
            response(tool=ToolCallBlock(id="method", name="Skill", input='{"skill":"financial-analysis"}')),
            response(tool=ToolCallBlock(id="read-method", name="Read", input='{"file_path":"/workspace/skills/financial-analysis/SKILL.md"}')),
            response("Method-backed findings."), response(f"Cash quality [filing]({self.link})."),
        ])
        await self.analyze(model, skill=skill)
        self.assertIn("Native-method", str(model.calls[1]["messages"]))
        self.assertIn("Native-method", str(model.calls[2]["messages"]))
        self.assertIn("Native-method", str(model.calls[3]["messages"]))
        tool_logs = [call.kwargs for call in self.logs.await_args_list if call.kwargs["step_type"] == "tool"]
        self.assertEqual([log["name"] for log in tool_logs], ["Skill", "Read"])
        self.assertTrue(all(log["status"] == "ok" for log in tool_logs))
        self.assertTrue(all(log["detail"]["skill"]["name"] == "financial-analysis" for log in tool_logs))
        await self.assert_cleaned()

    async def test_cancel_running_bash_removes_container_and_process(self):
        command = "python -u -c \"import os,time; from pathlib import Path; Path('/workspace/started.pid').write_text(str(os.getpid())); time.sleep(300)\""
        model = ScriptModel([response(tool=ToolCallBlock(id="blocking", name="Bash", input=json.dumps({"command": command})))])
        task = asyncio.create_task(self.analyze(model))
        try:
            async with asyncio.timeout(60):
                while not self.workspace.paths or not (self.workspace.paths[-1] / "started.pid").exists():
                    if task.done():
                        await task
                        self.fail("Analysis ended before Bash started")
                    await asyncio.sleep(0.1)
            self.assertTrue((self.workspace.paths[-1] / "started.pid").read_text().isdigit())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 30)
            self.body.assert_not_awaited()
            await self.assert_cleaned()
            self.assertFalse([t for t in asyncio.all_tasks()
                              if t.get_name().startswith("finance-analyst:") and not t.done()])
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
