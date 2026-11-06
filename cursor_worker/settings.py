"""Worker configuration. Everything comes from the container environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Tool profiles, most permissive first. A request can only pick the same or a narrower one
# than TOOL_PROFILE on the container.
TOOL_PROFILES = ("full", "local", "offline", "mcp", "none")


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
