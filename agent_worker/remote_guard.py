"""Stand-in for ssh, scp and sftp on the agent's PATH.

Before the real program runs, the guard tells the worker which host and command are about to run.
The worker records it in the job and, for hosts in SSH_APPROVAL_HOSTS, holds the call until a human
approves or denies it. rsync and git call `ssh` from PATH, so they pass through here as well.

This is a guardrail for an agent that follows its instructions, not a sandbox: a process that calls
/usr/bin/ssh directly is not seen. Hard limits belong on the server (key, user, authorized_keys).

Standard library only, run with `python3 -I`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# ssh options that take a value (from ssh(1)).
_SSH_VALUE_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
# scp/sftp options that take a value.
_SCP_VALUE_OPTS = set("cDFiJloPSX")
_PASS_THROUGH = {"-V", "-G", "-Q", "-h", "--help"}
DONE_ENV = "AGENT_WORKER_GUARD_DONE"


def real_program(name: str, own_dir: str) -> str:
    own = os.path.realpath(own_dir)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or os.path.realpath(entry) == own:
            continue
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return f"/usr/bin/{name}"


def parse_ssh(args: list[str]) -> tuple[list[str], str, list[str]] | None:
    """(options, destination, remote command) or None when there is no destination (ssh -V and friends)."""
    opts: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        if arg in _PASS_THROUGH or (len(arg) == 2 and arg[1] in "GVQ"):
            return None
        opts.append(arg)
        # -p22 carries its value; -p 22 takes the next argument. Flags can be grouped: -tt, -vp 22.
        letters = arg[1:]
        for pos, letter in enumerate(letters):
            if letter in _SSH_VALUE_OPTS:
                if pos == len(letters) - 1:
                    i += 1
                    if i < len(args):
                        opts.append(args[i])
                break
        i += 1
    if i >= len(args):
        return None
    return opts, args[i], args[i + 1 :]


def scp_hosts(args: list[str]) -> list[str]:
    """Remote hosts named in scp/sftp arguments: user@host:path, host:path, scp://host/..., sftp host."""
    hosts: list[str] = []
    i = 0
    operands: list[str] = []
    while i < len(args):
        arg = args[i]
        if arg == "--":
            operands.extend(args[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1:
            if arg[-1] in _SCP_VALUE_OPTS and len(arg) == 2:
                i += 1
            i += 1
            continue
        operands.append(arg)
        i += 1
    for item in operands:
        if "://" in item:
            item = item.split("://", 1)[1].split("/", 1)[0]
        elif ":" in item and not item.startswith(("/", ".")) and "/" not in item.split(":", 1)[0]:
            item = item.split(":", 1)[0]
        else:
            continue
        host = item.rsplit("@", 1)[-1].split(":", 1)[0].strip("[]")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def resolve(real_ssh: str, opts: list[str], destination: str) -> tuple[str, str, str]:
    """(alias, hostname, user) as ssh itself resolves them through ~/.ssh/config."""
    target = destination
    if target.startswith("ssh://"):
        target = target[6:].split("/", 1)[0]
    alias = target.rsplit("@", 1)[-1]
    if alias.startswith("[") and "]" in alias:
        alias = alias[1 : alias.index("]")]
    elif alias.count(":") == 1:
        alias = alias.split(":", 1)[0]
    hostname, user = alias, ""
    try:
        out = subprocess.run(
            [real_ssh, "-G", *opts, destination], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        for line in out.splitlines():
            key, _, value = line.partition(" ")
            if key == "hostname":
                hostname = value.strip()
            elif key == "user":
                user = value.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return alias, hostname, user


def ask_worker(payload: dict) -> tuple[bool, str]:
    url = os.environ.get("AGENT_WORKER_URL", "").rstrip("/") + "/internal/remote"
    try:
        timeout = float(os.environ.get("AGENT_WORKER_APPROVAL_TIMEOUT", "900")) + 30
    except ValueError:
        timeout = 930.0
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"could not reach the agent worker to record this call ({exc}); refusing to run it"
    return bool(body.get("allow")), str(body.get("reason") or "")


def _payload(tool: str, alias: str, hostname: str, user: str, command: str) -> dict:
    return {
        "workspace": os.environ.get("AGENT_WORKER_WORKSPACE", ""),
        "cwd": os.getcwd(),
        "tool": tool,
        "alias": alias,
        "hostname": hostname,
        "user": user,
        "command": command[:2000],
    }


def main(argv: list[str]) -> int:
    tool, args = argv[1], argv[2:]
    own_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    real = real_program(tool, own_dir)
    guarded = bool(os.environ.get("AGENT_WORKER_URL")) and not os.environ.get(DONE_ENV)
    if not guarded:
        os.execv(real, [real, *args])
    real_ssh = real if tool == "ssh" else real_program("ssh", own_dir)
    if tool == "ssh":
        parsed = parse_ssh(args)
        if parsed is None:
            os.execv(real, [real, *args])
        opts, destination, command = parsed
        alias, hostname, user = resolve(real_ssh, opts, destination)
        calls = [(alias, hostname, user, " ".join(command))]
    else:
        calls = []
        for host in scp_hosts(args):
            alias, hostname, user = resolve(real_ssh, [], host)
            calls.append((alias, hostname, user, " ".join(args)))
    for alias, hostname, user, command in calls:
        allow, reason = ask_worker(_payload(tool, alias, hostname, user, command))
        if not allow:
            sys.stderr.write(f"agent-worker: {reason or 'this call was refused'}\n")
            return 255
    if tool in {"scp", "sftp"}:
        # Their inner ssh call was covered above; point them at the guard so PATH stays honest.
        os.environ[DONE_ENV] = "1"
        args = ["-S", os.path.join(own_dir, "ssh"), *args]
    os.execv(real, [real, *args])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
