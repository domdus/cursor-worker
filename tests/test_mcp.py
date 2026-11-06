from __future__ import annotations

import asyncio
from dataclasses import replace

from mcp import Client

from cursor_worker.jobs import JobManager, Store
from cursor_worker.mcp_server import build_mcp


def _manager(settings, engine):
    return JobManager(settings, engine, Store(settings.data_dir / "worker.sqlite"))


def _data(result):
    return result.structured_content or {}


async def test_tools_are_listed(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"cursor_agent", "cursor_job", "cursor_cancel", "cursor_workspaces"}


async def test_cursor_agent_returns_the_result(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        result = await client.call_tool("cursor_agent", {"task": "write docs", "workspace": "docs"})
    data = _data(result)
    assert data["status"] == "succeeded"
    assert data["result"] == "Done: write docs"
    assert data["workspace"] == "docs"
    assert data["changes"]["files"] == [" M README.md"]


async def test_long_job_returns_a_job_id_then_finishes(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(replace(settings, mcp_wait_seconds=0), engine)
    async with Client(build_mcp(manager)) as client:
        first = _data(await client.call_tool("cursor_agent", {"task": "slow"}))
        assert first["status"] in {"queued", "running"} and "hint" in first
        engine.gate.set()
        done = _data(await client.call_tool("cursor_job", {"job_id": first["job_id"], "wait_seconds": 5}))
    assert done["status"] == "succeeded"


async def test_cancel_and_unknown_job(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(replace(settings, mcp_wait_seconds=0), engine)
    async with Client(build_mcp(manager)) as client:
        job = _data(await client.call_tool("cursor_agent", {"task": "slow"}))
        cancelled = _data(await client.call_tool("cursor_cancel", {"job_id": job["job_id"]}))
        unknown = _data(await client.call_tool("cursor_job", {"job_id": "nope"}))
    assert cancelled["status"] == "cancelled"
    assert unknown["status"] == "unknown"


async def test_rejected_request_is_a_result_not_a_crash(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        data = _data(await client.call_tool("cursor_agent", {"task": "x", "workspace": "../up"}))
    assert data["status"] == "rejected" and "not allowed" in data["error"]
