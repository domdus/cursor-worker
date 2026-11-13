"""Run one task on a local Cursor agent and report what happened.

Ported from the AI Workspace runtime (providers/cursor.py) without the Home Assistant parts:
no transcript handoff from HA, no small-talk tool switch, no Change History callbacks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from agent_worker.events import map_sdk_message
from agent_worker.images import cursor_payload
from agent_worker.redact import redact
from agent_worker.settings import Settings, profile_tools

logger = logging.getLogger(__name__)

MISSING_KEY = (
    "CURSOR_API_KEY is not set. Create a user API key at https://cursor.com/dashboard/integrations "
    "and set it on the worker container."
)

BASE_INSTRUCTIONS = (
    "You run as a headless worker. Nobody can answer questions while you work: make reasonable "
    "assumptions, finish the task, and ask only if going on would be destructive. End with a short "
    "summary of what you did, which files you changed, and anything left open. "
    "For remote servers use plain ssh, scp, sftp or rsync. On some hosts a call waits until the user "
    "approves it; if a call is refused, do not retry it or work around it, say so in your answer."
)

Emit = Callable[[dict], Awaitable[None]]
WORKSPACE_ENV = "AGENT_WORKER_WORKSPACE"


class EngineError(RuntimeError):
    pass


@dataclass
class RunSpec:
    job_id: str
    task: str
    workspace: str
    path: Path
    session: str = ""
    model: str = ""
    effort: str = ""
    profile: str = "full"
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    images: list[dict[str, str]] = field(default_factory=list)
    # Earlier conversation, pasted only when the agent is new (OpenAI-style callers resend history).
    context: str = ""
    # Caller's standing instructions (an OpenAI system message), also only on a new agent.
    instructions: str = ""
    # Which engine runs it: cursor (Cursor SDK) or claude (Claude Code).
    engine: str = "cursor"


@dataclass
class Outcome:
    status: str  # succeeded | failed
    text: str = ""
    error: str = ""
    run_id: str = ""
    agent_id: str = ""
    model: str = ""
    usage: dict[str, Any] | None = None
    changes: dict[str, Any] | None = None
    duration_ms: int | None = None


class Engine(Protocol):
    available: bool

    async def run(self, spec: RunSpec, emit: Emit) -> Outcome: ...

    async def models(self) -> list[dict[str, str]]: ...

    async def aclose(self) -> None: ...


AgentIdStore = tuple[Callable[[str, str], str | None], Callable[[str, str, str], None]]


class CursorEngine:
    """One Cursor bridge per workspace. Agent handles stay open per session so follow-ups keep context."""

    def __init__(self, settings: Settings, agent_ids: AgentIdStore | None = None) -> None:
        self.settings = settings
        self._clients: dict[str, Any] = {}
        self._client_lock = asyncio.Lock()
        self._agents: dict[tuple[str, str], tuple[Any, str]] = {}
        self._get_agent_id, self._set_agent_id = agent_ids or (lambda _w, _s: None, lambda _w, _s, _a: None)
        self._models: tuple[float, list[dict[str, str]]] | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.cursor_api_key)

    async def _client(self, workspace: str, path: Path) -> Any:
        async with self._client_lock:
            client = self._clients.get(workspace)
            if client is None:
                from cursor_sdk import AsyncClient

                state = self.settings.state_dir / workspace
                state.mkdir(parents=True, exist_ok=True)
                logger.info("starting cursor bridge workspace=%s path=%s", workspace, path)
                # The bridge copies os.environ, and the agent's shell inherits it from the bridge:
                # this is how the ssh guard knows which workspace (and so which job) it runs for.
                previous = os.environ.get(WORKSPACE_ENV)
                os.environ[WORKSPACE_ENV] = workspace
                try:
                    client = await AsyncClient.launch_bridge(
                        workspace=str(path), state_root=str(state), timeout=180
                    )
                finally:
                    if previous is None:
                        os.environ.pop(WORKSPACE_ENV, None)
                    else:
                        os.environ[WORKSPACE_ENV] = previous
                self._clients[workspace] = client
            return client

    def _options(self, spec: RunSpec, name: str) -> Any:
        from cursor_sdk import AgentOptions, LocalAgentOptions

        tools, blocked = profile_tools(spec.profile)
        resolved = spec.model.strip() or self.settings.model
        kwargs: dict[str, Any] = {
            "api_key": self.settings.cursor_api_key,
            "name": name,
            "local": LocalAgentOptions(cwd=str(spec.path)),
        }
        if resolved:
            kwargs["model"] = sdk_model(resolved, spec.effort)
        if spec.mcp_servers and spec.profile != "none":
            kwargs["mcp_servers"] = spec.mcp_servers
        if tools is not None:
            kwargs["tools"] = tools
        if blocked:
            kwargs["disallowed_tools"] = blocked
        try:
            return AgentOptions(**kwargs)
        except TypeError:
            kwargs["model"] = resolved
            return AgentOptions(**kwargs)

    async def _open(self, spec: RunSpec) -> tuple[Any, bool]:
        from cursor_sdk import AgentNotFoundError

        client = await self._client(spec.workspace, spec.path)
        fingerprint = agent_fingerprint(spec, self.settings.model)
        options = self._options(spec, f"worker-{(spec.session or spec.job_id)[:24]}")
        if not spec.session:
            return await client.create_agent(options), True
        key = (spec.workspace, spec.session)
        cached = self._agents.get(key)
        if cached is not None and cached[1] == fingerprint:
            logger.info("reuse agent workspace=%s session=%s", spec.workspace, spec.session)
            return cached[0], False
        agent = None
        resume_id = self._get_agent_id(spec.workspace, spec.session) if cached is None else None
        if resume_id:
            try:
                logger.info("resume agent session=%s agent=%s", spec.session, resume_id)
                agent = await client.resume_agent(resume_id, options)
            except AgentNotFoundError:
                logger.info("agent %s is gone, creating a new one", resume_id)
                agent = None
        created = agent is None
        if agent is None:
            agent = await client.create_agent(options)
        self._agents[key] = (agent, fingerprint)
        agent_id = str(getattr(agent, "agent_id", "") or "")
        if agent_id:
            self._set_agent_id(spec.workspace, spec.session, agent_id)
        return agent, created

    def forget(self, workspace: str, session: str) -> None:
        self._agents.pop((workspace, session), None)
        self._set_agent_id(workspace, session, "")

    async def run(self, spec: RunSpec, emit: Emit) -> Outcome:
        if not self.settings.cursor_api_key:
            raise EngineError(MISSING_KEY)
        started = time.monotonic()
        before = await git_head(spec.path)
        await emit({"type": "status", "text": "Starting agent..."})
        try:
            agent, created = await self._open(spec)
        except Exception as exc:
            raise EngineError(self._public(exc)) from exc
        agent_id = str(getattr(agent, "agent_id", "") or "")
        message = compose_message(spec, created, self.settings.instructions)
        try:
            run = await _start_run(agent, cursor_payload(message, spec.images), spec.mcp_servers)
        except Exception as exc:
            if spec.session:
                self.forget(spec.workspace, spec.session)
            raise EngineError(self._public(exc)) from exc
        run_id = str(getattr(run, "id", "") or getattr(run, "run_id", "") or "")
        logger.info("run started job=%s agent=%s run=%s created=%s", spec.job_id, agent_id, run_id, created)
        parts: list[str] = []
        stream_error = ""
        try:
            async for sdk_message in run.messages():
                for event in map_sdk_message(sdk_message):
                    if event.get("type") == "text":
                        parts.append(str(event.get("delta") or ""))
                    elif event.get("type") == "error" and not stream_error:
                        stream_error = str(event.get("message") or "")
                    await emit(event)
            result = await run.wait()
        except asyncio.CancelledError:
            await _cancel_run(run)
            if spec.session:
                self.forget(spec.workspace, spec.session)
            raise
        except Exception as exc:
            if spec.session:
                self.forget(spec.workspace, spec.session)
            raise EngineError(self._public(exc)) from exc
        status = str(getattr(result, "status", "") or "")
        used_model = model_id(getattr(result, "model", None)) or spec.model or self.settings.model
        changes = await git_changes(spec.path, before)
        duration = getattr(result, "duration_ms", None)
        if duration is None:
            duration = int((time.monotonic() - started) * 1000)
        usage = await cursor_usage(agent, result, run_id)
        if status == "error":
            if spec.session:
                self.forget(spec.workspace, spec.session)
            return Outcome(
                status="failed",
                error=run_error_text(result, run_id, used_model, stream_error),
                text="".join(parts).strip(),
                run_id=run_id,
                agent_id=agent_id,
                model=used_model,
                usage=usage,
                changes=changes,
                duration_ms=duration,
            )
        final = str(getattr(result, "result", None) or "").strip() or "".join(parts).strip()
        return Outcome(
            status="succeeded",
            text=final,
            run_id=run_id,
            agent_id=agent_id,
            model=used_model,
            usage=usage,
            changes=changes,
            duration_ms=duration,
        )

    async def models(self) -> list[dict[str, str]]:
        """Models the Cursor account can use, cached for an hour."""
        if not self.settings.cursor_api_key:
            return []
        if self._models and self._models[0] > time.monotonic():
            return self._models[1]
        key = self.settings.cursor_api_key

        def fetch() -> list[dict[str, str]]:
            from cursor_sdk import Cursor

            return [{"id": item.id, "name": item.display_name or item.id} for item in Cursor.models.list(api_key=key) if item.id]

        try:
            rows = await asyncio.to_thread(fetch)
        except Exception as exc:
            logger.warning("cursor model list failed: %s", self._public(exc))
            return [{"id": self.settings.model, "name": self.settings.model}]
        self._models = (time.monotonic() + 3600, rows)
        return rows

    def _public(self, exc: BaseException) -> str:
        text = redact(str(exc), [self.settings.cursor_api_key])
        if getattr(exc, "is_retryable", None):
            text = f"{text} (retryable)"
        return text

    async def aclose(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        self._agents.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                logger.debug("cursor bridge close failed", exc_info=True)


def compose_message(spec: RunSpec, created: bool, standing: str = "") -> str:
    """A new agent gets the worker instructions and the caller's context; a warm one only the task."""
    if not created:
        return spec.task
    blocks = [BASE_INSTRUCTIONS]
    if standing:
        blocks.append(standing)
    if spec.instructions.strip():
        blocks.append(spec.instructions.strip())
    if spec.context.strip():
        blocks.append("Conversation so far:\n" + spec.context.strip())
    blocks.append(spec.task)
    return "\n\n".join(blocks)


