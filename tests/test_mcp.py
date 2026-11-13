from __future__ import annotations

import asyncio
from dataclasses import replace

from mcp import Client

from agent_worker.jobs import JobManager, Store
from agent_worker.mcp_server import build_mcp


def _manager(settings, engine):
    return JobManager(settings, engine, Store(settings.data_dir / "worker.sqlite"))


def _data(result):
    return result.structured_content or {}


async def test_tools_are_listed(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"agent_run", "agent_job", "agent_cancel", "agent_workspaces"}


async def test_agent_run_returns_the_result(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        result = await client.call_tool("agent_run", {"task": "write docs", "workspace": "docs"})
    data = _data(result)
    assert data["status"] == "succeeded"
    assert data["result"] == "Done: write docs"
    assert data["workspace"] == "docs"
    assert data["changes"]["files"] == [" M README.md"]


async def test_long_job_returns_a_job_id_then_finishes(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(replace(settings, mcp_wait_seconds=0), engine)
    async with Client(build_mcp(manager)) as client:
        first = _data(await client.call_tool("agent_run", {"task": "slow"}))
        assert first["status"] in {"queued", "running"} and "hint" in first
        engine.gate.set()
        done = _data(await client.call_tool("agent_job", {"job_id": first["job_id"], "wait_seconds": 5}))
    assert done["status"] == "succeeded"


async def test_cancel_and_unknown_job(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(replace(settings, mcp_wait_seconds=0), engine)
    async with Client(build_mcp(manager)) as client:
        job = _data(await client.call_tool("agent_run", {"task": "slow"}))
        cancelled = _data(await client.call_tool("agent_cancel", {"job_id": job["job_id"]}))
        unknown = _data(await client.call_tool("agent_job", {"job_id": "nope"}))
    assert cancelled["status"] == "cancelled"
    assert unknown["status"] == "unknown"


async def test_rejected_request_is_a_result_not_a_crash(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        data = _data(await client.call_tool("agent_run", {"task": "x", "workspace": "../up"}))
    assert data["status"] == "rejected" and "not allowed" in data["error"]


async def test_agent_run_names_the_engines_and_takes_one(settings):
    from tests.test_engines import _router

    router = _router(settings, cursor=False)
    async with Client(build_mcp(_manager(settings, router))) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        description = tools["agent_run"].description
        assert "cursor (Cursor SDK)" in description and "claude (Claude Code) (default)" in description
        assert "engine" in tools["agent_run"].input_schema["properties"]
        result = await client.call_tool("agent_run", {"task": "hi", "engine": "claude", "wait_seconds": 5})
    data = _data(result)
    assert data["engine"] == "claude" and data["status"] == "succeeded"
    assert router.engines["claude"].specs and not router.engines["cursor"].specs
