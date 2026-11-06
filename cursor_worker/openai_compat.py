"""OpenAI-compatible chat endpoint, so Open WebUI (or any OpenAI client) can pick Cursor as a model.

Each request becomes a job. The newest user message is the task; earlier turns are pasted as context
only when the agent is new. Agent steps stream as reasoning_content so Open WebUI shows them as thinking.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from cursor_worker.images import parse_image
from cursor_worker.jobs import Job, JobError, JobManager, JobRequest
from cursor_worker.mcp_config import DEPTH_HEADER, parse_depth

router = APIRouter(prefix="/v1")

_CONTEXT_CHARS = 60_000
_SESSION_HEADERS = ("x-openwebui-chat-id", "x-session-id", "x-cursor-session")


def split_model(raw: str) -> tuple[str, str]:
    """'composer-2.5@my-repo' -> ('composer-2.5', 'my-repo'). A bare name uses the default workspace."""
    name = str(raw or "").strip()
    if name.startswith("cursor/"):
        name = name[len("cursor/") :]
    model, _, workspace = name.partition("@")
    if model in {"", "cursor", "cursor-worker"}:
        model = ""
    return model, workspace


def _text_and_images(content: Any) -> tuple[str, list[dict[str, str]]]:
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[dict[str, str]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            parsed = parse_image(str(url or "")) if str(url or "").startswith("data:") else None
            if parsed:
                images.append(parsed)
    return "\n".join(t for t in texts if t), images


def messages_to_request(messages: list[dict[str, Any]]) -> tuple[str, str, str, list[dict[str, str]]]:
    """(task, context, instructions, images) from an OpenAI messages list."""
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    images: list[dict[str, str]] = []
    last_user = -1
    for item in messages:
        role = str(item.get("role") or "")
        text, found = _text_and_images(item.get("content"))
        if role in {"system", "developer"}:
            if text.strip():
                system.append(text.strip())
            continue
        if role not in {"user", "assistant"}:
            continue
        turns.append((role, text))
        if role == "user":
            last_user = len(turns) - 1
            images = found
    if last_user < 0:
        raise HTTPException(status_code=400, detail="messages needs at least one user message.")
    task = turns[last_user][1].strip()
    earlier = turns[:last_user]
    lines = [f"{'User' if role == 'user' else 'Assistant'}: {text.strip()}" for role, text in earlier if text.strip()]
    context = "\n\n".join(lines)
    if len(context) > _CONTEXT_CHARS:
        context = "…" + context[-_CONTEXT_CHARS:]
    return task, context, "\n\n".join(system), images


def _chunk(cid: str, model: str, delta: dict[str, Any], finish: str | None = None) -> str:
    body = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _usage(job: Job) -> dict[str, int]:
    usage = job.usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _answer(job: Job) -> str:
    if job.status == "succeeded":
        return job.result
    if job.status == "cancelled":
        return job.result or "(cancelled)"
    detail = job.error or "The agent failed."
    return f"{job.result}\n\n**Cursor worker error:** {detail}".strip()


@router.get("/models")
async def models(request: Request) -> dict[str, Any]:
    manager: JobManager = request.app.state.jobs
    rows = await manager.engine.models()
    default = manager.settings.model
    if default and not any(row["id"] == default for row in rows):
        rows = [{"id": default, "name": default}, *rows]
    return {
        "object": "list",
        "data": [{"id": row["id"], "object": "model", "created": 0, "owned_by": "cursor"} for row in rows],
    }


@router.post("/chat/completions")
async def chat_completions(request: Request):
    manager: JobManager = request.app.state.jobs
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body is not JSON.") from None
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages is required.")
    model, workspace = split_model(str(body.get("model") or ""))
    workspace = request.headers.get("x-cursor-workspace", "").strip() or workspace
    session = next((request.headers.get(h, "").strip() for h in _SESSION_HEADERS if request.headers.get(h)), "")
    task, context, instructions, images = messages_to_request(body["messages"])
    try:
        job = manager.submit(
            JobRequest(
                task=task,
                workspace=workspace,
                session=session,
                model=model,
                effort=str(body.get("reasoning_effort") or ""),
                tools=request.headers.get("x-cursor-tools", "").strip(),
                images=images,
                context=context,
                instructions=instructions,
                depth=parse_depth(request.headers.get(DEPTH_HEADER)),
                origin="openai",
            )
        )
    except JobError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    cid = f"chatcmpl-{job.id}"
    shown = str(body.get("model") or job.model)

    if not body.get("stream"):
        await manager.wait(job, manager.settings.job_timeout + 30)
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": shown,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": _answer(job)}, "finish_reason": "stop"}
                ],
                "usage": _usage(job),
                "cursor_worker": {"job_id": job.id, "status": job.status, "changes": job.changes},
            }
        )

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    async def stream():
        streamed_text = False
        finished = False
        try:
            yield _chunk(cid, shown, {"role": "assistant", "content": ""})
            async for event in manager.follow(job):
                if await request.is_disconnected():
                    break
                kind = event.get("type")
                if kind == "text":
                    streamed_text = True
                    yield _chunk(cid, shown, {"content": str(event.get("delta") or "")})
                elif kind in {"tool", "status"}:
                    line = str(event.get("text") or "").strip()
                    if line:
                        yield _chunk(cid, shown, {"reasoning_content": line + "\n"})
                elif kind == "ping":
                    yield ": ping\n\n"
            if job.done:
                if not streamed_text and job.status == "succeeded" and job.result:
                    yield _chunk(cid, shown, {"content": job.result})
                elif job.status != "succeeded":
                    yield _chunk(cid, shown, {"content": "\n\n" + _answer(job)})
                yield _chunk(cid, shown, {}, "stop")
                if include_usage:
                    usage = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": shown, "choices": [], "usage": _usage(job)}
                    yield f"data: {json.dumps(usage)}\n\n"
                yield "data: [DONE]\n\n"
                finished = True
        finally:
            # The user pressed stop in the chat (or the client went away): stop the agent too.
            if not finished and not job.done:
                manager.cancel(job.id)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
