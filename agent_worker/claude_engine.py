"""Run one task on Claude Code (claude-agent-sdk) and report what happened.

Ported from the AI Workspace runtime's Claude provider. Claude Code runs as a subprocess in the job's
workspace with only what the job sends: no user or project settings, CLAUDE.md, skills or hooks from
disk, and no subagents. Tools follow the job's tool profile, like the Cursor engine.

Credentials, first one set wins:
  CLAUDE_CODE_OAUTH_TOKEN  a Claude Plan token (sk-ant-oat…, `claude setup-token`), billed to the plan
  ANTHROPIC_API_KEY        an Anthropic API key, billed as API credits
  DATA_DIR/claude/oauth-token  the Plan token from "Sign in with Claude"
Exactly one of them reaches Claude Code; the others are blanked so a stray variable cannot switch
a plan job to API billing or the other way round.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from typing import Any

from agent_worker.claude_login import load_token
from agent_worker.engine import (
    WORKSPACE_ENV,
    AgentIdStore,
    Emit,
    EngineError,
    Outcome,
    RunSpec,
    compose_message,
    git_changes,
    git_head,
)
from agent_worker.events import _resolved_tool_name, _tool_event, _tool_label
from agent_worker.redact import redact
from agent_worker.settings import Settings

logger = logging.getLogger(__name__)

API_KEY_URL = "https://console.anthropic.com/settings/keys"
MODELS = (
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
)
MISSING_KEY = (
    "No Claude credential is set. Sign in with `docker exec -it agent-worker python -m "
    "agent_worker.claude_login`, or set CLAUDE_CODE_OAUTH_TOKEN (Claude Plan token from `claude setup-token`) "
    f"or ANTHROPIC_API_KEY ({API_KEY_URL}) on the worker."
)
NOT_INSTALLED = "claude-agent-sdk is not installed in this worker image."
CLI_MISSING = "The Claude Code CLI that ships with claude-agent-sdk was not found in this worker image."
API_ERRORS = {
    "authentication_failed": (
        "Anthropic refused the Claude credential. Sign in again or check CLAUDE_CODE_OAUTH_TOKEN / "
        "ANTHROPIC_API_KEY. Plan tokens last one year."
    ),
    "billing_error": (
        "Anthropic refused the request for billing reasons. Check the Claude plan, or the credits of the "
        "API key's account in the Claude Console."
    ),
    "rate_limit": "This Claude credential hit its usage or rate limit. Try again later.",
}

# Claude Code's built-in tools by group. Profiles take whole groups, like the Cursor engine.
_FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "TodoWrite")
_SHELL_TOOLS = ("Bash", "BashOutput", "KillShell", "KillBash")
_WEB_TOOLS = ("WebSearch", "WebFetch")
# Never offered: subagents would start their own model runs outside the job's tool profile.
_NEVER = ("Task", "Agent")
_PROFILE_TOOLS = {
    "full": _FILE_TOOLS + _SHELL_TOOLS + _WEB_TOOLS,
    "local": _FILE_TOOLS + _SHELL_TOOLS,
    "offline": _FILE_TOOLS,
    "mcp": (),
    "none": (),
}
# Worker effort choices (shared with Cursor) -> Claude Code effort levels.
_EFFORT = {"fast": "low", "high": "high", "high_fast": "medium"}
# Claude Code answers this when a session to resume is gone (for example a new data volume).
_SESSION_GONE = ("no conversation found", "session not found")


def sdk_installed() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


def credential(settings: Settings) -> str:
    return (
        (settings.claude_oauth_token or "").strip()
        or (settings.anthropic_api_key or "").strip()
        or load_token(settings.claude_token_path)
    )


def credential_source(settings: Settings) -> str:
    """Where the credential comes from, for /health: env-plan, env-api, signed-in or none."""
    if (settings.claude_oauth_token or "").strip():
        return "env-plan"
    if (settings.anthropic_api_key or "").strip():
        return "env-api"
    return "signed-in" if load_token(settings.claude_token_path) else "none"


def is_plan_token(value: str) -> bool:
    return (value or "").strip().startswith("sk-ant-oat")


def subprocess_env(settings: Settings, cred: str, workspace: str) -> dict[str, str]:
    """Merged onto the Claude Code subprocess, not the worker's own environment."""
    settings.claude_config_dir.mkdir(parents=True, exist_ok=True)
    plan = is_plan_token(cred)
    return {
        "CLAUDE_AGENT_SDK_CLIENT_APP": "agent-worker",
        "CLAUDE_CONFIG_DIR": str(settings.claude_config_dir),
        "CLAUDE_CODE_OAUTH_TOKEN": cred if plan else "",
        "ANTHROPIC_API_KEY": "" if plan else cred,
        "ANTHROPIC_AUTH_TOKEN": "",
        "DISABLE_AUTOUPDATER": "1",
        # The agent's shell inherits this: the ssh guard knows which job a command belongs to.
        WORKSPACE_ENV: workspace,
    }


