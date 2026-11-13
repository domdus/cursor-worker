from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp import types as mtypes

from agent_worker.app import create_app
from agent_worker.jobs import JobError, JobManager, JobRequest, Store
from agent_worker.mcp_server import build_mcp
from agent_worker.remote_guard import parse_ssh, scp_hosts
from agent_worker.ssh_setup import BIN_DIR, setup_ssh

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"


# ---- ~/.ssh from env ---------------------------------------------------------------------------


def test_setup_ssh_writes_keys_config_and_known_hosts(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "prod").write_text(KEY)
    (keys / "prod.pub").write_text("ssh-ed25519 AAA")
    home = tmp_path / "home"
    info = setup_ssh(
        home,
        {
            "SSH_KEY": KEY,
            "SSH_KEYS_DIR": str(keys),
            "SSH_CONFIG": "Host web01\n  HostName 10.0.0.5\n  User deploy\nHost prod-*\n  User root",
            "SSH_KNOWN_HOSTS": "10.0.0.5 ssh-ed25519 AAAA",
        },
    )
    ssh = home / ".ssh"
    assert info.configured and info.identities == ["id_worker", "prod"] and info.strict == "yes"
    assert info.hosts == ["web01"]
    assert stat.S_IMODE((ssh / "keys" / "id_worker").stat().st_mode) == 0o600
    assert not (ssh / "keys" / "prod.pub").exists()
    config = (ssh / "config").read_text()
    assert config.index("Include") < config.index("Host *")
    assert "StrictHostKeyChecking yes" in config and "BatchMode yes" in config
    assert "ControlMaster auto" in config and f"ControlPath {ssh / 'cm' / '%C'}" in config
    assert stat.S_IMODE((ssh / "cm").stat().st_mode) == 0o700
    assert f"IdentityFile {ssh / 'keys' / 'prod'}" in config
    assert (ssh / "known_hosts").read_text().startswith("10.0.0.5")


def test_setup_ssh_defaults_and_bad_values(tmp_path):
    assert not setup_ssh(tmp_path / "a", {}).configured
    info = setup_ssh(tmp_path / "b", {"SSH_KEY": "/does/not/exist", "GIT_SSH_KEY": KEY.replace("\n", "\\n")})
    assert info.identities == ["id_git"] and info.strict == "accept-new"
    assert (tmp_path / "b" / ".ssh" / "keys" / "id_git").read_text().count("\n") == 3


# ---- argument parsing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, expected",
    [
        (["web01", "uptime"], ([], "web01", ["uptime"])),
        (["-p", "2222", "-l", "root", "web01", "ls", "-la"], (["-p", "2222", "-l", "root"], "web01", ["ls", "-la"])),
        (["-tt", "host"], (["-tt"], "host", [])),
        (["-oBatchMode=yes", "host", "cmd"], (["-oBatchMode=yes"], "host", ["cmd"])),
        (["-vp", "22", "h", "x"], (["-vp", "22"], "h", ["x"])),
        (["-p22", "h"], (["-p22"], "h", [])),
        (["--", "h", "x"], ([], "h", ["x"])),
        (["-V"], None),
        (["-G", "h"], None),
        (["-v"], None),
    ],
)
def test_parse_ssh(args, expected):
    assert parse_ssh(args) == expected


@pytest.mark.parametrize(
    "args, hosts",
    [
        (["-r", "./a", "user@web01:/tmp/"], ["web01"]),
        (["web01:/x", "./y"], ["web01"]),
        (["/local", "./x"], []),
        (["scp://u@h:22/p", "."], ["h"]),
        (["-P", "2222", "f", "h:/p"], ["h"]),
        (["db01"], []),  # sftp's bare host has no colon; covered by its inner ssh call
    ],
)
def test_scp_hosts(args, hosts):
    assert scp_hosts(args) == hosts


# ---- approvals in the job manager --------------------------------------------------------------


def _manager(settings, engine, **overrides):
    return JobManager(replace(settings, **overrides), engine, Store(settings.data_dir / "worker.sqlite"))


async def _running(manager, engine, **request):
    engine.gate = asyncio.Event()
    job = manager.submit(JobRequest(task="deploy", **request))
    await asyncio.sleep(0.05)
    assert job.status == "running"
    return job


