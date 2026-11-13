"""Clone GIT_REPO into the default workspace when the worker starts.

Credentials stay on the container (GIT_SSH_KEY path or GIT_TOKEN). Callers never
send them. A workspace that already has .git is not cloned again.

On every start the checkout is also set up so the agent's own git commands
(pull, push) authenticate: the repository config points git at the token or key
through the container's environment, without writing the secret into .git/config.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

_SEED = frozenset({"AGENTS.md", ".cursor"})


class GitBootstrapError(RuntimeError):
    pass


def bootstrap_git(workspace: Path) -> None:
    repo = os.environ.get("GIT_REPO", "").strip()
    if not repo:
        return
    git = shutil.which("git")
    if not git:
        raise GitBootstrapError("GIT_REPO is set but git is not installed in this image.")
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / ".git").is_dir():
        logger.info("git workspace already has a checkout, skip clone")
        _configure_identity(git, workspace, os.environ.copy())
        _configure_credentials(git, workspace, repo)
        return
    if not _may_clone(workspace):
        logger.info("git skip clone: workspace already has files")
        return
    env = _git_env(repo)
    _clear_seed(workspace)
    dest = str(workspace)
    cmd = [git, "clone"]
    branch = os.environ.get("GIT_BRANCH", "").strip()
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([repo, dest])
    logger.info("git clone %s -> %s", _redact_url(repo), dest)
    _run(cmd, env=env)
    _configure_identity(git, workspace, env)
    _configure_credentials(git, workspace, repo)


# Reads the token from the environment each time git asks, so .git/config never holds it.
_TOKEN_HELPER = (
    '!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
)


def _configure_credentials(git: str, workspace: Path, repo: str) -> None:
    """Let `git pull` and `git push` in the agent's shell use the same token or key as the clone."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    parsed = urlparse(repo)
    if os.environ.get("GIT_TOKEN", "").strip() and parsed.scheme in {"http", "https"} and parsed.hostname:
        origin = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
        _run([git, "-C", str(workspace), "config", "--replace-all", f"credential.{origin}.helper", _TOKEN_HELPER], env=env)
    key = _prepare_ssh_key()
    if key is not None:
        known = Path(tempfile.gettempdir()) / "agent-worker-known-hosts"
        command = f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known}"
        _run([git, "-C", str(workspace), "config", "core.sshCommand", command], env=env)


def _may_clone(workspace: Path) -> bool:
    try:
        names = {item.name for item in workspace.iterdir()}
    except FileNotFoundError:
        return True
    return not names or names <= _SEED


def _clear_seed(workspace: Path) -> None:
    agents = workspace / "AGENTS.md"
    if agents.is_file():
        agents.unlink()
    cursor = workspace / ".cursor"
    if cursor.is_dir():
        shutil.rmtree(cursor)


def _git_env(repo: str = "") -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in {"GIT_SSH_KEY", "GIT_TOKEN"}}
    env["GIT_TERMINAL_PROMPT"] = "0"
    header = _token_header(repo)
    if header:
        # Passed as git config through the environment so the token never shows up in the process list.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"] = header
    key = _prepare_ssh_key()
    if key is not None:
        known = Path(tempfile.gettempdir()) / "agent-worker-known-hosts"
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known}"
        )
    return env


def _prepare_ssh_key() -> Path | None:
    raw = os.environ.get("GIT_SSH_KEY", "").strip()
    if not raw:
        return None
    dest = Path(tempfile.mkdtemp(prefix="agent-worker-ssh-")) / "id"
    if raw.startswith("-----BEGIN"):
        text = raw if raw.endswith("\n") else raw + "\n"
        dest.write_text(text, encoding="utf-8")
    else:
        src = Path(raw)
        if not src.is_file():
            raise GitBootstrapError("GIT_SSH_KEY is not a readable key file.")
        dest.write_bytes(src.read_bytes())
    dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return dest


def _token_header(repo: str) -> tuple[str, str] | None:
    token = os.environ.get("GIT_TOKEN", "").strip()
    if not token:
        return None
    parsed = urlparse(repo)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    origin = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"http.{origin}/.extraHeader", f"Authorization: Basic {basic}"


def _configure_identity(git: str, workspace: Path, env: dict[str, str]) -> None:
    name = os.environ.get("GIT_USER_NAME", "").strip()
    email = os.environ.get("GIT_USER_EMAIL", "").strip()
    if name:
        _run([git, "-C", str(workspace), "config", "user.name", name], env=env)
    if email:
        _run([git, "-C", str(workspace), "config", "user.email", email], env=env)


def _redact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunparse(parsed._replace(netloc=netloc))


def _run(cmd: list[str], *, env: dict[str, str]) -> None:
    try:
        subprocess.run(
            cmd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()[:400]
        raise GitBootstrapError(f"git failed: {detail}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitBootstrapError(f"git failed: {exc}") from exc