def tool_policy(profile: str, mcp_servers: dict) -> dict[str, Any]:
    """ClaudeAgentOptions fields: which tools exist, which run without asking (everything else is denied)."""
    builtins = list(_PROFILE_TOOLS.get(profile, ()))
    allowed = [] if profile == "none" else [f"mcp__{name}__*" for name in sorted(mcp_servers or {})]
    blocked = list(_NEVER) + [tool for tool in _PROFILE_TOOLS["full"] if tool not in builtins]
    return {
        "tools": builtins,
        "allowed_tools": allowed + builtins,
        "disallowed_tools": blocked,
        # Nobody can answer a permission prompt in a headless job.
        "permission_mode": "dontAsk",
    }


def resolve_model(settings: Settings, model: str) -> str:
    return (model or "").strip() or settings.claude_model


def user_content(message: str, images: list[dict[str, str]] | None) -> str | list[dict[str, Any]]:
    pictures = [item for item in (images or []) if item.get("data") and item.get("mime")]
    if not pictures:
        return message
    blocks: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": item["mime"], "data": item["data"]}}
        for item in pictures
    ]
    blocks.append({"type": "text", "text": message})
    return blocks


async def _stream_prompt(content: list[dict[str, Any]]):
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def map_message(message: Any) -> list[dict[str, Any]]:
    """Text, thinking and tool steps as worker events. The result is handled by the engine."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage

    events: list[dict[str, Any]] = []
    if isinstance(message, AssistantMessage):
        if getattr(message, "error", None):
            # Claude Code also writes the failure as assistant text; the result reports it once.
            return events
        for block in message.content or []:
            if isinstance(block, TextBlock):
                if (block.text or "").strip():
                    events.append({"type": "text", "delta": str(block.text)})
            elif isinstance(block, ThinkingBlock):
                events.append({"type": "status", "text": "Thinking..."})
            elif isinstance(block, ToolUseBlock):
                name = _resolved_tool_name(block.name, block.input)
                events.append(_tool_event(name, "start", f"Calling {_tool_label(name)}...", block.id))
    elif isinstance(message, UserMessage):
        for block in message.content if isinstance(message.content, list) else []:
            if isinstance(block, ToolResultBlock):
                text = "Tool failed" if getattr(block, "is_error", False) else "Finished"
                events.append(_tool_event("tool", "end", text, block.tool_use_id))
    return events


def _int(value: object) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _pick(blob: Any, *keys: str) -> object:
    for key in keys:
        value = blob.get(key) if isinstance(blob, dict) else getattr(blob, key, None)
        if value:
            return value
    return None


def usage_from_result(message: Any) -> tuple[dict[str, Any] | None, str]:
    """(usage in the worker's shape, main model) from a ResultMessage."""
    usage = getattr(message, "usage", None) or {}
    prompt = _int(_pick(usage, "input_tokens"))
    completion = _int(_pick(usage, "output_tokens"))
    cache_read = _int(_pick(usage, "cache_read_input_tokens"))
    cache_write = _int(_pick(usage, "cache_creation_input_tokens"))
    model = ""
    by_model = getattr(message, "model_usage", None) or {}
    if isinstance(by_model, dict) and by_model:
        # The main model has the most output; small helper models come along too.
        model = max(by_model, key=lambda name: _int(_pick(by_model[name] or {}, "outputTokens", "output_tokens")))
        if not (prompt or completion):
            for blob in by_model.values():
                prompt += _int(_pick(blob or {}, "inputTokens", "input_tokens"))
                completion += _int(_pick(blob or {}, "outputTokens", "output_tokens"))
                cache_read += _int(_pick(blob or {}, "cacheReadInputTokens", "cache_read_input_tokens"))
                cache_write += _int(_pick(blob or {}, "cacheCreationInputTokens", "cache_creation_input_tokens"))
    cost = getattr(message, "total_cost_usd", None)
    try:
        cost_usd = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    if not (prompt or completion or cost_usd):
        return None, model
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": prompt + completion,
        "charged_cents": None,
        # What Claude Code reports; on a Plan token it is an estimate, not a charge.
        "cost_usd": cost_usd,
    }, model


