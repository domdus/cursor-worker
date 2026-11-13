"""MCP front door: any MCP client (Hermes, Claude Code, Open WebUI, AI Workspace) can hand a task to a coding agent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp import types as mtypes
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import NoBackChannelError
from pydantic import BaseModel, Field

from agent_worker.jobs import Job, JobError, JobManager, JobRequest
from agent_worker.mcp_config import DEPTH_HEADER, parse_depth
from agent_worker.settings import TOOL_PROFILES
from agent_worker.workspaces import list_workspaces

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Agent Worker runs coding tasks on a coding agent (Cursor or Claude Code) in a workspace folder on the "
    "worker's machine. Use agent_run for work that needs files, a shell, a repository or several steps. "
    "Long tasks return a job_id while still running: poll it with agent_job, stop it with agent_cancel. "
    "Status needs_approval means a remote command waits for the user: show it to them and let them decide."
)

_MAX_WAIT = 600


class ApprovalAnswer(BaseModel):
    approve: bool = Field(description="Allow this command on that server?")
    note: str = Field(default="", description="Optional note for the agent")


_STATE = "agent-worker-approval:"
_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean", "title": "Allow", "description": "Allow this command on that server?"},
        "note": {"type": "string", "title": "Note", "description": "Optional note for the agent"},
    },
    "required": ["approve"],
}


def _approval_message(job: Job, approval: dict[str, Any]) -> str:
    target = f"{approval['user']}@{approval['host']}" if approval.get("user") else approval["host"]
    detail = approval.get("command") or "(interactive session or file transfer)"
    real = approval.get("hostname")
    return (
        f"Agent job {job.id} wants to run {approval['tool']} on {target}"
        + (f" ({real})" if real and real != approval["host"] else "")
        + f":\n\n{detail}\n\nAllow it?"
    )


def _answer_from(result: Any) -> tuple[bool, str] | None:
    """(allow, note) from an elicitation answer, or None when the user dismissed it."""
    action = getattr(result, "action", "")
    if action not in {"accept", "decline"}:
        return None
    if action == "decline":
        return False, "declined"
    data = getattr(result, "data", None)
    content = getattr(result, "content", None)
    if data is not None:
        return bool(getattr(data, "approve", False)), str(getattr(data, "note", "") or "")
    if isinstance(content, dict):
        return bool(content.get("approve")), str(content.get("note") or "")
    return False, ""


def _can_elicit(ctx: Context | None) -> bool:
    try:
        caps = ctx.client_capabilities if ctx is not None else None
    except Exception:
        return False
    return caps is not None and getattr(caps, "elicitation", None) is not None


def _depth(ctx: Context | None) -> int:
    try:
        headers = ctx.headers if ctx is not None else None
    except Exception:
        headers = None
    if not headers:
        return 0
    return parse_depth(headers.get(DEPTH_HEADER) or headers.get(DEPTH_HEADER.lower()))


def job_payload(job: Job, *, recent: int = 0, approval_tool: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"job_id": job.id, "status": job.status, "engine": job.engine, "workspace": job.workspace}
    if job.session:
        out["session"] = job.session
    if job.status == "needs_approval" and job.pending_approval:
        out["approval"] = job.pending_approval
        out["hint"] = (
            "The agent is paused until a human approves this remote command. Show it to the user and ask. "
            "Do not decide yourself. The user answers with POST /jobs/{id}/approve on the worker"
            + (" or you pass their answer to agent_approve" if approval_tool else "")
            + "; then call agent_job to wait for the result."
        ).replace("{id}", job.id)
        return out
    if job.done:
        if job.result:
            out["result"] = job.result
        if job.error:
            out["error"] = job.error
        if job.changes:
            out["changes"] = job.changes
        if job.usage:
            out["usage"] = job.usage
    else:
        out["hint"] = "Still running. Call agent_job with this job_id to wait for the result."
        if recent:
            steps = [
                str(e.get("text") or e.get("message") or "")
                for e in job.events
                if e.get("type") in {"tool", "status", "error"}
            ]
            out["recent_steps"] = [s for s in steps if s][-recent:]
    return out


_ENGINE_LABELS = {"cursor": "cursor (Cursor SDK)", "claude": "claude (Claude Code)"}


def _engines_note(engine) -> str:
    """Which engines this worker has, for the agent_run description. Set up at start; signing in later works too."""
    engines = getattr(engine, "engines", None) or {"cursor": engine}
    default = engine.default() if hasattr(engine, "default") else "cursor"
    names = [_ENGINE_LABELS.get(name, name) + (" (default)" if name == default else "") for name in engines]
    return "engine picks the agent: " + ", ".join(names) + ". Leave it empty for the default."


def build_mcp(manager: JobManager) -> MCPServer:
    settings = manager.settings
    server = MCPServer(name="agent-worker", instructions=INSTRUCTIONS)
    engines_note = _engines_note(manager.engine)

    async def _progress(ctx: Context | None, job: Job) -> None:
        if ctx is None:
            return
        try:
            last = next(
                (str(e.get("text") or "") for e in reversed(job.events) if e.get("type") in {"tool", "status"}),
                "",
            )
            await ctx.report_progress(len(job.events), None, last or job.status)
        except Exception:
            logger.debug("progress report failed", exc_info=True)

    def _apply(job: Job, decided: tuple[bool, str]) -> bool:
        try:
            manager.decide(job.id, *decided)
        except JobError:
            return False
        return True

    async def _ask_human(job: Job, ctx: Context | None) -> bool | mtypes.InputRequiredResult:
        """Ask the user through the MCP client (elicitation), never the calling model.

        True once decided. Older protocol versions get a server-initiated request on the open
        stream; 2026-07-28 clients get an InputRequiredResult and retry the call with the answer.
        """
        approval = job.pending_approval
        if approval is None or not _can_elicit(ctx):
            return False
        message = _approval_message(job, approval)
        try:
            answer = await asyncio.wait_for(ctx.elicit(message, ApprovalAnswer), settings.ssh_approval_timeout)
        except NoBackChannelError:
            return mtypes.InputRequiredResult(
                input_requests={
                    f"approve-{approval['id']}": mtypes.ElicitRequest(
                        params=mtypes.ElicitRequestFormParams(message=message, requested_schema=_SCHEMA)
                    )
                },
                request_state=_STATE + json.dumps({"job": job.id, "approval": approval["id"]}),
            )
        except Exception:
            logger.info("elicitation failed for job %s", job.id, exc_info=True)
            return False
        decided = _answer_from(answer)
        if decided is None or not _apply(job, decided):
            return False
        await asyncio.sleep(0)
        return True

    async def _resume(ctx: Context | None) -> Job | None:
        """On a retry after InputRequiredResult: apply the user's answer, return the job it belongs to."""
        try:
            state = ctx.request_state if ctx is not None else None
        except Exception:
            state = None
        if not state or not state.startswith(_STATE):
            return None
        try:
            ref = json.loads(state[len(_STATE) :])
        except ValueError:
            return None
        job = manager.get(str(ref.get("job") or ""))
        if job is None:
            return None
        response = (ctx.input_responses or {}).get(f"approve-{ref.get('approval')}")
        pending = job.pending_approval or {}
        if response is not None and pending.get("id") == ref.get("approval"):
            decided = _answer_from(response)
            if decided is not None and _apply(job, decided):
                await asyncio.sleep(0)
        return job

    async def _wait(job: Job, seconds: float, ctx: Context | None) -> mtypes.InputRequiredResult | None:
        """Wait in slices so the client sees progress; stop to ask when the job needs approval."""
        left = max(0.0, min(float(seconds), _MAX_WAIT))
        while not job.done:
            if job.status == "needs_approval":
                asked = await _ask_human(job, ctx)
                if isinstance(asked, mtypes.InputRequiredResult):
                    return asked
                if not asked:
                    break
                left = max(left, float(settings.mcp_wait_seconds))
                continue
            if left <= 0:
                break
            step = min(5.0, left)
            await manager.wait(job, step, stop_on_approval=True)
            left -= step
            await _progress(ctx, job)
        return None

    def payload(job: Job, recent: int = 0) -> dict[str, Any]:
        return job_payload(job, recent=recent, approval_tool=settings.approval_tool)

    @server.tool(
        description=(
            "Give a task to a coding agent. It can read and edit files in the workspace, run a shell, "
            "use git, ssh to servers, and call MCP servers. Returns the agent's final answer plus changed files "
            "and commits. If the task takes longer than wait_seconds, returns status 'running' and a job_id for "
            f"agent_job. tools narrows what the agent may use: one of {', '.join(TOOL_PROFILES)} "
            "(full = everything, local = no web search/fetch, offline = no web and no shell, mcp = only MCP tools, "
            "none = chat only). Use the same session for follow-ups that should remember the earlier turns. "
            + engines_note
        )
    )
    async def agent_run(
        task: str,
        workspace: str = "",
        session: str = "",
        model: str = "",
        tools: str = "",
        mcp_servers: dict[str, Any] | None = None,
        wait_seconds: int | None = None,
        engine: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        job = await _resume(ctx)
        if job is None:
            try:
                job = manager.submit(
                    JobRequest(
                        task=task,
                        workspace=workspace,
                        session=session,
                        model=model,
                        tools=tools,
                        mcp_servers=mcp_servers,
                        depth=_depth(ctx),
                        origin="mcp",
                        engine=engine,
                    )
                )
            except JobError as exc:
                return {"status": "rejected", "error": str(exc)}
        wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
        asked = await _wait(job, wait, ctx)
        return asked if asked is not None else payload(job, recent=5)

    @server.tool(
        description=(
            "Status and result of an agent job. wait_seconds waits for the job to finish "
            "(up to 600) before answering."
        )
    )
    async def agent_job(job_id: str, wait_seconds: int = 0, ctx: Context | None = None) -> dict[str, Any]:
        job = await _resume(ctx) or manager.get(job_id)
        if job is None:
            return {"status": "unknown", "error": f"No job {job_id!r}."}
        asked = await _wait(job, wait_seconds, ctx)
        return asked if asked is not None else payload(job, recent=5)

    @server.tool(description="Stop a running agent job.")
    async def agent_cancel(job_id: str) -> dict[str, Any]:
        job = manager.cancel(job_id)
        if job is None:
            return {"status": "unknown", "error": f"No job {job_id!r}."}
        await manager.wait(job, 15)
        return payload(job)

    if settings.approval_tool:

        @server.tool(
            description=(
                "Pass the USER's answer to a job waiting for approval (status needs_approval). Only call this "
                "after asking the user and getting their explicit answer; never decide on your own."
            )
        )
        async def agent_approve(
            job_id: str, approve: bool, note: str = "", wait_seconds: int | None = None, ctx: Context | None = None
        ) -> dict[str, Any]:
            try:
                job = manager.decide(job_id, approve, note)
            except JobError as exc:
                return {"status": "rejected", "error": str(exc)}
            await asyncio.sleep(0)
            wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
            asked = await _wait(job, wait, ctx)
            return asked if asked is not None else payload(job, recent=5)

    @server.tool(description="Workspaces the agents can use, and which one is the default.")
    async def agent_workspaces() -> dict[str, Any]:
        return {"workspaces": list_workspaces(settings), "create_on_demand": settings.create_workspaces}

    return server