async def _start_run(agent: Any, payload: Any, mcp_servers: dict) -> Any:
    # The SDK does not keep MCP servers with a resumed agent, so pass them with every send.
    if mcp_servers:
        try:
            return await agent.send(payload, {"mcp_servers": mcp_servers})
        except TypeError:
            pass
    return await agent.send(payload)


async def _cancel_run(run: Any) -> None:
    cancel = getattr(run, "cancel", None)
    if not callable(cancel):
        return
    try:
        await asyncio.shield(asyncio.wait_for(cancel(), 10))
    except Exception:
        logger.debug("cursor run cancel failed", exc_info=True)


# ---- model selection -------------------------------------------------------------------------


def model_id(value: Any) -> str:
    if value is None:
        return ""
    ident = getattr(value, "id", None)
    if ident:
        return str(ident).strip()
    if isinstance(value, dict):
        return str(value.get("id") or "").strip()
    return str(value).strip()


def sdk_model(ident: str, effort: str = "") -> Any:
    name = (ident or "").strip()
    if not name:
        return None
    effort = (effort or "").strip()
    composer = name.lower() in {"composer-2.5", "composer-2"}
    if not composer and not effort:
        return name
    params = [{"id": "fast", "value": "false" if effort == "high" else "true"}]
    if effort in {"high", "high_fast"}:
        params.append({"id": "effort", "value": "high"})
    try:
        from cursor_sdk import ModelParameterValue, ModelSelection

        return ModelSelection(id=name, params=[ModelParameterValue(id=p["id"], value=p["value"]) for p in params])
    except Exception:
        return {"id": name, "params": params}


