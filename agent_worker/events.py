from __future__ import annotations

import json
from typing import Any

_WRAPPERS = {"mcp", "callmcptool", "call_mcp_tool", "call-mcp-tool"}


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _mapping(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        raw = obj.strip()
        if raw.startswith("{") and raw.endswith("}"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}
    data = getattr(obj, "__dict__", None)
    return dict(data) if isinstance(data, dict) else {}


def _nested_tool_name(payload: Any, depth: int = 0, *, allow_name: bool = True) -> str:
    if depth > 4:
        return ""
    data = _mapping(payload)
    if not data:
        return ""
    for key in ("toolName", "tool_name", "mcpToolName", "mcp_tool_name", "tool"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in _WRAPPERS:
            return raw.strip()
    if allow_name:
        raw = data.get("name")
        if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in _WRAPPERS:
            return raw.strip()
    for key in ("arguments", "input", "params", "args"):
        found = _nested_tool_name(data.get(key), depth + 1, allow_name=False)
        if found:
            return found
    return ""


def _resolved_tool_name(name: str, *payloads: Any) -> str:
    for payload in payloads:
        found = _nested_tool_name(payload)
        if found:
            return found
    return str(name or "tool").strip() or "tool"


def _tool_label(name: str) -> str:
    raw = str(name or "tool").strip() or "tool"
    if raw.lower() == "mcp":
        return "MCP"
    return raw


def _call_id(message: Any, block: Any = None) -> str:
    if block is not None:
        value = _get(block, "id")
        if value:
            return str(value)
    return str(_get(message, "call_id") or _get(message, "callId") or "")


def _tool_event(name: str, phase: str, text: str, call_id: str = "") -> dict[str, str]:
    event = {"type": "tool", "name": name, "phase": phase, "text": text}
    if call_id:
        event["id"] = call_id
    return event


def map_sdk_message(message: Any) -> list[dict[str, str]]:
    kind = _get(message, "type")
    if kind == "assistant":
        content = _get(_get(message, "message"), "content") or []
        events: list[dict[str, str]] = []
        for block in content:
            block_type = _get(block, "type")
            if block_type == "text":
                text = _get(block, "text") or ""
                if text:
                    events.append({"type": "text", "delta": str(text)})
            elif block_type == "tool_use":
                name = _resolved_tool_name(
                    str(_get(block, "name") or "tool"),
                    _get(block, "input"),
                )
                label = _tool_label(name)
                events.append(
                    _tool_event(name, "start", f"Calling {label}...", _call_id(message, block))
                )
        return events
    if kind == "thinking":
        return [{"type": "status", "text": "Thinking..."}]
    if kind == "tool_call":
        name = _resolved_tool_name(
            str(_get(message, "name") or "tool"),
            _get(message, "args"),
            _get(message, "input"),
        )
        label = _tool_label(name)
        status = str(_get(message, "status") or "")
        call_id = _call_id(message)
        if status == "completed":
            return [_tool_event(name, "end", f"Finished {label}", call_id)]
        if status == "error":
            return [_tool_event(name, "end", f"{label} failed", call_id)]
        return [_tool_event(name, "start", f"Calling {label}...", call_id)]
    if kind == "status":
        text = str(_get(message, "message") or _get(message, "status") or "Working...")
        if text.upper() == "ERROR":
            detail = str(_get(message, "error") or _get(message, "detail") or "").strip()
            return [{"type": "error", "message": detail or "Run failed."}]
        return [{"type": "status", "text": text[:160]}]
    if kind in {"error", "ERROR"}:
        text = str(_get(message, "message") or _get(message, "error") or "Run failed.")
        return [{"type": "error", "message": text[:400]}]
    return []
