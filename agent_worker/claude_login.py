"""Sign in with Claude: get a Claude Plan token for the worker without Claude Code on your computer.

Runs Anthropic's own `claude setup-token` (the Claude Code CLI bundled with claude-agent-sdk) in a
pseudo-terminal. The CLI cannot open a browser in the container, so it prints a sign-in link and
waits for the code that Anthropic's page shows after sign-in. The code goes back to the CLI, which
prints a Claude Plan token (valid for one year). The worker keeps it in DATA_DIR (owner-only file),
so no restart is needed. CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in the environment still win.

Two ways in: `docker exec -it agent-worker python -m agent_worker.claude_login`, or
POST /auth/claude/login and POST /auth/claude/login/{id} with the code.

The CLI gets a temporary config folder that is deleted afterwards. The token and code are never logged.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import logging
import os
import pty
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

LINK_SECONDS = 45
TOKEN_SECONDS = 60
# A sign-in that nobody finishes is dropped after this long.
LOGIN_TTL_SECONDS = 15 * 60
_MAX_CODE = 512

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]")
_LINK = re.compile(r"https://\S*/oauth/authorize\?\S+")
_PROMPT = re.compile(r"Paste\s*code", re.I)
_TOKEN = re.compile(r"sk-ant-oat\d*-[A-Za-z0-9_-]{20,}")
_REJECTED = re.compile(r"OAuth\s*error|wasn't\s*accepted|was\s*not\s*accepted|invalid\s*code", re.I)
_CODE = re.compile(r"^[A-Za-z0-9_\-#.~]+$")


class LoginError(Exception):
    """`key` is what the agent form shows: cli_missing, no_link, code_rejected, bad_code, timeout, expired."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key


def cli_path() -> str:
    """The Claude Code CLI that ships inside claude-agent-sdk, or one on PATH."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec and spec.origin:
        bundled = Path(spec.origin).parent / "_bundled" / "claude"
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    return shutil.which("claude") or ""


def plain_text(raw: bytes) -> str:
    """Terminal output without colours and cursor moves (a cursor move counts as a space)."""
    return _ANSI.sub(b" ", raw).decode("utf-8", "replace").replace("\r", "")


def find_link(text: str) -> str:
    match = _LINK.search(text)
    return match.group(0) if match else ""


def find_token(text: str) -> str:
    match = _TOKEN.search(text)
    return match.group(0) if match else ""


def clean_code(code: str) -> str:
    """The code from Anthropic's page, or an error: never anything that types more into the terminal."""
    value = str(code or "").strip()
    if not value or len(value) > _MAX_CODE or not _CODE.match(value):
        raise LoginError("bad_code")
    return value