def agent_fingerprint(spec: RunSpec, default_model: str) -> str:
    """Stable shape of an agent's configuration: names and endpoints, not rotating tokens."""
    servers = {
        str(name): {"url": str(cfg.get("url") or ""), "type": str(cfg.get("type") or ""), "command": str(cfg.get("command") or "")}
        for name, cfg in sorted((spec.mcp_servers or {}).items())
    }
    return json.dumps(
        {
            "mcp": servers,
            "model": (spec.model or default_model).strip(),
            "effort": spec.effort,
            "profile": spec.profile,
        },
        sort_keys=True,
    )


# ---- results ---------------------------------------------------------------------------------


def _nested_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in {"", "error", "none"} or text.startswith("<") or "object at 0x" in text:
            return ""
        return text
    if isinstance(value, dict):
        for key in ("message", "msg", "detail", "reason", "code"):
            text = _nested_text(value.get(key))
            if text:
                return text
        return ""
    for key in ("message", "msg", "detail", "reason", "code"):
        text = _nested_text(getattr(value, key, None))
        if text:
            return text
    return _nested_text(str(value))


def run_error_text(result: Any, run_id: str, model: str, stream_error: str = "") -> str:
    for attr in ("error_message", "error", "result", "message", "error_code"):
        text = _nested_text(getattr(result, attr, None))
        if text:
            return text
    if stream_error:
        return stream_error
    suffix = f" ({run_id})" if run_id else ""
    return f"Run failed{suffix}. Cursor ended the {model} run with no text."


