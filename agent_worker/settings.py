"""Worker configuration. Everything comes from the container environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Tool profiles, most permissive first. A request can only pick the same or a narrower one
# than TOOL_PROFILE on the container.
TOOL_PROFILES = ("full", "local", "offline", "mcp", "none")
# Agent engines a job can run on.
ENGINES = ("cursor", "claude")
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _patterns(raw: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in raw.replace("\n", ",").split(",") if item.strip())


def _engine(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in ENGINES else ""


def _profile(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in TOOL_PROFILES else "full"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workspaces_dir: Path
    default_workspace: str
    default_workspace_dir: Path | None
    state_dir: Path
    mcp_config_path: Path
    cursor_api_key: str
    model: str
    host: str
    port: int
    token: str = ""
    tool_profile: str = "full"
    allow_request_mcp: bool = True
    create_workspaces: bool = True
    max_concurrent_jobs: int = 2
    job_timeout: int = 1800
    mcp_wait_seconds: int = 20
    max_call_depth: int = 1
    keep_jobs: int = 200
    instructions: str = ""
    # Hosts (fnmatch patterns on the ssh alias or its HostName) where a job pauses for a human first.
    ssh_approval_hosts: tuple[str, ...] = ()
    ssh_approval_timeout: int = 900
    # Let MCP callers approve with the agent_approve tool. Off: the calling model could approve itself.
    approval_tool: bool = False
    # Claude Code (claude-agent-sdk): a Claude Plan token (sk-ant-oat…, `claude setup-token`) or an
    # Anthropic API key. Without either, a token from "Sign in with Claude" in DATA_DIR is used.
    claude_oauth_token: str = ""
    anthropic_api_key: str = ""
    claude_model: str = DEFAULT_CLAUDE_MODEL
    # Engine for jobs that do not name one. Empty: cursor, or claude when only Claude is set up.
    default_engine: str = ""

    @property
    def claude_token_path(self) -> Path:
        """Where "Sign in with Claude" keeps the Plan token (owner-only file in the data volume)."""
        return self.data_dir / "claude" / "oauth-token"

    @property
    def claude_config_dir(self) -> Path:
        """Claude Code's own state (sessions for resume). Never the user's ~/.claude."""
        return self.data_dir / "claude" / "config"

    def needs_approval(self, *names: str) -> bool:
        from fnmatch import fnmatchcase

        for name in names:
            value = (name or "").strip().lower()
            if value and any(fnmatchcase(value, pattern) for pattern in self.ssh_approval_hosts):
                return True
        return False

    def open_without_token(self) -> bool:
        """Only a worker bound to loopback may answer without WORKER_TOKEN."""
        return self.host in LOOPBACK_HOSTS

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        workspaces_dir = Path(os.environ.get("WORKSPACES_DIR", str(data_dir / "workspaces"))).resolve()
        # WORKSPACE_DIR binds the default workspace to one host folder, like the HA runtime did.
        single = os.environ.get("WORKSPACE_DIR", "").strip()
        configured = os.environ.get("MCP_CONFIG_PATH", "").strip()
        return cls(
            data_dir=data_dir,
            workspaces_dir=workspaces_dir,
            default_workspace=(os.environ.get("DEFAULT_WORKSPACE", "").strip() or "default").lower(),
            default_workspace_dir=Path(single).resolve() if single else None,
            state_dir=Path(os.environ.get("CURSOR_STATE_DIR", str(data_dir / "cursor-state"))).resolve(),
            mcp_config_path=Path(configured).resolve() if configured else data_dir / "mcp.json",
            cursor_api_key=os.environ.get("CURSOR_API_KEY", "").strip(),
            model=os.environ.get("CURSOR_MODEL", "").strip() or "composer-2.5",
            host=os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_int("PORT", 8787, 1),
            token=os.environ.get("WORKER_TOKEN", "").strip(),
            tool_profile=_profile(os.environ.get("TOOL_PROFILE", "full")),
            allow_request_mcp=_bool("ALLOW_REQUEST_MCP", True),
            create_workspaces=_bool("CREATE_WORKSPACES", True),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2, 1),
            job_timeout=_int("JOB_TIMEOUT_SECONDS", 1800, 30),
            mcp_wait_seconds=_int("MCP_WAIT_SECONDS", 20, 0),
            max_call_depth=_int("MAX_CALL_DEPTH", 1, 1),
            keep_jobs=_int("KEEP_JOBS", 200, 10),
            instructions=os.environ.get("WORKER_INSTRUCTIONS", "").strip(),
            ssh_approval_hosts=_patterns(os.environ.get("SSH_APPROVAL_HOSTS", "")),
            ssh_approval_timeout=_int("SSH_APPROVAL_TIMEOUT", 900, 10),
            approval_tool=_bool("APPROVAL_TOOL", False),
            claude_oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            claude_model=os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_CLAUDE_MODEL,
            default_engine=_engine(os.environ.get("DEFAULT_ENGINE", "")),
        )


def narrow_profile(requested: str | None, ceiling: str) -> str:
    """The narrower of the requested profile and the container's ceiling. Unknown values keep the ceiling."""
    want = (requested or "").strip().lower()
    if want not in TOOL_PROFILES:
        return ceiling
    return want if TOOL_PROFILES.index(want) >= TOOL_PROFILES.index(ceiling) else ceiling


def profile_tools(profile: str) -> tuple[list[str] | None, list[str] | None]:
    """(tools, disallowed_tools) for AgentOptions."""
    if profile == "none":
        return [], None
    if profile == "mcp":
        return ["mcp"], None
    if profile == "offline":
        # Shell goes too: curl or python would reach the web anyway.
        return None, ["webSearch", "webFetch", "shell"]
    if profile == "local":
        return None, ["webSearch", "webFetch"]
    return None, None


def touches_files(profile: str) -> bool:
    return profile in {"full", "local", "offline"}
