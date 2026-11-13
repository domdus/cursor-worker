"""Claude engine: credentials, tool profiles, a whole job through a fake `query`, resume and errors."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

sdk = pytest.importorskip("claude_agent_sdk")

from agent_worker import claude_engine  # noqa: E402
from agent_worker.claude_engine import ClaudeEngine, credential, credential_source, subprocess_env, tool_policy  # noqa: E402
from agent_worker.claude_login import save_token  # noqa: E402
from agent_worker.engine import WORKSPACE_ENV, EngineError, RunSpec  # noqa: E402
from tests.conftest import make_settings  # noqa: E402

PLAN = "sk-ant-oat01-" + "p" * 40
API = "sk-ant-api03-" + "a" * 40


def _settings(tmp: Path, **kw):
    return make_settings(tmp, **{"cursor_api_key": "", **kw})


def test_credentials_env_first_then_signed_in(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert credential(settings) == "" and credential_source(settings) == "none"
    save_token(settings.claude_token_path, PLAN)
    assert credential(settings) == PLAN and credential_source(settings) == "signed-in"
    assert oct(settings.claude_token_path.stat().st_mode & 0o777) == "0o600"
    api = replace(settings, anthropic_api_key=API)
    assert credential(api) == API and credential_source(api) == "env-api"
    both = replace(api, claude_oauth_token=PLAN.replace("p", "q"))
    assert credential(both).startswith("sk-ant-oat01-q") and credential_source(both) == "env-plan"


def test_only_one_credential_reaches_claude_code(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = subprocess_env(settings, PLAN, "repo")
    assert plan["CLAUDE_CODE_OAUTH_TOKEN"] == PLAN and plan["ANTHROPIC_API_KEY"] == ""
    assert plan["ANTHROPIC_AUTH_TOKEN"] == "" and plan[WORKSPACE_ENV] == "repo"
    assert plan["CLAUDE_CONFIG_DIR"] == str(settings.claude_config_dir)
    api = subprocess_env(settings, API, "repo")
    assert api["ANTHROPIC_API_KEY"] == API and api["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_tool_profiles() -> None:
    servers = {"ha": {}, "git": {}}
    full = tool_policy("full", servers)
    assert {"Bash", "WebSearch", "Edit"} <= set(full["tools"])
    assert full["allowed_tools"][:2] == ["mcp__git__*", "mcp__ha__*"]
    assert full["permission_mode"] == "dontAsk" and {"Task", "Agent"} <= set(full["disallowed_tools"])
    local = tool_policy("local", servers)
    assert "Bash" in local["tools"] and "WebSearch" not in local["tools"] and "WebFetch" in local["disallowed_tools"]
    offline = tool_policy("offline", servers)
    assert "Bash" not in offline["tools"] and "Bash" in offline["disallowed_tools"] and "Read" in offline["tools"]
    mcp = tool_policy("mcp", servers)
    assert mcp["tools"] == [] and mcp["allowed_tools"] == ["mcp__git__*", "mcp__ha__*"]
    none = tool_policy("none", servers)
    assert none["tools"] == [] and none["allowed_tools"] == []


# --- a whole job through a fake query() -------------------------------------------------------------


def _result(text="All done.", session="sess-1", error=False, subtype="success"):
    return sdk.ResultMessage(
        subtype=subtype, duration_ms=1234, duration_api_ms=1000, is_error=error, num_turns=2, session_id=session,
        total_cost_usd=0.012, usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 7},
        result=text, model_usage={"claude-sonnet-5-5": {"outputTokens": 20}, "claude-haiku-5-5": {"outputTokens": 1}},
    )


def _turn(*items):
    return list(items)


class FakeQuery:
    """Stands in for claude_agent_sdk.query; records options, plays scripted turns."""

    def __init__(self, *turns, fail_after_result: bool = False, hang: asyncio.Event | None = None):
        self.turns = list(turns)
        self.options: list = []
        self.prompts: list = []
        self.fail_after_result = fail_after_result
        self.hang = hang
        self.closed = 0

    def __call__(self, *, prompt, options=None, transport=None):
        self.options.append(options)
        self.prompts.append(prompt)
        items = self.turns.pop(0)
        outer = self

        async def gen():
            try:
                for item in items:
                    yield item
                if outer.hang is not None:
                    await outer.hang.wait()
                if outer.fail_after_result:
                    raise RuntimeError("Command failed with exit code 1")
            finally:
                outer.closed += 1

        return gen()


class Ids:
    def __init__(self):
        self.rows: dict = {}

    def get(self, workspace, session):
        return self.rows.get((workspace, session))

    def set(self, workspace, session, value):
        if value:
            self.rows[(workspace, session)] = value
        else:
            self.rows.pop((workspace, session), None)


def _spec(tmp: Path, **kw) -> RunSpec:
    base = dict(job_id="j1", task="Fix the bug", workspace="repo", path=tmp / "ws", engine="claude", profile="full")
    base.update(kw)
    return RunSpec(**base)


def _engine(tmp: Path, fake: FakeQuery, monkeypatch, **settings_kw):
    monkeypatch.setattr(sdk, "query", fake)
    ids = Ids()
    settings = _settings(tmp, claude_oauth_token=PLAN, **settings_kw)
    return ClaudeEngine(settings, (ids.get, ids.set)), ids


async def _collect(engine, spec):
    events: list = []

    async def emit(event):
        events.append(event)

    return await engine.run(spec, emit), events


def test_a_job_runs_and_reports(tmp_path: Path, monkeypatch) -> None:
    fake = FakeQuery(
        _turn(
            sdk.AssistantMessage(content=[sdk.ThinkingBlock("hm", "sig"), sdk.TextBlock("Looking. ")], model="m"),
            sdk.AssistantMessage(content=[sdk.ToolUseBlock("t1", "Bash", {"command": "ls"})], model="m"),
            sdk.UserMessage(content=[sdk.ToolResultBlock("t1", "ok")]),
            sdk.AssistantMessage(content=[sdk.TextBlock("Fixed it.")], model="m"),
            _result("Fixed it."),
        ),
        fail_after_result=True,
    )
    engine, ids = _engine(tmp_path, fake, monkeypatch, instructions="Be brief.")
    outcome, events = asyncio.run(_collect(engine, _spec(tmp_path, session="chat-1", mcp_servers={"ha": {"type": "http", "url": "http://h/mcp"}})))
    assert outcome.status == "succeeded" and outcome.text == "Fixed it."
    assert outcome.agent_id == "sess-1" and outcome.model == "claude-sonnet-5-5"
    assert outcome.usage["prompt_tokens"] == 100 and outcome.usage["cost_usd"] == 0.012
    kinds = [(e["type"], e.get("phase")) for e in events]
    assert ("tool", "start") in kinds and ("tool", "end") in kinds and ("text", None) in kinds
    options = fake.options[0]
    assert options.cwd == str(tmp_path / "ws") and options.setting_sources == [] and options.strict_mcp_config
    assert options.model == "claude-sonnet-5-5" and options.resume is None
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == PLAN and options.env[WORKSPACE_ENV] == "repo"
    assert "mcp__ha__*" in options.allowed_tools and options.mcp_servers["ha"]["url"] == "http://h/mcp"
    # A new session gets the worker's instructions; the session id is kept for the next turn.
    assert "Be brief." in fake.prompts[0] and fake.prompts[0].endswith("Fix the bug")
    assert ids.rows[("repo@claude", "chat-1")] == "sess-1"


def test_a_follow_up_resumes_and_a_lost_session_starts_fresh(tmp_path: Path, monkeypatch) -> None:
    fake = FakeQuery(
        _turn(_result("No conversation found with session ID: old", error=True, subtype="error_during_execution")),
        _turn(_result("Fresh answer.", session="sess-2")),
    )
    engine, ids = _engine(tmp_path, fake, monkeypatch)
    ids.rows[("repo@claude", "chat-1")] = "old"
    outcome, _events = asyncio.run(_collect(engine, _spec(tmp_path, session="chat-1", task="And now?")))
    assert [o.resume for o in fake.options] == ["old", None]
    # The warm session only gets the task; the fresh one gets the instructions again.
    assert fake.prompts[0] == "And now?" and fake.prompts[1] != "And now?"
    assert outcome.status == "succeeded" and ids.rows[("repo@claude", "chat-1")] == "sess-2"


def test_claude_errors_become_plain_messages(tmp_path: Path, monkeypatch) -> None:
    fake = FakeQuery(
        _turn(
            sdk.AssistantMessage(content=[sdk.TextBlock("Invalid API key · Please run /login")], model="m",
                                 error="authentication_failed"),
            _result("Invalid API key · Please run /login", error=True, subtype="success"),
        )
    )
    engine, ids = _engine(tmp_path, fake, monkeypatch)
    outcome, events = asyncio.run(_collect(engine, _spec(tmp_path, session="s")))
    assert outcome.status == "failed" and outcome.error.startswith("Anthropic refused the Claude credential")
    # The CLI's own hint is not repeated as answer text, and a failed session is not resumed.
    assert not [e for e in events if e["type"] == "text"]
    assert ("repo@claude", "s") not in ids.rows


def test_crash_before_result_is_an_engine_error_without_the_token(tmp_path: Path, monkeypatch) -> None:
    class Boom(FakeQuery):
        def __call__(self, *, prompt, options=None, transport=None):
            async def gen():
                raise RuntimeError(f"CLI died with token {PLAN}")
                yield  # pragma: no cover

            return gen()

    engine, _ids = _engine(tmp_path, Boom(), monkeypatch)
    with pytest.raises(EngineError) as err:
        asyncio.run(_collect(engine, _spec(tmp_path)))
    assert PLAN not in str(err.value) and "CLI died" in str(err.value)


def test_no_credential_says_how_to_sign_in(tmp_path: Path, monkeypatch) -> None:
    engine = ClaudeEngine(_settings(tmp_path))
    assert not engine.available
    with pytest.raises(EngineError) as err:
        asyncio.run(_collect(engine, _spec(tmp_path)))
    assert "agent_worker.claude_login" in str(err.value)


def test_cancel_closes_the_claude_run(tmp_path: Path, monkeypatch) -> None:
    async def run():
        hang = asyncio.Event()
        fake = FakeQuery(_turn(sdk.AssistantMessage(content=[sdk.TextBlock("Working")], model="m")), hang=hang)
        engine, _ids = _engine(tmp_path, fake, monkeypatch)
        task = asyncio.create_task(_collect(engine, _spec(tmp_path)))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if fake.options:
                break
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.closed == 1

    asyncio.run(run())


def test_images_and_effort(tmp_path: Path, monkeypatch) -> None:
    fake = FakeQuery(_turn(_result("A cat.")))
    engine, _ids = _engine(tmp_path, fake, monkeypatch)
    spec = _spec(tmp_path, effort="high", images=[{"mime": "image/png", "data": "aGk="}], model="claude-opus-5-5")
    outcome, _ = asyncio.run(_collect(engine, spec))
    assert outcome.status == "succeeded"
    assert fake.options[0].effort == "high" and fake.options[0].model == "claude-opus-5-5"
    assert not isinstance(fake.prompts[0], str)


def test_models_list(tmp_path: Path) -> None:
    engine = ClaudeEngine(_settings(tmp_path, claude_model="claude-sonnet-9"))
    rows = asyncio.run(engine.models())
    assert rows[0]["id"] == "claude-sonnet-9" and any(r["id"] == "claude-opus-5-5" for r in rows)
    assert claude_engine.resolve_model(engine.settings, "") == "claude-sonnet-9"
