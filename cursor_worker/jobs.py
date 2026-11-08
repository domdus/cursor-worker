"""Jobs: queue, run, record events, cancel. One job writes to a workspace at a time."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from cursor_worker.engine import Engine, EngineError, RunSpec
from cursor_worker.mcp_config import McpConfigError, load_mcp_servers, servers_from_request, with_depth
from cursor_worker.settings import Settings, narrow_profile, touches_files
from cursor_worker.workspaces import WorkspaceError, resolve

logger = logging.getLogger(__name__)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_MAX_EVENTS = 5000


class JobError(ValueError):
    """The request cannot become a job (bad workspace, bad MCP config, nested call)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class JobRequest:
    task: str
    workspace: str = ""
    session: str = ""
    model: str = ""
    effort: str = ""
    tools: str = ""
    mcp_servers: dict[str, Any] | None = None
    images: list[dict[str, str]] = field(default_factory=list)
    context: str = ""
    instructions: str = ""
    depth: int = 0
    origin: str = "api"


@dataclass
class Job:
    id: str
    task: str
    workspace: str
    session: str
    model: str
    profile: str
    origin: str
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    result: str = ""
    error: str = ""
    run_id: str = ""
    agent_id: str = ""
    usage: dict[str, Any] | None = None
    changes: dict[str, Any] | None = None
    duration_ms: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    approved_hosts: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task_handle: asyncio.Task | None = field(default=None, repr=False)
    approval_future: asyncio.Future | None = field(default=None, repr=False)
    approval_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def summary(self, *, task_chars: int = 300) -> dict[str, Any]:
        task = self.task if len(self.task) <= task_chars else self.task[:task_chars] + "…"
        return {
            "id": self.id,
            "status": self.status,
            "workspace": self.workspace,
            "session": self.session or None,
            "model": self.model,
            "tools": self.profile,
            "origin": self.origin,
            "task": task,
            "created_at": self.created_at,
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
            "approval": self.pending_approval,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.summary(task_chars=10_000),
            "result": self.result or None,
            "error": self.error or None,
            "run_id": self.run_id or None,
            "agent_id": self.agent_id or None,
            "usage": self.usage,
            "changes": self.changes,
            "duration_ms": self.duration_ms,
            "events": len(self.events),
            "approved_hosts": sorted(self.approved_hosts),
        }


