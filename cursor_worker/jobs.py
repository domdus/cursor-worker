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
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task_handle: asyncio.Task | None = field(default=None, repr=False)

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

    async def wait(self, job: Job, timeout: float) -> Job:
        """Wait until the job ends or the timeout passes, whichever is first."""
        if job.done or timeout <= 0:
            return job
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not job.done:
            left = deadline - loop.time()
            if left <= 0:
                break
            job.changed.clear()
            if job.done:
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

    async def aclose(self) -> None:
        running = [j.task_handle for j in self.jobs.values() if j.task_handle is not None and not j.done]
        for handle in running:
            handle.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