def _child_env(home: str) -> dict[str, str]:
    """Only what the CLI needs; never the runtime's own Claude or Cursor credentials."""
    keep = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    # Never the worker's own credentials: ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, CURSOR_API_KEY,
    # WORKER_TOKEN, GIT_TOKEN and SSH keys stay out of the sign-in.
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(
        {
            "HOME": home,
            "CLAUDE_CONFIG_DIR": home,
            "TERM": "xterm-256color",
            "BROWSER": "none",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    return env


class ClaudeLogin:
    """One run of `claude setup-token`."""

    def __init__(self, cli: str) -> None:
        self.id = secrets.token_urlsafe(16)
        self.created = time.monotonic()
        self._cli = cli
        self._home = tempfile.mkdtemp(prefix="claude-login-")
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._reader: threading.Thread | None = None
        self._sent_at = 0

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > LOGIN_TTL_SECONDS

    def _text(self, since: int = 0) -> str:
        with self._lock:
            return plain_text(bytes(self._buf[since:]))

    def _read(self) -> None:
        master = self._master
        while master is not None:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)

    def _spawn(self) -> None:
        master, slave = pty.openpty()
        # Wide enough that the sign-in link is printed on one line.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
        self._proc = subprocess.Popen(
            [self._cli, "setup-token"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=_child_env(self._home),
            cwd=self._home,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        self._master = master
        self._reader = threading.Thread(target=self._read, name="claude-login", daemon=True)
        self._reader.start()

    async def _wait(self, check, seconds: float, since: int = 0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = self._text(since)
            found = check(text)
            if found:
                return found
            if self._proc is not None and self._proc.poll() is not None:
                # One last look at what it printed before it ended.
                return check(self._text(since)) or None
            await asyncio.sleep(0.2)
        return None

    async def start(self) -> str:
        """Start the CLI and return the sign-in link."""
        await asyncio.to_thread(self._spawn)
        link = await self._wait(lambda text: find_link(text) if _PROMPT.search(text) else "", LINK_SECONDS)
        if not link:
            self.close()
            raise LoginError("no_link")
        return link

    async def finish(self, code: str) -> str:
        """Hand the code to the CLI and return the Claude Plan token it prints."""
        value = clean_code(code)
        if self._master is None or self._proc is None or self._proc.poll() is not None:
            raise LoginError("expired")
        with self._lock:
            since = len(self._buf)
        os.write(self._master, value.encode() + b"\r")

        def outcome(text: str):
            token = find_token(text)
            if token:
                return ("token", token)
            if _REJECTED.search(text):
                return ("rejected", "")
            return None

        result = await self._wait(outcome, TOKEN_SECONDS, since)
        self.close()
        if not result:
            raise LoginError("timeout")
        if result[0] == "rejected":
            raise LoginError("code_rejected")
        return result[1]

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("claude setup-token did not stop")
        master, self._master = self._master, None
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        shutil.rmtree(self._home, ignore_errors=True)


class LoginManager:
    """At most one sign-in at a time; a new one replaces the old one."""

    def __init__(self, cli_finder=None) -> None:
        # Looked up on each use, so a CLI that appears later (or a test) is picked up.
        self._cli_finder = cli_finder or (lambda: cli_path())
        self._current: ClaudeLogin | None = None

    def available(self) -> bool:
        return bool(self._cli_finder())

    def _drop(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None

    async def start(self) -> ClaudeLogin:
        cli = self._cli_finder()
        if not cli:
            raise LoginError("cli_missing")
        self._drop()
        login = ClaudeLogin(cli)
        self._current = login
        try:
            login.link = await login.start()
        except BaseException:
            if self._current is login:
                self._current = None
            raise
        return login

    async def finish(self, login_id: str, code: str) -> str:
        login = self._current
        if login is None or login.id != str(login_id or "") or login.expired:
            if login is not None and login.expired:
                self._drop()
            raise LoginError("expired")
        # A mistyped code keeps the sign-in open for another try.
        clean_code(code)
        try:
            return await login.finish(code)
        finally:
            if self._current is login:
                self._current = None

    def cancel(self, login_id: str) -> None:
        if self._current is not None and self._current.id == str(login_id or ""):
            self._drop()

    def close(self) -> None:
        self._drop()


# ---- keeping the token ---------------------------------------------------------------------------


def save_token(path: Path, token: str) -> None:
    """Write the Plan token owner-only, replacing an older one in one step."""
    if not _TOKEN.fullmatch(token or ""):
        raise LoginError("login_failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.replace(tmp, path)


def load_token(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if _TOKEN.fullmatch(value) else ""


def forget_token(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# ---- docker exec -it agent-worker python -m agent_worker.claude_login ----------------------------


async def _interactive(path: Path) -> int:
    manager = LoginManager()
    if not manager.available():
        print("The Claude Code CLI was not found. Is claude-agent-sdk installed in this image?", file=sys.stderr)
        return 1
    print("Starting Claude sign-in...", flush=True)
    try:
        login = await manager.start()
    except LoginError as exc:
        print(f"Could not start the sign-in ({exc.key}).", file=sys.stderr)
        return 1
    print("\n1. Open this link and sign in with the Claude account that has your plan:\n")
    print(login.link)
    print("\n2. Anthropic then shows a code. Paste it here and press Enter.\n", flush=True)
    for _attempt in range(3):
        try:
            code = (await asyncio.to_thread(input, "Code: ")).strip()
        except (EOFError, KeyboardInterrupt):
            manager.close()
            print("\nCancelled. Run it with `docker exec -it` so you can paste the code.", file=sys.stderr)
            return 1
        try:
            token = await manager.finish(login.id, code)
        except LoginError as exc:
            if exc.key == "bad_code":
                print("That is not a code from Anthropic's page. Copy the whole code.", file=sys.stderr)
                continue
            print(f"Sign-in failed ({exc.key}). Run this command again for a new link.", file=sys.stderr)
            return 1
        save_token(path, token)
        print(f"\nSigned in. The Claude Plan token is saved in {path} and lasts one year.")
        print("Claude jobs use it from now on; no restart needed.")
        return 0
    manager.close()
    return 1


def main() -> None:
    from agent_worker.settings import Settings

    settings = Settings.from_env()
    if settings.claude_oauth_token or settings.anthropic_api_key:
        print(
            "Note: CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY is set on the container and wins over a "
            "signed-in token.",
            file=sys.stderr,
        )
    raise SystemExit(asyncio.run(_interactive(settings.claude_token_path)))


if __name__ == "__main__":
    main()
