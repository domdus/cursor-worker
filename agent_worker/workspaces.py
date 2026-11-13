"""Named workspaces: one folder each under WORKSPACES_DIR."""

from __future__ import annotations

import re
from pathlib import Path

from agent_worker.settings import Settings

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class WorkspaceError(ValueError):
    pass


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name or "")) and ".." not in name


def resolve(settings: Settings, name: str | None, *, create: bool | None = None) -> tuple[str, Path]:
    """Map a workspace name to its folder. Empty means the default workspace."""
    chosen = (name or "").strip().lower() or settings.default_workspace
    if not valid_name(chosen):
        raise WorkspaceError(
            f"Workspace name {chosen!r} is not allowed. Use lowercase letters, digits, '.', '_' or '-'."
        )
    if chosen == settings.default_workspace and settings.default_workspace_dir is not None:
        path = settings.default_workspace_dir
    else:
        path = (settings.workspaces_dir / chosen).resolve()
        if path.parent != settings.workspaces_dir.resolve():
            raise WorkspaceError("Workspace path escapes WORKSPACES_DIR.")
    may_create = settings.create_workspaces if create is None else create
    if not path.is_dir():
        if chosen != settings.default_workspace and not may_create:
            raise WorkspaceError(f"Workspace {chosen!r} does not exist and CREATE_WORKSPACES is off.")
        path.mkdir(parents=True, exist_ok=True)
    return chosen, path


def list_workspaces(settings: Settings) -> list[dict]:
    rows: dict[str, Path] = {}
    if settings.workspaces_dir.is_dir():
        for item in sorted(settings.workspaces_dir.iterdir()):
            if item.is_dir() and valid_name(item.name):
                rows[item.name] = item
    default_path = settings.default_workspace_dir or settings.workspaces_dir / settings.default_workspace
    rows[settings.default_workspace] = default_path
    return [
        {
            "name": name,
            "default": name == settings.default_workspace,
            "git": (path / ".git").is_dir(),
            "exists": path.is_dir(),
        }
        for name, path in sorted(rows.items())
    ]
