"""MCP front door: any MCP client (Hermes, Claude Code, Open WebUI, AI Workspace) can hand a task to Cursor."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from cursor_worker.jobs import Job, JobError, JobManager, JobRequest
from cursor_worker.mcp_config import DEPTH_HEADER, parse_depth
from cursor_worker.settings import TOOL_PROFILES
from cursor_worker.workspaces import list_workspaces

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Cursor Worker runs coding tasks on a Cursor agent in a workspace folder on the worker's machine. "
    "Use cursor_agent for work that needs files, a shell, a repository or several steps. "
    "Long tasks return a job_id while still running: poll it with cursor_job, stop it with cursor_cancel."
)

_MAX_WAIT = 600


def _depth(ctx: Context | None) -> int:
    try:
        headers = ctx.headers if ctx is not None else None
    except Exception:
        headers = None
    if not headers:
        return 0
    return parse_depth(headers.get(DEPTH_HEADER) or headers.get(DEPTH_HEADER.lower()))


def job_payload(job: Job, *, recent: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {"job_id": job.id, "status": job.status, "workspace": job.workspace}
    if job.session:
        out["session"] = job.session
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
        out["hint"] = "Still running. Call cursor_job with this job_id to wait for the result."
        if recent:
            steps = [
                str(e.get("text") or e.get("message") or "")
                for e in job.events
                if e.get("type") in {"tool", "status", "error"}
            ]
            out["recent_steps"] = [s for s in steps if s][-recent:]
    return out


def build_mcp(manager: JobManager) -> MCPServer:
    settings = manager.settings
    server = MCPServer(name="cursor-worker", instructions=INSTRUCTIONS)

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

    async def _wait(job: Job, seconds: float, ctx: Context | None) -> Job:
        """Wait in slices so the client sees progress while the agent works."""
        left = max(0.0, min(float(seconds), _MAX_WAIT))
        while left > 0 and not job.done:
            step = min(5.0, left)
            await manager.wait(job, step)
            left -= step
            await _progress(ctx, job)
        return job

    @server.tool(
        description=(
            "Give a task to a Cursor coding agent. It can read and edit files in the workspace, run a shell, "
            "use git, and call MCP servers. Returns the agent's final answer plus changed files and commits. "
            "If the task takes longer than wait_seconds, returns status 'running' and a job_id for cursor_job. "
            f"tools narrows what the agent may use: one of {', '.join(TOOL_PROFILES)} "
            "(full = everything, local = no web search/fetch, offline = no web and no shell, mcp = only MCP tools, "
            "none = chat only). Use the same session for follow-ups that should remember the earlier turns."
        )
    )
    async def cursor_agent(
        task: str,
        workspace: str = "",
        session: str = "",
        model: str = "",
        tools: str = "",
        mcp_servers: dict[str, Any] | None = None,
        wait_seconds: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
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
                )
            )
        except JobError as exc:
            return {"status": "rejected", "error": str(exc)}
        wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
        await _wait(job, wait, ctx)
        return job_payload(job, recent=5)

    @server.tool(
        description=(
            "Status and result of a Cursor worker job. wait_seconds waits for the job to finish "
            "(up to 600) before answering."
        )
    )
    async def cursor_job(job_id: str, wait_seconds: int = 0, ctx: Context | None = None) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            return {"status": "unknown", "error": f"No job {job_id!r}."}
        await _wait(job, wait_seconds, ctx)
        return job_payload(job, recent=5)

    @server.tool(description="Stop a running Cursor worker job.")
    async def cursor_cancel(job_id: str) -> dict[str, Any]:
        job = manager.cancel(job_id)
        if job is None:
            return {"status": "unknown", "error": f"No job {job_id!r}."}
        await manager.wait(job, 15)
        return job_payload(job)

    @server.tool(description="Workspaces the Cursor worker can use, and which one is the default.")
    async def cursor_workspaces() -> dict[str, Any]:
        return {"workspaces": list_workspaces(settings), "create_on_demand": settings.create_workspaces}

    return server
