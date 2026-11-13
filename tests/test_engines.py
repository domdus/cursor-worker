"""Picking the engine: jobs, MCP, /v1 model ids, defaults and health."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agent_worker.app import create_app
from agent_worker.engine import EngineError
from agent_worker.engines import EngineRouter, split_engine
from agent_worker.openai_compat import model_engine
from tests.conftest import FakeEngine


class Named(FakeEngine):
    def __init__(self, name: str, settings, available: bool = True) -> None:
        super().__init__()
        self.name = name
        self.settings = settings
        self.available = available

    async def models(self):
        if self.name == "claude":
            return [{"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"}]
        return await super().models()


def _router(settings, *, claude=True, cursor=True, default=""):
    return EngineRouter(
        {"cursor": Named("cursor", settings, cursor), "claude": Named("claude", settings, claude)}, default
    )


def test_default_engine(settings) -> None:
    assert _router(settings).default() == "cursor"
    assert _router(settings, cursor=False).default() == "claude"
    assert _router(settings, default="claude").default() == "claude"
    assert _router(settings).pick("Claude") == "claude"
    with pytest.raises(EngineError):
        _router(settings).pick("gemini")


def test_model_ids() -> None:
    assert split_engine("claude-code/claude-opus-5-5") == ("claude", "claude-opus-5-5")
    # Cursor's own Claude models stay on Cursor.
    assert split_engine("claude-4.5-sonnet") == ("", "claude-4.5-sonnet")
    assert model_engine("claude-code/claude-opus-5-5@repo") == ("claude", "claude-opus-5-5", "repo")
    assert model_engine("claude-code") == ("claude", "", "")
    assert model_engine("composer-2.5@repo") == ("", "composer-2.5", "repo")


def _client(settings, router):
    return TestClient(create_app(settings, router), base_url="http://127.0.0.1")


def test_jobs_run_on_the_engine_they_name(settings) -> None:
    router = _router(settings)
    with _client(settings, router) as client:
        router.engines["claude"].gate = asyncio.Event()
        claude = client.post("/jobs", json={"task": "hi", "engine": "claude"}).json()
        # Before the run, the job shows the engine's own default model.
        assert claude["engine"] == "claude" and claude["model"] == settings.claude_model
        router.engines["claude"].gate.set()
        client.get(f"/jobs/{claude['id']}?wait=5")
        assert router.engines["claude"].specs[-1].engine == "claude"
        cursor = client.post("/jobs", json={"task": "hi", "wait": 5}).json()
        assert cursor["engine"] == "cursor" and cursor["model"] == settings.model
        assert len(router.engines["cursor"].specs) == 1
        bad = client.post("/jobs", json={"task": "hi", "engine": "gemini"})
        assert bad.status_code == 400 and "Unknown engine" in bad.json()["detail"]


def test_v1_routes_claude_code_models(settings) -> None:
    router = _router(settings)
    with _client(settings, router) as client:
        ids = [row["id"] for row in client.get("/v1/models").json()["data"]]
        assert "claude-code/claude-sonnet-5-5" in ids and "composer-2.5" in ids
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "claude-code/claude-opus-5-5", "messages": [{"role": "user", "content": "hello"}]},
        ).json()
        assert reply["choices"][0]["message"]["content"] == "Done: hello"
        spec = router.engines["claude"].specs[-1]
        assert spec.engine == "claude" and spec.model == "claude-opus-5-5"


def test_v1_hides_engines_that_are_not_set_up(settings) -> None:
    with _client(settings, _router(settings, claude=False)) as client:
        ids = [row["id"] for row in client.get("/v1/models").json()["data"]]
        assert not any(i.startswith("claude-code/") for i in ids)


def test_mcp_tool_takes_an_engine(settings) -> None:
    from agent_worker.jobs import JobManager, JobRequest, Store

    router = _router(settings)

    async def run():
        manager = JobManager(settings, router, Store(settings.data_dir / "w.sqlite"))
        job = manager.submit(JobRequest(task="x", engine="claude", origin="mcp"))
        await manager.wait(job, 5)
        from agent_worker.mcp_server import job_payload

        return job_payload(job)

    payload = asyncio.run(run())
    assert payload["engine"] == "claude" and payload["status"] == "succeeded"


def test_health_reports_claude(settings, monkeypatch) -> None:
    from agent_worker import claude_engine

    monkeypatch.setattr(claude_engine, "sdk_installed", lambda: True)
    with _client(replace(settings, anthropic_api_key="sk-ant-api03-x"), _router(settings)) as client:
        body = client.get("/health").json()
    assert body["claude"]["ready"] is True and body["claude"]["credential"] == "env-api"
    assert body["claude"]["installed"] is True and body["default_engine"] == "cursor"
