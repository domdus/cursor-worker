"""Sign in with Claude: the worker drives `claude setup-token` in a terminal and keeps the Plan token.

A fake `claude` program stands in for the real CLI: it prints the same lines (link, prompt, token or
OAuth error) so the terminal handling, link and token reading run for real.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_worker import claude_login
from agent_worker.app import create_app
from agent_worker.claude_login import (
    LoginError,
    LoginManager,
    clean_code,
    find_link,
    find_token,
    load_token,
    plain_text,
)
from tests.conftest import FakeEngine

LINK = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference&code_challenge=abc&code_challenge_method=S256&state=xyz"
)
TOKEN = "sk-ant-oat01-" + "Ab3_-" * 20

_FAKE = f'''#!{sys.executable}
import os, sys, time
assert sys.argv[1:] == ["setup-token"], sys.argv
leaked = [k for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CURSOR_API_KEY", "RUNTIME_TOKEN") if k in os.environ]
out = sys.stdout
out.write("\\x1b[2J\\x1b[1;1HWelcome\\x1b[1Cto\\x1b[1CClaude\\x1b[1CCode\\n")
out.write("\\x1b[33m\\u00b7 Opening browser to sign in\\u2026\\x1b[0m\\n")
if leaked:
    out.write("LEAKED " + ",".join(leaked) + "\\n")
out.write("Browser didn't open? Use the url below to sign in (c to copy)\\n\\n")
out.write("{LINK}\\n\\n")
out.write("Paste\\x1b[1Ccode\\x1b[1Chere\\x1b[1Cif\\x1b[1Cprompted\\x1b[1C>\\x1b[?25h")
out.flush()
code = sys.stdin.readline().strip()
open(os.path.join(os.environ["HOME"], "code-seen"), "w").write(code)
if code == "good#state":
    out.write("\\n \\x1b[32m\\u2713 Long-lived authentication token created successfully!\\x1b[0m\\n\\n")
    out.write(" Your OAuth token (valid for 1 year):\\n\\n \\x1b[1m{TOKEN}\\x1b[0m\\n\\n")
    out.write(" Store this token securely.\\n")
else:
    out.write("\\nOAuth error: That code wasn't accepted. It may be incomplete, expired, already used.\\n")
    out.write("Press Enter to retry.\\n")
out.flush()
time.sleep(30)
'''


@pytest.fixture
def fake_cli(tmp_path: Path) -> str:
    path = tmp_path / "claude"
    path.write_text(_FAKE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_reading_link_token_and_codes() -> None:
    raw = b"Paste\x1b[1Ccode\x1b[1Chere\n" + LINK.encode() + b"\x1b[0m\n"
    text = plain_text(raw)
    assert "Paste code here" in text
    assert find_link(text) == LINK
    assert find_token(plain_text(f"token:\n \x1b[1m{TOKEN}\x1b[0m".encode())) == TOKEN
    assert clean_code("  abc-DEF_123#state.x~ ") == "abc-DEF_123#state.x~"
    for bad in ("", "a b", "code\r\nrm -rf /", "x" * 600, "\x03"):
        with pytest.raises(LoginError) as err:
            clean_code(bad)
        assert err.value.key == "bad_code"


def test_sign_in_returns_the_token_and_cleans_up(fake_cli, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-runtime")
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_runtime")

    async def run():
        manager = LoginManager(cli_finder=lambda: fake_cli)
        login = await manager.start()
        assert login.link == LINK
        home = login._home
        assert os.path.isdir(home)
        assert "LEAKED" not in login._text()
        token = await manager.finish(login.id, "good#state")
        assert token == TOKEN
        # The temporary Claude config folder is gone and the CLI has stopped.
        assert not os.path.exists(home)
        with pytest.raises(LoginError) as err:
            await manager.finish(login.id, "good#state")
        assert err.value.key == "expired"

    asyncio.run(run())


def test_a_wrong_code_is_reported_and_a_mistyped_one_keeps_the_sign_in(fake_cli) -> None:
    async def run():
        manager = LoginManager(cli_finder=lambda: fake_cli)
        login = await manager.start()
        with pytest.raises(LoginError) as err:
            await manager.finish(login.id, "has space")
        assert err.value.key == "bad_code"
        with pytest.raises(LoginError) as err:
            await manager.finish(login.id, "wrong#state")
        assert err.value.key == "code_rejected"
        manager.close()

    asyncio.run(run())


def test_only_one_sign_in_at_a_time_and_missing_cli(fake_cli) -> None:
    async def run():
        manager = LoginManager(cli_finder=lambda: fake_cli)
        first = await manager.start()
        second = await manager.start()
        assert first.id != second.id and first._proc is None
        with pytest.raises(LoginError) as err:
            await manager.finish(first.id, "good#state")
        assert err.value.key == "expired"
        manager.cancel(second.id)
        assert second._proc is None
        missing = LoginManager(cli_finder=lambda: "")
        assert not missing.available()
        with pytest.raises(LoginError) as err:
            await missing.start()
        assert err.value.key == "cli_missing"

    asyncio.run(run())


def test_an_old_sign_in_expires(fake_cli, monkeypatch) -> None:
    async def run():
        manager = LoginManager(cli_finder=lambda: fake_cli)
        login = await manager.start()
        real = time.monotonic
        monkeypatch.setattr(claude_login.time, "monotonic", lambda: real() + 10_000)
        with pytest.raises(LoginError) as err:
            await manager.finish(login.id, "good#state")
        assert err.value.key == "expired"
        assert login._proc is None

    asyncio.run(run())


def test_endpoints_save_the_token_and_never_return_it(settings, fake_cli, monkeypatch) -> None:
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    secured = replace(settings, token="secret")
    with TestClient(create_app(secured, FakeEngine()), base_url="http://127.0.0.1") as client:
        assert client.post("/auth/claude/login").status_code == 401
        auth = {"Authorization": "Bearer secret"}
        started = client.post("/auth/claude/login", headers=auth).json()
        assert started["url"] == LINK and started["id"]
        wrong = client.post(f"/auth/claude/login/{started['id']}", json={"code": "nope"}, headers=auth)
        assert wrong.status_code == 400 and wrong.json()["detail"] == "code_rejected"
        again = client.post("/auth/claude/login", headers=auth).json()
        done = client.post(f"/auth/claude/login/{again['id']}", json={"code": "good#state"}, headers=auth)
        assert done.status_code == 200 and TOKEN not in done.text
        assert done.json()["credential"] == "signed-in"
        assert load_token(secured.claude_token_path) == TOKEN
        assert oct(secured.claude_token_path.stat().st_mode & 0o777) == "0o600"
        assert client.get("/health").json()["claude"]["credential"] == "signed-in"
        assert client.delete("/auth/claude/token", headers=auth).json() == {"removed": True}
        assert load_token(secured.claude_token_path) == ""


def test_env_credentials_win_over_a_signed_in_token(settings, fake_cli, monkeypatch) -> None:
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    env = replace(settings, anthropic_api_key="sk-ant-api03-env")
    with TestClient(create_app(env, FakeEngine()), base_url="http://127.0.0.1") as client:
        started = client.post("/auth/claude/login").json()
        done = client.post(f"/auth/claude/login/{started['id']}", json={"code": "good#state"}).json()
    assert done["credential"] == "env-api" and "wins" in done["note"]


def test_missing_cli(settings, monkeypatch) -> None:
    monkeypatch.setattr(claude_login, "cli_path", lambda: "")
    with TestClient(create_app(settings, FakeEngine()), base_url="http://127.0.0.1") as client:
        failed = client.post("/auth/claude/login")
    assert failed.status_code == 501 and failed.json()["detail"] == "cli_missing"


def test_interactive_command(settings, fake_cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    answers = iter(["not a code", "good#state"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert asyncio.run(claude_login._interactive(settings.claude_token_path)) == 0
    out = capsys.readouterr()
    assert LINK in out.out and TOKEN not in out.out and "Copy the whole code" in out.err
    assert load_token(settings.claude_token_path) == TOKEN


def test_interactive_command_without_a_terminal(settings, fake_cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)

    def no_input(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)
    assert asyncio.run(claude_login._interactive(settings.claude_token_path)) == 1
    assert "docker exec -it" in capsys.readouterr().err
    assert load_token(settings.claude_token_path) == ""