def _int(value: object) -> int:
    try:
        if value is None or value is False:
            return 0
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def usage_from(blob: Any) -> dict[str, Any]:
    data = blob if isinstance(blob, dict) else {}
    if blob is not None and not data:
        for name in (
            "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "charged_cents", "total_cents",
        ):
            if hasattr(blob, name):
                data[name] = getattr(blob, name)
    prompt = _int(data.get("prompt_tokens") or data.get("input_tokens") or data.get("inputTokens"))
    completion = _int(data.get("completion_tokens") or data.get("output_tokens") or data.get("outputTokens"))
    cents = data.get("charged_cents", data.get("total_cents"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": _int(data.get("cache_read_tokens") or data.get("cache_read_input_tokens")),
        "cache_write_tokens": _int(data.get("cache_write_tokens") or data.get("cache_creation_input_tokens")),
        "total_tokens": _int(data.get("total_tokens")) or prompt + completion,
        "charged_cents": _int(cents) if cents is not None else None,
    }


async def cursor_usage(agent: Any, result: Any, run_id: str) -> dict[str, Any] | None:
    billed = None
    if run_id and hasattr(agent, "get_usage"):
        try:
            billed = await asyncio.wait_for(agent.get_usage(run_id=run_id), 4)
        except Exception:
            logger.debug("cursor get_usage skipped run=%s", run_id, exc_info=True)
    tokens = usage_from(getattr(result, "usage", None))
    extra = usage_from(billed)
    merged = {key: extra[key] or tokens[key] for key in tokens}
    if extra["charged_cents"] is None:
        merged["charged_cents"] = tokens["charged_cents"]
    if not any(merged.values()):
        return None
    return merged


# ---- git -------------------------------------------------------------------------------------


async def _git(path: Path, *args: str) -> str | None:
    if not (path / ".git").exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(path), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except Exception:
        return None
    return out.decode("utf-8", "replace") if proc.returncode == 0 else None


async def git_head(path: Path) -> str:
    return ((await _git(path, "rev-parse", "HEAD")) or "").strip()


async def git_changes(path: Path, before: str) -> dict[str, Any] | None:
    """What the run left in a git workspace: uncommitted files and any new commits."""
    status = await _git(path, "status", "--porcelain")
    if status is None:
        return None
    after = await git_head(path)
    commits: list[str] = []
    if before and after and before != after:
        log = await _git(path, "log", "--oneline", f"{before}..{after}")
        commits = [line for line in (log or "").splitlines() if line.strip()][:50]
    files = [line for line in status.splitlines() if line.strip()]
    return {"files": files[:200], "more_files": max(0, len(files) - 200), "commits": commits, "head": after}