async def test_plain_host_is_logged_and_allowed(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod-*",))
    job = await _running(manager, engine)
    assert await manager.remote_call("default", "ssh", "web01", "10.0.0.5", "deploy", "uptime") == (True, "")
    assert job.events[-1]["text"] == "ssh deploy@web01: uptime"
    engine.gate.set()


async def test_approval_host_waits_for_a_decision(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod-*",))
    job = await _running(manager, engine)
    call = asyncio.create_task(manager.remote_call("default", "ssh", "prod-db", "10.0.9.9", "root", "reboot"))
    await asyncio.sleep(0.02)
    assert job.status == "needs_approval"
    assert job.pending_approval["command"] == "reboot"
    assert job.summary()["approval"]["host"] == "prod-db"
    manager.decide(job.id, True, "go")
    assert await call == (True, "")
    assert job.status == "running" and "prod-db" in job.approved_hosts
    # Approved once per job and host.
    assert await manager.remote_call("default", "ssh", "prod-db", "", "root", "uptime") == (True, "")
    with pytest.raises(JobError):
        manager.decide(job.id, True)
    engine.gate.set()
    await manager.wait(job, 5)
    assert job.status == "succeeded"


async def test_hostname_pattern_and_denial(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("10.0.9.*",))
    job = await _running(manager, engine)
    call = asyncio.create_task(manager.remote_call("default", "scp", "db", "10.0.9.9", "", "f db:/x"))
    await asyncio.sleep(0.02)
    manager.decide(job.id, False, "not today")
    allow, reason = await call
    assert not allow and "not today" in reason and "Do not retry" in reason
    engine.gate.set()


async def test_approval_timeout_and_cancel(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",), ssh_approval_timeout=0)
    job = await _running(manager, engine)
    allow, reason = await manager.remote_call("default", "ssh", "prod", "", "", "x")
    assert not allow and "No answer" in reason
    manager2 = _manager(settings, engine, ssh_approval_hosts=("prod",))
    other = await _running(manager2, engine, workspace="two")
    call = asyncio.create_task(manager2.remote_call("two", "ssh", "prod", "", "", "x"))
    await asyncio.sleep(0.02)
    manager2.cancel(other.id)
    allow, _ = await call
    assert not allow and other.status == "cancelled"
    engine.gate.set()
    await manager.wait(job, 5)


async def test_no_job_means_no_approval(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",))
    assert (await manager.remote_call("default", "ssh", "prod", "", "", "x"))[0] is False
    assert (await manager.remote_call("default", "ssh", "web", "", "", "x"))[0] is True


async def test_chat_only_jobs_are_not_the_active_job(settings, engine):
    manager = _manager(settings, engine)
    await _running(manager, engine, tools="none")
    assert manager.active_job("default") is None
    engine.gate.set()


# ---- HTTP: guard endpoint and approve API ------------------------------------------------------


def _http(app, host="127.0.0.1"):
    transport = httpx.ASGITransport(app=app, client=(host, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787")


async def test_guard_endpoint_and_approve_api(settings, engine):
    app = create_app(replace(settings, ssh_approval_hosts=("prod",)), engine)
    engine.gate = asyncio.Event()
    async with _http(app) as http, _http(app, "172.17.0.1") as outside:
        job = (await http.post("/jobs", json={"task": "t"})).json()
        await asyncio.sleep(0.05)
        body = {"workspace": "default", "tool": "ssh", "alias": "prod", "hostname": "prod", "command": "rm -rf /tmp/x"}
        assert (await outside.post("/internal/remote", json=body)).status_code == 403
        call = asyncio.create_task(http.post("/internal/remote", json=body))
        await asyncio.sleep(0.05)
        state = (await http.get(f"/jobs/{job['id']}")).json()
        assert state["status"] == "needs_approval" and state["approval"]["command"] == "rm -rf /tmp/x"
        assert (await http.post(f"/jobs/{job['id']}/approve", json={"approve": True})).status_code == 200
        assert (await call).json() == {"allow": True, "reason": ""}
        assert (await http.post(f"/jobs/{job['id']}/approve", json={"approve": True})).status_code == 409
        engine.gate.set()
        done = (await http.get(f"/jobs/{job['id']}", params={"wait": 5})).json()
        assert done["status"] == "succeeded" and done["approved_hosts"] == ["prod"]


# ---- MCP: elicitation asks the human -----------------------------------------------------------


def _ssh_hook(manager, results):
    async def hook(spec):
        results.append(await manager.remote_call(spec.workspace, "ssh", "prod", "prod", "root", "reboot"))

    return hook


async def test_mcp_elicitation_asks_the_user(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",))
    results: list = []
    engine.hook = _ssh_hook(manager, results)
    asked: list[str] = []

    async def elicit(_ctx, params):
        asked.append(params.message)
        return mtypes.ElicitResult(action="accept", content={"approve": True, "note": ""})

    async with Client(build_mcp(manager), elicitation_callback=elicit) as client:
        data = (await client.call_tool("agent_run", {"task": "reboot prod"})).structured_content
    assert data["status"] == "succeeded"
    assert results == [(True, "")]
    assert "reboot" in asked[0] and "root@prod" in asked[0]


async def test_mcp_without_elicitation_reports_needs_approval(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",), approval_tool=True)
    results: list = []
    engine.hook = _ssh_hook(manager, results)
    async with Client(build_mcp(manager)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "agent_approve" in names
        first = (await client.call_tool("agent_run", {"task": "reboot prod"})).structured_content
        assert first["status"] == "needs_approval" and first["approval"]["host"] == "prod"
        assert "Do not decide yourself" in first["hint"]
        done = (await client.call_tool("agent_approve", {"job_id": first["job_id"], "approve": False})).structured_content
    assert done["status"] == "succeeded"
    assert results[0][0] is False


async def test_approve_tool_is_off_by_default(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        assert "agent_approve" not in {t.name for t in (await client.list_tools()).tools}


# ---- Open WebUI: answer in the chat ------------------------------------------------------------


async def test_openai_chat_pauses_and_continues(settings, engine):
    app = create_app(replace(settings, ssh_approval_hosts=("prod",)), engine)
    manager = app.state.jobs
    results: list = []
    engine.hook = _ssh_hook(manager, results)
    headers = {"X-OpenWebUI-Chat-Id": "c1"}

    def content(resp):
        text = ""
        for line in resp.text.splitlines():
            if line.startswith("data: {"):
                chunk = json.loads(line[6:])
                if chunk["choices"]:
                    text += chunk["choices"][0]["delta"].get("content", "")
        return text

    async with _http(app) as http:
        ask = {"model": "composer-2.5", "stream": True, "messages": [{"role": "user", "content": "reboot prod"}]}
        first = await http.post("/v1/chat/completions", json=ask, headers=headers)
        assert "Approval needed" in content(first) and "reboot" in content(first)
        job = manager.list(1)[0]
        assert job.status == "needs_approval"  # not cancelled when the reply ended
        other = {**ask, "messages": [*ask["messages"], {"role": "user", "content": "what?"}]}
        assert "Approval needed" in content(await http.post("/v1/chat/completions", json=other, headers=headers))
        yes = {**ask, "messages": [*ask["messages"], {"role": "user", "content": "Approve"}]}
        second = await http.post("/v1/chat/completions", json=yes, headers=headers)
    assert content(second) == "Done: reboot prod"
    assert results == [(True, "")] and job.status == "succeeded"


# ---- the real guard script ---------------------------------------------------------------------


class _Worker(BaseHTTPRequestHandler):
    calls: list = []
    allow = True

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Worker.calls.append(body)
        reply = json.dumps({"allow": _Worker.allow, "reason": "" if _Worker.allow else "nope"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_world(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    for name in ("ssh", "scp", "sftp"):
        script = real / name
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "-G" ]; then echo "hostname 10.0.0.5"; echo "user deploy"; exit 0; fi\n'
            f'echo "REAL {name} $*"\n'
        )
        script.chmod(0o755)
    server = HTTPServer(("127.0.0.1", 0), _Worker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Worker.calls = []
    _Worker.allow = True
    env = {
        **os.environ,
        "PATH": f"{BIN_DIR}{os.pathsep}{real}{os.pathsep}{os.environ['PATH']}",
        "AGENT_WORKER_URL": f"http://127.0.0.1:{server.server_port}",
        "AGENT_WORKER_WORKSPACE": "default",
    }
    env.pop(f"AGENT_WORKER_GUARD_DONE", None)
    yield env
    server.shutdown()


def _run(env, *argv):
    return subprocess.run(list(argv), env=env, capture_output=True, text=True, timeout=20)


def test_guard_reports_and_runs_the_real_ssh(fake_world):
    out = _run(fake_world, "ssh", "-p", "2222", "web01", "systemctl", "restart", "nginx")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "REAL ssh -p 2222 web01 systemctl restart nginx"
    call = _Worker.calls[0]
    assert call["alias"] == "web01" and call["hostname"] == "10.0.0.5" and call["user"] == "deploy"
    assert call["command"] == "systemctl restart nginx" and call["workspace"] == "default"


def test_guard_refuses_when_denied(fake_world):
    _Worker.allow = False
    out = _run(fake_world, "ssh", "prod", "reboot")
    assert out.returncode == 255 and "nope" in out.stderr and "REAL" not in out.stdout


def test_guard_fails_closed_without_the_worker(fake_world):
    env = {**fake_world, "AGENT_WORKER_URL": "http://127.0.0.1:9"}
    out = _run(env, "ssh", "web01", "uptime")
    assert out.returncode == 255 and "could not reach" in out.stderr


def test_guard_passes_through_info_calls_and_outside_the_worker(fake_world):
    assert _run(fake_world, "ssh", "-V").stdout.startswith("REAL ssh -V")
    env = {k: v for k, v in fake_world.items() if k != "AGENT_WORKER_URL"}
    assert _run(env, "ssh", "web01", "x").stdout.startswith("REAL ssh")
    assert _Worker.calls == []


def test_guard_scp_reports_hosts_and_points_at_the_guard(fake_world):
    out = _run(fake_world, "scp", "./a", "deploy@web01:/tmp/")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"REAL scp -S {BIN_DIR / 'ssh'} ./a deploy@web01:/tmp/"
    assert _Worker.calls[0]["tool"] == "scp" and _Worker.calls[0]["alias"] == "web01"
