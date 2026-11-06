from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from cursor_worker.engine import EngineError, Outcome, RunSpec
from cursor_worker.settings import Settings


class FakeEngine:
    """Scripted stand-in for CursorEngine. `gate` holds runs until released."""

    available = True

    def __init__(self) -> None:
        self.specs: list[RunSpec] = []
        self.gate: asyncio.Event | None = None
        self.fail: str = ""
        self.running = 0
        self.max_running = 0

    async def run(self, spec: RunSpec, emit) -> Outcome:
        self.specs.append(spec)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await emit({"type": "status", "text": "Thinking..."})
            await emit({"type": "tool", "name": "shell", "phase": "start", "text": "Calling shell..."})
            if self.gate is not None:
                await self.gate.wait()
            await emit({"type": "tool", "name": "shell", "phase": "end", "text": "Finished shell"})
            if self.fail:
                raise EngineError(self.fail)
            await emit({"type": "text", "delta": "Done: "})
            await emit({"type": "text", "delta": spec.task})
            return Outcome(
                status="succeeded",
                text=f"Done: {spec.task}",
                run_id="run-1",
                agent_id="agent-1",
                model=spec.model or "composer-2.5",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "charged_cents": None},
                changes={"files": [" M README.md"], "more_files": 0, "commits": [], "head": "abc"},
                duration_ms=12,
            )
        finally:
            self.running -= 1

    async def models(self):
        return [{"id": "composer-2.5", "name": "Composer 2.5"}, {"id": "gpt-5", "name": "GPT-5"}]

    async def aclose(self) -> None:
        return None


def make_settings(tmp: Path, **overrides) -> Settings:
    base = Settings(
        data_dir=tmp / "data",
        workspaces_dir=tmp / "data" / "workspaces",
        default_workspace="default",
        default_workspace_dir=None,
        state_dir=tmp / "data" / "cursor-state",
        mcp_config_path=tmp / "data" / "mcp.json",
        cursor_api_key="test-key",
        model="composer-2.5",
        host="127.0.0.1",
        port=8787,
    )
    base = replace(base, **overrides)
    base.ensure_dirs()
    return base


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()
