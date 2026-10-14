"""Explicit one-turn live-model acceptance for the original-filing analyst.

Creates one new test session using an existing session's model reference, never
reads credential secrets, and sends through the real browser composer. No retry.
"""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from playwright.sync_api import expect

from acceptance import Acceptance, require


class FilingAcceptance(Acceptance):
    def test_sources(self, suffix=""):
        saved = self.api_get("/fin/results", {"session_id": self.args.session_id, "limit": "10"})["artifacts"]
        documents = [item for item in saved if item["kind"] == "document"]
        narratives = [item for item in saved if item["kind"] == "narrative"]
        require(len(documents) == 1 and len(narratives) == 1, "Expected one saved filing and one report")
        document, narrative = documents[0], narratives[0]
        require(document["artifact_id"] in narrative["input_ids"], "Report must depend on the original filing")
        self.report["narrative_text"] = narrative["text"]
        self.report["document"] = {"artifact_id": document["artifact_id"], **document["document"]}
        panel = self.page.locator('[data-testid="finance-report"]')
        self.page.locator(f'[data-finance-artifact="{narrative["artifact_id"]}"] [data-finance-report-open]').first.click()
        expect(panel).to_be_visible()
        links = panel.locator('a[data-finance-citation][href^="#filing/"]')
        require(links.count() > 0, "Saved report has no original-document citation")
        self.screenshot("filing-report" + suffix)
        links.first.click()
        drawer = self.page.locator('[data-testid="finance-provenance"]')
        details = drawer.locator(f'[data-finance-document="{document["artifact_id"]}"]')
        expect(details).to_be_visible()
        expect(details).to_contain_text(document["document"]["sha256"])
        require(self.page.locator("iframe").count() == 0, "Original HTML must not be embedded")
        self.screenshot("filing-source" + suffix)
        with self.page.expect_download() as pending:
            details.get_by_role("button", name="下载原文").click()
        output = self.output / ("filing" + suffix + ".html")
        pending.value.save_as(str(output))
        raw = output.read_bytes()
        require(len(raw) == document["document"]["byte_size"], "Downloaded size differs from pinned version")
        require(hashlib.sha256(raw).hexdigest() == document["document"]["sha256"], "Downloaded hash differs from pinned version")
        self.page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(panel).to_be_visible()
        self.page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        self.report["checks"]["document_citation_download" + suffix] = {"status": "passed", "byte_size": len(raw)}

    def test_assistant_citations(self, suffix=""):
        # The document report is checked above; no numeric tables were requested.
        pass

    def test_formula(self):
        pass

    def test_download(self):
        # Real HTML attachment download is exercised with its source drawer above.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real-model", action="store_true")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-url", default="http://127.0.0.1:8080")
    parser.add_argument("--user-id", default="dev")
    parser.add_argument("--output-dir", type=Path, default=Path("/e2e-output"))
    args = parser.parse_args()
    if not args.run_real_model:
        parser.error("Requires --run-real-model; this test spends provider tokens")
    with httpx.Client(base_url=args.api_url, headers={"X-User-ID": args.user_id}, timeout=30) as client:
        response = client.get("/sessions/", params={"agent_id": args.agent_id})
        response.raise_for_status()
        sessions = response.json()["sessions"]
        source = next(item for item in sessions if item["session"]["id"] == args.source_session_id)
        require(not source["is_running"], "Source session is running; do not overlap the test")
        config = source["session"]["config"]["chat_model_config"]
        require(bool(config), "Existing session must have a configured model")
        config = {**config, "parameters": {**config["parameters"], "max_tokens": 3000}}
        response = client.post("/sessions/", json={"agent_id": args.agent_id,
            "name": "SEC 原文分析实测", "chat_model_config": config})
        response.raise_for_status()
        session_id = response.json()["session_id"]
    print(json.dumps({"created_session_id": session_id, "model": config["model"], "max_output_tokens_per_call": 3000}), flush=True)
    prompt = "获取苹果最新一期10-Q，只获取一份。使用原文分析总营收同比变化及管理层给出的原因。请调用分析工具，尽量只做必要的文件读取和搜索，报告控制在300字以内，附可点击的原文引用；区分原文陈述与推断。不要另行采集companyfacts，不要导出Excel，不要自动重复失败的分析。"
    run_args = SimpleNamespace(**vars(args), session_id=session_id, multi_turn=True, headed=False,
                               ui_timeout=45, turn_timeout=420, prompts=[prompt])
    return FilingAcceptance(run_args).run()


if __name__ == "__main__":
    raise SystemExit(main())