class Store:
    """SQLite record of jobs, their events and session -> agent ids. Survives restarts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (job_id TEXT, seq INTEGER, data TEXT NOT NULL, PRIMARY KEY (job_id, seq));
            CREATE TABLE IF NOT EXISTS sessions (workspace TEXT, session TEXT, agent_id TEXT, PRIMARY KEY (workspace, session));
            """
        )

    def save(self, job: Job) -> None:
        data = json.dumps({**job.as_dict(), "task": job.task}, ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, created_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (job.id, job.created_at, data),
            )

    def add_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO events (job_id, seq, data) VALUES (?, ?, ?)",
                (job_id, event["seq"], json.dumps(event, ensure_ascii=False)),
            )

    def load(self, limit: int) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        with self._lock:
            rows = self._db.execute("SELECT id, data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for job_id, data in rows:
                events = [
                    json.loads(raw)
                    for (raw,) in self._db.execute("SELECT data FROM events WHERE job_id = ? ORDER BY seq", (job_id,))
                ]
                out.append((json.loads(data), events))
            return out

    def prune(self, keep: int) -> None:
        with self._lock:
            old = [
                row[0]
                for row in self._db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?", (keep,))
            ]
            for job_id in old:
                self._db.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
                self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def agent_id(self, workspace: str, session: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT agent_id FROM sessions WHERE workspace = ? AND session = ?", (workspace, session)
            ).fetchone()
        return (row[0] or None) if row else None

    def set_agent_id(self, workspace: str, session: str, agent_id: str) -> None:
        with self._lock:
            if agent_id:
                self._db.execute(
                    "INSERT OR REPLACE INTO sessions (workspace, session, agent_id) VALUES (?, ?, ?)",
                    (workspace, session, agent_id),
                )
            else:
                self._db.execute("DELETE FROM sessions WHERE workspace = ? AND session = ?", (workspace, session))

    def close(self) -> None:
        with self._lock:
            self._db.close()


class JobManager:
    def __init__(self, settings: Settings, engine: Engine, store: Store) -> None:
        self.settings = settings
        self.engine = engine
        self.store = store
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._restore()

    def _restore(self) -> None:
        for data, events in reversed(self.store.load(self.settings.keep_jobs)):
            job = Job(
                id=data["id"],
                task=data.get("task") or "",
                workspace=data.get("workspace") or "",
                session=data.get("session") or "",
                model=data.get("model") or "",
                profile=data.get("tools") or "",
                origin=data.get("origin") or "api",
                status=data.get("status") or "failed",
                created_at=data.get("created_at") or _now(),
                started_at=data.get("started_at") or "",
                finished_at=data.get("finished_at") or "",
                result=data.get("result") or "",
                error=data.get("error") or "",
                run_id=data.get("run_id") or "",
                agent_id=data.get("agent_id") or "",
                usage=data.get("usage"),
                changes=data.get("changes"),
                duration_ms=data.get("duration_ms"),
                events=events,
            )
            if not job.done:
                job.status = "failed"
                job.error = "The worker restarted while this job was running."
                job.finished_at = _now()
                self.store.save(job)
            job.changed.set()
            self.jobs[job.id] = job

    # ---- submit ------------------------------------------------------------------------------

    def _build(self, req: JobRequest, job_id: str) -> RunSpec:
        if req.depth >= self.settings.max_call_depth:
            raise JobError(
                "This call came from a Cursor worker agent. Nested worker jobs are refused (MAX_CALL_DEPTH).",
                status=409,
            )
        task = (req.task or "").strip()
        if not task:
            raise JobError("task is empty.")
        try:
            name, path = resolve(self.settings, req.workspace)
        except WorkspaceError as exc:
            raise JobError(str(exc)) from exc
        if req.mcp_servers and not self.settings.allow_request_mcp:
            raise JobError("This worker does not accept MCP servers with a job (ALLOW_REQUEST_MCP=false).")
        try:
            servers = load_mcp_servers(self.settings.mcp_config_path)
            servers.update(servers_from_request(req.mcp_servers))
        except McpConfigError as exc:
            raise JobError(str(exc)) from exc
        return RunSpec(
            job_id=job_id,
            task=task,
            workspace=name,
            path=path,
            session=(req.session or "").strip()[:200],
            model=(req.model or "").strip(),
            effort=(req.effort or "").strip(),
            profile=narrow_profile(req.tools, self.settings.tool_profile),
            mcp_servers=with_depth(servers, req.depth + 1),
            images=list(req.images or []),
            context=req.context,
            instructions=req.instructions,
        )

    def submit(self, req: JobRequest) -> Job:
        job_id = uuid.uuid4().hex[:16]
        spec = self._build(req, job_id)
        job = Job(
            id=job_id,
            task=spec.task,
            workspace=spec.workspace,
            session=spec.session,
            model=spec.model or self.settings.model,
            profile=spec.profile,
            origin=req.origin,
        )
        self.jobs[job.id] = job
        self.store.save(job)
        self._prune()
        job.task_handle = asyncio.create_task(self._run(job, spec), name=f"job-{job.id}")
        job.task_handle.add_done_callback(lambda _t, job=job: self._settle(job))
        logger.info(
            "job %s queued workspace=%s session=%s tools=%s origin=%s",
            job.id, job.workspace, job.session or "-", job.profile, job.origin,
        )
        return job

    def _prune(self) -> None:
        keep = self.settings.keep_jobs
        if len(self.jobs) <= keep:
            return
        finished = sorted((j for j in self.jobs.values() if j.done), key=lambda j: j.created_at)
        for job in finished[: len(self.jobs) - keep]:
            self.jobs.pop(job.id, None)
        self.store.prune(keep)

    # ---- run ---------------------------------------------------------------------------------

    async def _emit(self, job: Job, event: dict[str, Any]) -> None:
        if len(job.events) >= _MAX_EVENTS:
            return
        entry = {"seq": len(job.events) + 1, "at": _now(), **event}
        job.events.append(entry)
        self.store.add_event(job.id, entry)
        job.changed.set()

    def _settle(self, job: Job) -> None:
        """A task cancelled before its first step never enters _run; close the job here."""
        if not job.done:
            self._finish(job, "cancelled", error="Cancelled before it started.")

    def _finish(self, job: Job, status: str, **fields: Any) -> None:
        if job.approval_future is not None and not job.approval_future.done():
            job.approval_future.set_result((False, "The job ended."))
        job.pending_approval = None
        job.status = status
        job.finished_at = _now()
        for key, value in fields.items():
            setattr(job, key, value)
        self.store.save(job)
        job.changed.set()
        logger.info("job %s %s", job.id, status)

    async def _run(self, job: Job, spec: RunSpec) -> None:
        locks: list[asyncio.Lock] = []
        if touches_files(spec.profile):
            locks.append(self._workspace_locks.setdefault(spec.workspace, asyncio.Lock()))
        if spec.session:
            locks.append(self._session_locks.setdefault((spec.workspace, spec.session), asyncio.Lock()))
        acquired: list[asyncio.Lock] = []
        try:
            async with self._slots:
                for lock in locks:
                    if lock.locked():
                        await self._emit(job, {"type": "status", "text": "Waiting for another job in this workspace..."})
                    await lock.acquire()
                    acquired.append(lock)
                job.status = "running"
                job.started_at = _now()
                self.store.save(job)
                await self._emit(job, {"type": "status", "text": "Running"})
                outcome = await asyncio.wait_for(
                    self.engine.run(spec, lambda event: self._emit(job, event)),
                    timeout=self.settings.job_timeout,
                )
            fields = dict(
                result=outcome.text,
                error=outcome.error,
                run_id=outcome.run_id,
                agent_id=outcome.agent_id,
                usage=outcome.usage,
                changes=outcome.changes,
                duration_ms=outcome.duration_ms,
            )
            if outcome.model:
                fields["model"] = outcome.model
            if outcome.status == "succeeded":
                await self._emit(job, {"type": "result", "text": outcome.text})
            else:
                await self._emit(job, {"type": "error", "message": outcome.error})
            self._finish(job, outcome.status, **fields)
        except asyncio.CancelledError:
            await self._emit(job, {"type": "status", "text": "Cancelled"})
            self._finish(job, "cancelled", error=job.error or "Cancelled.")
        except asyncio.TimeoutError:
            message = f"The job ran longer than JOB_TIMEOUT_SECONDS ({self.settings.job_timeout}s) and was stopped."
            await self._emit(job, {"type": "error", "message": message})
            self._finish(job, "failed", error=message)
        except EngineError as exc:
            await self._emit(job, {"type": "error", "message": str(exc)})
            self._finish(job, "failed", error=str(exc))
        except Exception as exc:
            logger.exception("job %s crashed", job.id)
            message = f"The worker failed: {type(exc).__name__}"
            await self._emit(job, {"type": "error", "message": message})
            self._finish(job, "failed", error=message)
        finally:
            for lock in acquired:
                lock.release()

    # ---- read / control ----------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self, limit: int = 50, workspace: str = "", status: str = "") -> list[Job]:
        rows = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        if workspace:
            rows = [j for j in rows if j.workspace == workspace]
        if status:
            rows = [j for j in rows if j.status == status]
        return rows[: max(1, min(limit, 500))]

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if not job.done and job.task_handle is not None:
            job.task_handle.cancel()
        return job

    async def wait(self, job: Job, timeout: float, *, stop_on_approval: bool = False) -> Job:
        """Wait until the job ends (or asks for approval) or the timeout passes, whichever is first."""

        def settled() -> bool:
            return job.done or (stop_on_approval and job.status == "needs_approval")

        if settled() or timeout <= 0:
            return job
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not settled():
            left = deadline - loop.time()
            if left <= 0:
                break
            job.changed.clear()
            if settled():
                break
            try:
                await asyncio.wait_for(job.changed.wait(), left)
            except asyncio.TimeoutError:
                break
        return job

    async def follow(self, job: Job, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Events with seq > after, then new ones as they come, until the job ends."""
        seen = max(0, after)
        while True:
            while seen < len(job.events):
                seen += 1
                yield job.events[seen - 1]
            if job.done:
                return
            job.changed.clear()
            if seen < len(job.events) or job.done:
                continue
            try:
                await asyncio.wait_for(job.changed.wait(), 15)
            except asyncio.TimeoutError:
                yield {"type": "ping"}

    # ---- remote calls and approvals --------------------------------------------------------

    def active_job(self, workspace: str) -> Job | None:
        """The job whose agent has a shell in this workspace. File jobs hold the workspace lock, so at most one."""
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True):
            if job.workspace == workspace and job.status in {"running", "needs_approval"} and touches_files(job.profile):
                return job
        return None

    async def remote_call(
        self, workspace: str, tool: str, alias: str, hostname: str, user: str, command: str
    ) -> tuple[bool, str]:
        """Record an ssh/scp/sftp call in its job; hold it for a human on approval hosts."""
        alias = (alias or hostname or "?").strip()
        hostname = (hostname or alias).strip()
        target = f"{user}@{alias}" if user else alias
        label = f"{tool} {target}" + (f": {command}" if command and tool == "ssh" else "")
        label = label if len(label) <= 300 else label[:300] + "…"
        needs = self.settings.needs_approval(alias, hostname)
        job = self.active_job(workspace)
        if job is None:
            logger.warning("remote call outside a job workspace=%r %s", workspace, label)
            if needs:
                return False, f"{alias} needs approval, and no running job was found to ask in."
            return True, ""
        await self._emit(job, {"type": "tool", "name": tool, "phase": "start", "text": label,
                               "host": alias, "hostname": hostname, "command": command})
        if not needs:
            return True, ""
        async with job.approval_lock:
            if alias.lower() in job.approved_hosts:
                return True, ""
            if job.done:
                return False, "The job ended."
            approval = {
                "id": uuid.uuid4().hex[:12],
                "tool": tool,
                "host": alias,
                "hostname": hostname,
                "user": user,
                "command": command,
                "requested_at": _now(),
            }
            job.approval_future = asyncio.get_running_loop().create_future()
            job.pending_approval = approval
            job.status = "needs_approval"
            self.store.save(job)
            await self._emit(job, {"type": "approval", **approval, "text": f"Approval needed: {label}"})
            logger.info("job %s waits for approval: %s", job.id, label)
            try:
                allow, note = await asyncio.wait_for(
                    asyncio.shield(job.approval_future), self.settings.ssh_approval_timeout
                )
            except asyncio.TimeoutError:
                allow, note = False, f"No answer within {self.settings.ssh_approval_timeout} s."
            finally:
                job.approval_future = None
                job.pending_approval = None
                if not job.done:
                    job.status = "running"
                    self.store.save(job)
            if allow:
                job.approved_hosts.add(alias.lower())
                await self._emit(job, {"type": "status", "text": f"Approved: {alias}" + (f" ({note})" if note else "")})
                return True, ""
            await self._emit(job, {"type": "status", "text": f"Denied: {alias}" + (f" ({note})" if note else "")})
            return False, (
                f"The user did not approve {tool} to {alias}"
                + (f": {note}" if note else ".")
                + " Do not retry or work around this; report it in your answer."
            )

    def decide(self, job_id: str, allow: bool, note: str = "") -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("Job not found.", status=404)
        future = job.approval_future
        if job.status != "needs_approval" or future is None or future.done():
            raise JobError("This job is not waiting for an approval.", status=409)
        future.set_result((bool(allow), (note or "").strip()[:300]))
        # Switch now, not when the waiting call wakes up, so a caller that reads the job next sees it moving.
        job.pending_approval = None
        job.status = "running"
        self.store.save(job)
        job.changed.set()
        return job

    def pending_for(self, workspace: str, session: str, origin: str) -> Job | None:
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True):
            if job.status == "needs_approval" and job.workspace == workspace and job.origin == origin:
                if not session or job.session == session:
                    return job
        return None

    async def aclose(self) -> None:
        running = [j.task_handle for j in self.jobs.values() if j.task_handle is not None and not j.done]
        for handle in running:
            handle.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