class _Turn:
    """What one Claude Code run produced."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.result: Any = None
        self.api_error = ""


class ClaudeEngine:
    def __init__(self, settings: Settings, agent_ids: AgentIdStore | None = None) -> None:
        self.settings = settings
        self._get_session, self._set_session = agent_ids or (lambda _w, _s: None, lambda _w, _s, _a: None)

    @property
    def available(self) -> bool:
        return sdk_installed() and bool(credential(self.settings))

    @staticmethod
    def _key(workspace: str) -> str:
        # Its own row next to Cursor's agent id, so a session can switch engines without mixups.
        return f"{workspace}@claude"

    def forget(self, workspace: str, session: str) -> None:
        self._set_session(self._key(workspace), session, "")

    def _public(self, exc: BaseException, cred: str) -> str:
        try:
            from claude_agent_sdk import CLINotFoundError
        except ImportError:  # pragma: no cover
            CLINotFoundError = ()  # type: ignore[assignment]
        if CLINotFoundError and isinstance(exc, CLINotFoundError):
            return CLI_MISSING
        return (redact(str(exc), [cred]).strip() or type(exc).__name__)[:400]

    async def _turn(self, spec: RunSpec, cred: str, resume: str | None, emit: Emit) -> _Turn:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

        message = compose_message(spec, not resume, self.settings.instructions)
        content = user_content(message, spec.images)
        servers = {} if spec.profile == "none" else dict(spec.mcp_servers or {})
        kwargs: dict[str, Any] = {
            "cwd": str(spec.path),
            # No user, project or local settings, CLAUDE.md, skills or hooks: only what the job sends.
            "setting_sources": [],
            "strict_mcp_config": True,
            "model": resolve_model(self.settings, spec.model),
            "env": subprocess_env(self.settings, cred, spec.workspace),
            "resume": resume,
            "mcp_servers": servers,
            "stderr": lambda line: logger.debug("claude cli: %s", redact(line, [cred])),
            **tool_policy(spec.profile, servers),
        }
        level = _EFFORT.get((spec.effort or "").strip())
        if level:
            kwargs["effort"] = level
        turn = _Turn()
        stream = query(
            prompt=content if isinstance(content, str) else _stream_prompt(content),
            options=ClaudeAgentOptions(**kwargs),
        )
        try:
            # Read to the end: leaving early closes the SDK's generator from the wrong task.
            async for item in stream:
                if turn.result is not None:
                    continue
                if isinstance(item, AssistantMessage) and item.error:
                    turn.api_error = str(item.error)
                if isinstance(item, ResultMessage):
                    turn.result = item
                    continue
                for event in map_message(item):
                    if event.get("type") == "text":
                        turn.parts.append(str(event.get("delta") or ""))
                    await emit(event)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(stream.aclose())
            except Exception:
                logger.debug("claude stream close after cancel", exc_info=True)
            raise
        except Exception:
            if turn.result is None:
                raise
            # Claude Code exits non-zero after an error result; the result is what counts.
            logger.debug("claude cli exit after result job=%s", spec.job_id, exc_info=True)
        return turn

    async def run(self, spec: RunSpec, emit: Emit) -> Outcome:
        if not sdk_installed():
            raise EngineError(NOT_INSTALLED)
        cred = credential(self.settings)
        if not cred:
            raise EngineError(MISSING_KEY)
        started = time.monotonic()
        spec.path.mkdir(parents=True, exist_ok=True)
        before = await git_head(spec.path)
        await emit({"type": "status", "text": "Starting Claude..."})
        key = self._key(spec.workspace)
        resume = self._get_session(key, spec.session) if spec.session else None
        try:
            turn = await self._turn(spec, cred, resume, emit)
            gone = turn.result is not None and turn.result.is_error and any(
                mark in str(turn.result.result or "").lower() for mark in _SESSION_GONE
            )
            if resume and gone:
                logger.info("claude session %s is gone, starting a new one", resume)
                self.forget(spec.workspace, spec.session)
                turn = await self._turn(spec, cred, None, emit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            text = self._public(exc, cred)
            if resume and any(mark in text.lower() for mark in _SESSION_GONE):
                self.forget(spec.workspace, spec.session)
            raise EngineError(text) from exc
        result = turn.result
        changes = await git_changes(spec.path, before)
        duration = int((time.monotonic() - started) * 1000)
        if result is None:
            return Outcome(
                status="failed",
                error="Claude stopped without an answer.",
                text="".join(turn.parts).strip(),
                changes=changes,
                duration_ms=duration,
                model=resolve_model(self.settings, spec.model),
            )
        session_id = str(result.session_id or "")
        usage, used_model = usage_from_result(result)
        model = used_model or resolve_model(self.settings, spec.model)
        failed = bool(result.is_error) or str(getattr(result, "subtype", "") or "").startswith("error_")
        if spec.session:
            if failed:
                self.forget(spec.workspace, spec.session)
            elif session_id:
                self._set_session(key, spec.session, session_id)
        duration = _int(getattr(result, "duration_ms", None)) or duration
        if failed:
            error = API_ERRORS.get(turn.api_error) or redact(str(result.result or ""), [cred]).strip()
            return Outcome(
                status="failed",
                error=(error or "Claude stopped without an answer.")[:400],
                text="".join(turn.parts).strip(),
                run_id=session_id,
                agent_id=session_id,
                model=model,
                usage=usage,
                changes=changes,
                duration_ms=duration,
            )
        final = str(result.result or "").strip() or "".join(turn.parts).strip()
        return Outcome(
            status="succeeded",
            text=final,
            run_id=session_id,
            agent_id=session_id,
            model=model,
            usage=usage,
            changes=changes,
            duration_ms=duration,
        )

    async def models(self) -> list[dict[str, str]]:
        rows = [{"id": ident, "name": name} for ident, name in MODELS]
        if not any(row["id"] == self.settings.claude_model for row in rows):
            rows.insert(0, {"id": self.settings.claude_model, "name": self.settings.claude_model})
        return rows

    async def aclose(self) -> None:
        return None
