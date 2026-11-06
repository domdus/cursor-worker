from __future__ import annotations

from dataclasses import replace

import pytest

from cursor_worker.mcp_config import (
    DEPTH_HEADER,
    McpConfigError,
    load_mcp_servers,
    rewrite_loopback_url,
    servers_from_request,
    with_depth,
)
from cursor_worker.settings import narrow_profile, profile_tools, touches_files
from cursor_worker.workspaces import WorkspaceError, list_workspaces, resolve


def test_request_can_only_narrow_the_tool_profile():
    assert narrow_profile("full", "offline") == "offline"
    assert narrow_profile("mcp", "offline") == "mcp"
    assert narrow_profile("", "local") == "local"
    assert narrow_profile("bogus", "full") == "full"
    assert narrow_profile("none", "full") == "none"


def test_profile_tools():
    assert profile_tools("full") == (None, None)
    assert profile_tools("local") == (None, ["webSearch", "webFetch"])
    assert profile_tools("offline")[1] == ["webSearch", "webFetch", "shell"]
    assert profile_tools("mcp") == (["mcp"], None)
    assert profile_tools("none") == ([], None)
    assert touches_files("offline") and not touches_files("mcp")


def test_request_servers_are_http_only():
    with pytest.raises(McpConfigError):
        servers_from_request({"evil": {"command": "rm", "args": ["-rf", "/"]}})
    with pytest.raises(McpConfigError):
        servers_from_request({"x": {"url": "file:///etc/passwd"}})
    out = servers_from_request({"mcpServers": {"gh": {"url": "https://example.com/mcp", "headers": {"A": "b"}}}})
    assert out == {"gh": {"type": "http", "url": "https://example.com/mcp", "headers": {"A": "b"}}}


def test_file_servers_allow_stdio_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    path = tmp_path / "mcp.json"
    path.write_text(
        '{"mcpServers": {"fs": {"command": "npx", "args": ["x"]},'
        ' "gh": {"url": "https://api/mcp", "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}}}'
    )
    servers = load_mcp_servers(path)
    assert servers["fs"]["type"] == "stdio"
    assert servers["gh"]["headers"]["Authorization"] == "Bearer secret"


def test_depth_header_is_stamped_on_http_servers_only():
    out = with_depth({"a": {"type": "http", "url": "http://x"}, "b": {"type": "stdio", "command": "c"}}, 1)
    assert out["a"]["headers"][DEPTH_HEADER] == "1"
    assert "headers" not in out["b"]


def test_loopback_rewrite():
    assert rewrite_loopback_url("http://127.0.0.1:8123/mcp", "host.docker.internal") == "http://host.docker.internal:8123/mcp"
    assert rewrite_loopback_url("http://example.com/mcp", "host.docker.internal") == "http://example.com/mcp"


def test_workspace_names(settings):
    assert resolve(settings, "")[0] == "default"
    name, path = resolve(settings, "My-Repo")
    assert name == "my-repo" and path.is_dir()
    for bad in ("../etc", "a/b", ".hidden", "x" * 80):
        with pytest.raises(WorkspaceError):
            resolve(settings, bad)


def test_workspace_creation_can_be_off(settings):
    closed = replace(settings, create_workspaces=False)
    with pytest.raises(WorkspaceError):
        resolve(closed, "new-one")
    assert resolve(closed, "")[0] == "default"


def test_default_workspace_dir_override(settings, tmp_path):
    bound = tmp_path / "host-project"
    custom = replace(settings, default_workspace_dir=bound)
    assert resolve(custom, "")[1] == bound
    names = {row["name"] for row in list_workspaces(custom)}
    assert "default" in names
