"""HTTP entry point: job API, OpenAI-compatible /v1, MCP at /mcp, one bearer token for all of it."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from agent_worker import openai_compat
from agent_worker.engine import CursorEngine, Engine
from agent_worker.images import parse_images
from agent_worker.jobs import JobError, JobManager, JobRequest, Store
from agent_worker.mcp_config import DEPTH_HEADER, parse_depth
from agent_worker.mcp_server import build_mcp
from agent_worker.settings import LOOPBACK_HOSTS, Settings
from agent_worker.version import VERSION
from agent_worker.workspaces import WorkspaceError, list_workspaces, resolve

logger = logging.getLogger(__name__)

_OPEN_PATHS = {"/health"}
# Called by the ssh guard inside the container. No bearer token, but only from loopback.
_INTERNAL_PATHS = {"/internal/remote"}


def _loopback_host(header: str) -> bool:
    """Host header check for the open loopback mode, so a rebound DNS name cannot reach the API."""
    host = str(header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host in LOOPBACK_HOSTS


def _token_ok(settings: Settings, header: str) -> bool:
    if not settings.token:
        return settings.open_without_token()
    scheme, _, value = str(header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), settings.token.encode())


class RemoteCall(BaseModel):
    workspace: str = Field(default="", max_length=64)
    cwd: str = Field(default="", max_length=4096)
    tool: str = Field(default="ssh", max_length=10)
    alias: str = Field(default="", max_length=255)
    hostname: str = Field(default="", max_length=255)
    user: str = Field(default="", max_length=255)
    command: str = Field(default="", max_length=4000)


class Decision(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=300)
    wait: int = Field(default=0, ge=0, le=600)


class LoginCode(BaseModel):
    code: str = Field(default="", max_length=512)


class JobBody(BaseModel):
    task: str = Field(max_length=320_000)
    engine: str = Field(default="", max_length=20)
    workspace: str = Field(default="", max_length=64)
    session: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=200)
    effort: str = Field(default="", max_length=20)
    tools: str = Field(default="", max_length=20)
    mcp_servers: dict[str, Any] | None = None
    images: list[Any] = Field(default_factory=list, max_length=4)
    instructions: str = Field(default="", max_length=60_000)
    wait: int = Field(default=0, ge=0, le=600)


def _sse(payload: dict[str, Any]) -> str:
    return f"event: {payload.get('type', 'message')}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:\t%(name)s: %(message)s")
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.data_dir / "worker.sqlite")
    if engine is None:
        from agent_worker.claude_engine import ClaudeEngine
        from agent_worker.engines import EngineRouter

        ids = (store.agent_id, store.set_agent_id)
        engine = EngineRouter(
            {"cursor": CursorEngine(settings, ids), "claude": ClaudeEngine(settings, ids)},
            settings.default_engine,
        )
    manager = JobManager(settings, engine, store)
    mcp = build_mcp(manager)
    # Our own middleware checks the token and the Host header; the MCP app's host check would
    # otherwise refuse every name except 127.0.0.1 when the worker listens on 0.0.0.0.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateful, so the server knows the client's capabilities and can ask the user (elicitation)
        # when a job needs approval.
        stateless_http=False,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from agent_worker.git_bootstrap import GitBootstrapError, bootstrap_git
        from agent_worker.ssh_setup import prepare_environment, setup_ssh

        prepare_environment(settings)
        try:
            app.state.ssh = setup_ssh()
        except OSError as exc:
            logger.error("Could not set up ~/.ssh from SSH_* variables: %s", exc)

        try:
            _name, path = resolve(settings, "", create=True)
            bootstrap_git(path)
        except (GitBootstrapError, WorkspaceError) as exc:
            # A wrong token or an unreachable repo must not take the worker down with it.
            logger.error("Could not set up GIT_REPO in the default workspace: %s", exc)
        if not engine.available:
            logger.warning(
                "Neither CURSOR_API_KEY nor a Claude credential is set. Jobs will fail until one is."
            )
        if not settings.token and not settings.open_without_token():
            logger.warning("No WORKER_TOKEN and HOST=%s: every request is refused until a token is set.", settings.host)
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await manager.aclose()
                login = getattr(app.state, "claude_login", None)
                if login is not None:
                    login.close()
                await engine.aclose()
                store.close()

    app = FastAPI(title="Agent Worker", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs = manager
    app.state.ssh = None

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path in _INTERNAL_PATHS:
            client = request.client.host if request.client else ""
            if client not in LOOPBACK_HOSTS:
                return JSONResponse({"detail": "Only for the ssh guard inside the container."}, status_code=403)
            return await call_next(request)
        open_mode = not settings.token and settings.open_without_token()
        if open_mode and not _loopback_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "Without WORKER_TOKEN the worker only answers on 127.0.0.1."}, status_code=403)
        if request.url.path not in _OPEN_PATHS and not _token_ok(settings, request.headers.get("authorization", "")):
            detail = "Bearer token is missing or wrong." if settings.token else "Set WORKER_TOKEN on the worker."
            return JSONResponse({"detail": detail}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    def _engine_ready(name: str) -> bool:
        engines = getattr(engine, "engines", None)
        if engines is None:
            return engine.available if name == "cursor" else False
        return name in engines and engines[name].available

    def _login_manager():
        from agent_worker.claude_login import LoginManager

        if getattr(app.state, "claude_login", None) is None:
            app.state.claude_login = LoginManager()
        return app.state.claude_login

    @app.get("/health")
    async def health() -> dict[str, Any]:
        from agent_worker.claude_engine import credential_source, sdk_installed

        default = engine.default() if hasattr(engine, "default") else "cursor"
        return {
            "ok": True,
            "version": VERSION,
            "cursor": _engine_ready("cursor"),
            "claude": {
                "ready": _engine_ready("claude"),
                "installed": sdk_installed(),
                "credential": credential_source(settings),
                "model": settings.claude_model,
            },
            "default_engine": default,
            "model": settings.model,
            "tool_profile": settings.tool_profile,
            "token_required": bool(settings.token) or not settings.open_without_token(),
            "ssh": {
                "configured": bool(app.state.ssh and app.state.ssh.configured),
                "hosts": list(app.state.ssh.hosts) if app.state.ssh else [],
                "approval_hosts": list(settings.ssh_approval_hosts),
            },
        }

    @app.post("/auth/claude/login")
    async def claude_login_start() -> dict[str, Any]:
        """Start Anthropic's `claude setup-token`; returns the sign-in link to open."""
        from agent_worker.claude_login import LoginError

        try:
            login = await _login_manager().start()
        except LoginError as exc:
            raise HTTPException(status_code=501 if exc.key == "cli_missing" else 502, detail=exc.key) from None
        return {
            "id": login.id,
            "url": login.link,
            "next": f"Open the url, sign in, then POST the code Anthropic shows to /auth/claude/login/{login.id}.",
        }

    @app.post("/auth/claude/login/{login_id}")
    async def claude_login_finish(login_id: str, body: LoginCode) -> dict[str, Any]:
        """Hand over the code; the Claude Plan token is saved in the data volume (never returned or logged)."""
        from agent_worker.claude_engine import credential_source
        from agent_worker.claude_login import LoginError, save_token

        try:
            token = await _login_manager().finish(login_id, body.code)
        except LoginError as exc:
            status = {"expired": 410, "bad_code": 400, "code_rejected": 400}.get(exc.key, 502)
            raise HTTPException(status_code=status, detail=exc.key) from None
        save_token(settings.claude_token_path, token)
        logger.info("Claude sign-in finished; Plan token saved")
        source = credential_source(settings)
        return {
            "ok": True,
            "credential": source,
            "note": "Saved. Claude jobs use it now."
            if source == "signed-in"
            else "Saved, but CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY on the container wins over it.",
        }

    @app.delete("/auth/claude/login/{login_id}", status_code=204)
    async def claude_login_cancel(login_id: str) -> None:
        _login_manager().cancel(login_id)

    @app.delete("/auth/claude/token")
    async def claude_sign_out() -> dict[str, Any]:
        """Forget the signed-in Plan token (environment variables are not touched)."""
        from agent_worker.claude_login import forget_token

        return {"removed": forget_token(settings.claude_token_path)}

    @app.get("/workspaces")
    async def workspaces() -> dict[str, Any]:
        return {"workspaces": list_workspaces(settings), "create_on_demand": settings.create_workspaces}

    @app.post("/jobs", status_code=202)
    async def create_job(request: Request, body: JobBody):
        try:
            job = manager.submit(
                JobRequest(
                    task=body.task,
                    engine=body.engine,
                    workspace=body.workspace,
                    session=body.session,
                    model=body.model,
                    effort=body.effort,
                    tools=body.tools,
                    mcp_servers=body.mcp_servers,
                    images=parse_images(body.images),
                    instructions=body.instructions,
                    depth=parse_depth(request.headers.get(DEPTH_HEADER)),
                    origin="api",
                )
            )
        except JobError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        if body.wait:
            await manager.wait(job, body.wait, stop_on_approval=True)
        return JSONResponse(job.as_dict(), status_code=200 if job.done else 202)

    @app.get("/jobs")
    async def list_jobs(limit: int = 50, workspace: str = "", status: str = "") -> dict[str, Any]:
        return {"jobs": [job.summary() for job in manager.list(limit, workspace, status)]}

    def _job_or_404(job_id: str):
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, wait: int = 0) -> dict[str, Any]:
        job = _job_or_404(job_id)
        if wait:
            await manager.wait(job, min(max(wait, 0), 600), stop_on_approval=True)
        return job.as_dict()

    @app.get("/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str, after: int = 0, follow: bool = True):
        job = _job_or_404(job_id)
        if not follow:
            return {"events": job.events[max(0, after) :]}

        async def stream():
            async for event in manager.follow(job, after):
                if await request.is_disconnected():
                    return
                if event.get("type") == "ping":
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
            yield _sse({"type": "end", "status": job.status, "job": job.as_dict()})

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @app.post("/jobs/{job_id}/approve")
    async def approve_job(job_id: str, body: Decision) -> dict[str, Any]:
        """A human's answer to a job in needs_approval."""
        try:
            job = manager.decide(job_id, body.approve, body.note)
        except JobError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        await asyncio.sleep(0)
        if body.wait:
            await manager.wait(job, body.wait, stop_on_approval=True)
        return job.as_dict()

    @app.post("/internal/remote")
    async def remote_call(body: RemoteCall) -> dict[str, Any]:
        allow, reason = await manager.remote_call(
            body.workspace, body.tool, body.alias, body.hostname, body.user, body.command
        )
        return {"allow": allow, "reason": reason}

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        job = _job_or_404(job_id)
        manager.cancel(job_id)
        await manager.wait(job, 15)
        return job.as_dict()

    app.include_router(openai_compat.router)
    # Last: everything not matched above goes to the MCP app, which only answers /mcp.
    app.mount("/", mcp_app)
    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
