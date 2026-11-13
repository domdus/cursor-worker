"""Build ~/.ssh from environment variables and put the ssh/scp/sftp guard first on PATH.

  SSH_KEY           one private key: the key text, or a path to a mounted file
  SSH_KEYS_DIR      a mounted folder of private keys; each is copied to ~/.ssh/keys/<file name>
  SSH_CONFIG        an ssh config (text or path): Host aliases, users, ports, IdentityFile ...
  SSH_KNOWN_HOSTS   known_hosts lines (text or path); pins the servers' host keys
  SSH_STRICT_HOST_KEYS  yes | accept-new  (default: yes with SSH_KNOWN_HOSTS, else accept-new)

GIT_SSH_KEY is added as one more identity, so git over SSH works in every workspace.
Nothing is written when ~/.ssh is a read-only mount: then that mount is used as it is.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from agent_worker.settings import Settings

logger = logging.getLogger(__name__)

BIN_DIR = Path(__file__).resolve().parent / "bin"
_SKIP = {"config", "known_hosts", "authorized_keys"}


@dataclass
class SshInfo:
    configured: bool = False
    identities: list[str] = field(default_factory=list)
    strict: str = ""
    hosts: list[str] = field(default_factory=list)


def _text_or_file(raw: str) -> str:
    """A value that is either the content itself or a path to a file holding it."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "\n" not in value and not value.startswith("-----BEGIN"):
        path = Path(value).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8")
    # An env_file cannot hold real newlines; accept "\n" written out.
    if "\n" not in value and "\\n" in value:
        value = value.replace("\\n", "\n")
    return value


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _hosts_in(config: str) -> list[str]:
    hosts: list[str] = []
    for line in config.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            hosts.extend(p for p in parts[1:] if "*" not in p and "?" not in p and not p.startswith("!"))
    return hosts


def setup_ssh(home: Path | None = None, env: dict[str, str] | None = None) -> SshInfo:
    env = dict(os.environ if env is None else env)
    home = home or Path(env.get("HOME") or "~").expanduser()
    ssh_dir = home / ".ssh"
    key = _text_or_file(env.get("SSH_KEY", ""))
    git_key = _text_or_file(env.get("GIT_SSH_KEY", ""))
    keys_dir = Path(env["SSH_KEYS_DIR"]).expanduser() if env.get("SSH_KEYS_DIR", "").strip() else None
    user_config = _text_or_file(env.get("SSH_CONFIG", ""))
    known = _text_or_file(env.get("SSH_KNOWN_HOSTS", ""))
    if not any((key, git_key, keys_dir, user_config, known)):
        return SshInfo()
    for name, value in (("SSH_KEY", key), ("GIT_SSH_KEY", git_key)):
        if value and "PRIVATE KEY" not in value:
            logger.error("%s is neither a private key nor a readable key file; it is ignored", name)
    key = key if "PRIVATE KEY" in key else ""
    git_key = git_key if "PRIVATE KEY" in git_key else ""
    try:
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe = ssh_dir / ".agent-worker-probe"
        probe.write_text("")
        probe.unlink()
    except OSError:
        logger.warning("~/.ssh is not writable (a read-only mount?); using it as it is and ignoring SSH_* variables")
        return SshInfo(configured=True)
    ssh_dir.chmod(0o700)
    (ssh_dir / "cm").mkdir(mode=0o700, exist_ok=True)
    identities: list[Path] = []
    if key:
        identities.append(ssh_dir / "keys" / "id_worker")
        _write_private(identities[-1], key)
    if git_key and git_key != key:
        identities.append(ssh_dir / "keys" / "id_git")
        _write_private(identities[-1], git_key)
    if keys_dir is not None:
        if not keys_dir.is_dir():
            logger.error("SSH_KEYS_DIR %s is not a folder", keys_dir)
        else:
            for item in sorted(keys_dir.iterdir()):
                if item.is_file() and not item.name.endswith(".pub") and item.name not in _SKIP:
                    target = ssh_dir / "keys" / item.name
                    _write_private(target, item.read_text(encoding="utf-8"))
                    identities.append(target)
    strict = (env.get("SSH_STRICT_HOST_KEYS", "").strip().lower() or ("yes" if known else "accept-new"))
    if strict not in {"yes", "accept-new"}:
        strict = "yes"
    if known:
        _write_private(ssh_dir / "known_hosts", known)
    if user_config:
        _write_private(ssh_dir / "config.d" / "user", user_config)
    # ssh keeps the first value it finds, so the user's config comes first and these are fallbacks.
    lines = ["# Written by agent-worker at start. Put your own settings in SSH_CONFIG.", ""]
    if user_config:
        lines.append(f"Include {ssh_dir / 'config.d' / 'user'}")
        lines.append("")
    lines += [
        "Host *",
        *[f"  IdentityFile {path}" for path in identities],
        f"  StrictHostKeyChecking {strict}",
        f"  UserKnownHostsFile {ssh_dir / 'known_hosts'}",
        "  BatchMode yes",
        "  ConnectTimeout 15",
        "  ServerAliveInterval 30",
        # Reuse one connection per host/user/port for 60 s: quick command loops don't log in again
        # each time (MaxStartups, fail2ban). %C is a hash, so the socket path stays short.
        "  ControlMaster auto",
        f"  ControlPath {ssh_dir / 'cm' / '%C'}",
        "  ControlPersist 60s",
        "",
    ]
    _write_private(ssh_dir / "config", "\n".join(lines))
    info = SshInfo(
        configured=True,
        identities=[p.name for p in identities],
        strict=strict,
        hosts=_hosts_in(user_config),
    )
    logger.info(
        "ssh ready: %d key(s), host keys %s, hosts: %s",
        len(identities), strict, ", ".join(info.hosts) or "(any)",
    )
    return info


def prepare_environment(settings: Settings) -> None:
    """Env the Cursor bridges (and so the agent's shell) inherit: guard on PATH, how to reach the worker."""
    path = os.environ.get("PATH", "")
    if not path.startswith(str(BIN_DIR)):
        os.environ["PATH"] = f"{BIN_DIR}{os.pathsep}{path}" if path else str(BIN_DIR)
    os.environ["AGENT_WORKER_URL"] = f"http://127.0.0.1:{settings.port}"
    os.environ["AGENT_WORKER_APPROVAL_TIMEOUT"] = str(settings.ssh_approval_timeout)
