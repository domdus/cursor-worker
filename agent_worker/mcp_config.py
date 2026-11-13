"""MCP servers the Cursor agent gets: mcp.json on the container plus http/sse servers sent with a job."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# Sent to every HTTP MCP server the agent uses. When the agent calls this worker (or another
# worker) back through MCP, the receiver sees the depth and refuses to start a nested job.
DEPTH_HEADER = "X-Agent-Worker-Depth"


class McpConfigError(ValueError):
    pass


def rewrite_loopback_url(url: str, host: str | None = None) -> str:
    """Map 127.0.0.1 MCP URLs to the Docker host when the worker is not on the host network."""
    target = (host if host is not None else os.environ.get("REWRITE_LOOPBACK_HOST") or "").strip()
    if not target or not url:
        return url
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in _LOOPBACK:
        return url
    user = parsed.username or ""
    if parsed.password:
        user = f"{user}:{parsed.password}"
    auth = f"{user}@" if user else ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse(parsed._replace(netloc=f"{auth}{target}{port}"))


def _subst(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise McpConfigError(f"MCP config references unset environment variable ${{{name}}}")
        return os.environ[name]

    return _ENV.sub(replace, value)


def _stringify(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise McpConfigError(f"{field} must be a string")
    return _subst(value)


def server_to_sdk(name: str, raw: Any) -> dict[str, Any]:
    """A server from mcp.json. Trusted: may use ${ENV} and stdio commands."""
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP server {name} must be an object")
    url = raw.get("url")
    command = raw.get("command")
    kind = str(raw.get("type") or "").strip().lower()
    if url or kind in {"http", "sse", "streamable-http", "streamable_http"}:
        if not url:
            raise McpConfigError(f"MCP server {name} has type {kind} but no url")
        server: dict[str, Any] = {
            "type": "sse" if kind == "sse" else "http",
            "url": rewrite_loopback_url(_stringify(url, f"{name}.url")),
        }
        headers = raw.get("headers")
        if headers is not None:
            if not isinstance(headers, dict):
                raise McpConfigError(f"{name}.headers must be an object")
            server["headers"] = {str(key): _stringify(val, f"{name}.headers.{key}") for key, val in headers.items()}
        return server
    if command:
        server = {"type": "stdio", "command": _stringify(command, f"{name}.command")}
        args = raw.get("args") or []
        if not isinstance(args, list):
            raise McpConfigError(f"{name}.args must be a list")
        server["args"] = [_stringify(item, f"{name}.args") for item in args]
        env = raw.get("env")
        if env is not None:
            if not isinstance(env, dict):
                raise McpConfigError(f"{name}.env must be an object")
            server["env"] = {str(key): _stringify(val, f"{name}.env.{key}") for key, val in env.items()}
        if raw.get("cwd"):
            server["cwd"] = _stringify(raw["cwd"], f"{name}.cwd")
        return server
    raise McpConfigError(f"MCP server {name} needs a url or a command")


def server_from_request(name: str, raw: Any) -> dict[str, Any]:
    """A server sent by a caller. HTTP or SSE only: a request never starts a process or reads the environment."""
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP server {name} must be an object")
    if raw.get("command") or str(raw.get("type") or "").strip().lower() == "stdio":
        raise McpConfigError(f"MCP server {name}: only http and sse servers can be sent with a job")
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise McpConfigError(f"MCP server {name} needs an http(s) url")
    kind = str(raw.get("type") or "").strip().lower()
    server: dict[str, Any] = {"type": "sse" if kind == "sse" else "http", "url": rewrite_loopback_url(url)}
    headers = raw.get("headers")
    if headers is not None:
        if not isinstance(headers, dict) or not all(isinstance(val, str) for val in headers.values()):
            raise McpConfigError(f"{name}.headers must be an object of strings")
        server["headers"] = {str(key): val for key, val in headers.items()}
    return server


def servers_from_request(raw: Any) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise McpConfigError("mcp_servers must be an object of name -> {url, type?, headers?}")
    block = raw.get("mcpServers", raw)
    if not isinstance(block, dict):
        raise McpConfigError("mcpServers must be an object")
    return {str(name): server_from_request(str(name), cfg) for name, cfg in block.items()}


def load_mcp_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"MCP config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise McpConfigError("MCP config must be a JSON object")
    servers = data.get("mcpServers", data)
    if not isinstance(servers, dict):
        raise McpConfigError("mcpServers must be an object")
    return {str(name): server_to_sdk(str(name), cfg) for name, cfg in servers.items()}


def with_depth(servers: dict[str, dict[str, Any]], depth: int) -> dict[str, dict[str, Any]]:
    """Stamp the call depth on every HTTP/SSE server so a call back into a worker is recognised."""
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        item = dict(cfg)
        if item.get("type") in {"http", "sse"}:
            headers = dict(item.get("headers") or {})
            headers[DEPTH_HEADER] = str(depth)
            item["headers"] = headers
        out[name] = item
    return out


def parse_depth(value: str | None) -> int:
    try:
        return max(0, int(str(value or "0").strip() or "0"))
    except ValueError:
        return 0
