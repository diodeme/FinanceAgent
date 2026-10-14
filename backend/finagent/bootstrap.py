"""Provision one owned FinanceAgent without touching credentials or sessions."""

from agentscope.app.storage import AgentData, AgentRecord, StorageBase

from .db import get_pool
from .registry import COMPANIES, seed_registry
from .repository import recover_stale_operations
from .sec_client import contact_configured


def capabilities() -> dict:
    return {
        "companies": [company.model_dump(mode="json") for company in COMPANIES.values()],
        "max_quarters": 8,
        "scope": "consolidated_us_gaap",
        "web_search": False,
        "filing_text": False,  # No pre-extracted text/sections or full-read guarantee.
        "raw_filing_html": True,
        "filing_download_configured": contact_configured(),
        "max_filings": 8,
        "tools": ["lookup_company", "data_fetch", "fetch_filings", "calculate_metrics", "data_analyze", "find_results", "get_result", "export_report"],
        "warnings": [
            "通用网页搜索暂未接入：未找到当前环境下可靠的免费免授权搜索接口。",
            "支持保存 SEC 申报主文档原始 HTML；下载或引用不代表已通读全文，不预先解析章节。",
        ] + ([] if contact_configured() else ["原始申报下载待配置：SEC_USER_AGENT 必须包含真实联系邮箱；未配置时不会尝试下载。"]),
    }


async def ensure_finance_agent(user_id: str, storage: StorageBase) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,12531))", "fin.bootstrap:" + user_id)
        await seed_registry(conn)
        mapped = await conn.fetchval("SELECT agent_id FROM fin.builtin_agents WHERE user_id=$1", user_id)
        record = await storage.get_agent(user_id, mapped) if mapped else None
        if record is None or record.user_id != user_id or record.source != "user":
            existing = await storage.list_agents(user_id)
            record = next((r for r in existing if r.user_id == user_id and r.source == "user" and r.data.name == "FinanceAgent"), None)
            if record:
                mapped = record.id
            else:
                mapped = await storage.upsert_agent(user_id, AgentRecord(
                    user_id=user_id,
                    data=AgentData(
                        name="FinanceAgent",
                        system_prompt="你是 FinanceAgent，帮助用户查询、比较和分析财务数据。用用户的语言回答，尊重证据与数据范围。",
                    ),
                ))
            await conn.execute("""
                INSERT INTO fin.builtin_agents(user_id,agent_id) VALUES($1,$2)
                ON CONFLICT(user_id) DO UPDATE SET agent_id=excluded.agent_id
            """, user_id, mapped)
    await recover_stale_operations()
    return {"agent_id": mapped, "capabilities": capabilities()}
