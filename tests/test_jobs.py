from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace

import pytest

from agent_worker.engine import RunSpec, compose_message, git_changes, git_head
from agent_worker.jobs import JobError, JobManager, JobRequest, Store


def manager_for(settings, engine):
    return JobManager(settings, engine, Store(settings.data_dir / "worker.sqlite"))


async def test_job_runs_and_records_events(settings, engine):
    manager = manager_for(settings, engine)
    job = manager.submit(JobRequest(task="fix the bug"))
    await manager.wait(job, 5)
    assert job.status == "succeeded"
    assert job.result == "Done: fix the bug"
    assert job.changes["files"] == [" M README.md"]
    kinds = [e["type"] for e in job.events]
    assert kinds[0] == "status" and "tool" in kinds and kinds[-1] == "result"
    assert [e["seq"] for e in job.events] == list(range(1, len(job.events) + 1))


async def test_spec_gets_depth_and_narrowed_tools(settings, engine):
    manager = manager_for(replace(settings, tool_profile="local"), engine)
    job = manager.submit(
        JobRequest(task="t", tools="full", mcp_servers={"gh": {"url": "https://example.com/mcp"}}, depth=0)
    )
    await manager.wait(job, 5)
    spec = engine.specs[0]
    assert spec.profile == "local"
    assert spec.mcp_servers["gh"]["headers"]["X-Agent-Worker-Depth"] == "1"


async def test_nested_call_is_refused(settings, engine):
    manager = manager_for(settings, engine)
    with pytest.raises(JobError) as err:
        manager.submit(JobRequest(task="t", depth=1))
    assert err.value.status == 409


async def test_request_mcp_can_be_disabled(settings, engine):
    manager = manager_for(replace(settings, allow_request_mcp=False), engine)
    with pytest.raises(JobError):
        manager.submit(JobRequest(task="t", mcp_servers={"x": {"url": "https://x/mcp"}}))


async def test_same_workspace_jobs_run_one_at_a_time(settings, engine):
    engine.gate = asyncio.Event()
    manager = manager_for(replace(settings, max_concurrent_jobs=4), engine)
    first = manager.submit(JobRequest(task="a"))
    second = manager.submit(JobRequest(task="b"))
    other = manager.submit(JobRequest(task="c", workspace="other"))
    chat_only = manager.submit(JobRequest(task="d", tools="none"))
    await asyncio.sleep(0.05)
    assert engine.running == 3  # a, c, d — b waits for a
    assert second.status == "queued"
    engine.gate.set()
    for job in (first, second, other, chat_only):
        await manager.wait(job, 5)
        assert job.status == "succeeded"


async def test_cancel_running_and_queued(settings, engine):
    engine.gate = asyncio.Event()
    manager = manager_for(settings, engine)
    running = manager.submit(JobRequest(task="a"))
    queued = manager.submit(JobRequest(task="b"))
    await asyncio.sleep(0.05)
    manager.cancel(queued.id)
    manager.cancel(running.id)
    await manager.wait(running, 5)
    await manager.wait(queued, 5)
    assert running.status == "cancelled"
    assert queued.status == "cancelled"


async def test_cancel_before_first_step(settings, engine):
    manager = manager_for(settings, engine)
    job = manager.submit(JobRequest(task="a"))
    manager.cancel(job.id)
    await manager.wait(job, 5)
    assert job.status == "cancelled"


async def test_timeout_fails_the_job(settings, engine):
    engine.gate = asyncio.Event()
    manager = manager_for(replace(settings, job_timeout=0), engine)
    job = manager.submit(JobRequest(task="a"))
    await manager.wait(job, 5)
    assert job.status == "failed"
    assert "JOB_TIMEOUT_SECONDS" in job.error


async def test_engine_error_fails_the_job(settings, engine):
    engine.fail = "bad key"
    manager = manager_for(settings, engine)
    job = manager.submit(JobRequest(task="a"))
    await manager.wait(job, 5)
    assert job.status == "failed" and job.error == "bad key"


async def test_follow_streams_until_done(settings, engine):
    engine.gate = asyncio.Event()
    manager = manager_for(settings, engine)
    job = manager.submit(JobRequest(task="a"))

    async def release():
        await asyncio.sleep(0.05)
        engine.gate.set()

    asyncio.create_task(release())
    seen = [e async for e in manager.follow(job)]
    assert seen[-1]["type"] == "result"
    assert job.done


async def test_restart_restores_jobs_and_fails_running_ones(settings, engine):
    manager = manager_for(settings, engine)
    done = manager.submit(JobRequest(task="a"))
    await manager.wait(done, 5)
    engine.gate = asyncio.Event()
    stuck = manager.submit(JobRequest(task="b"))
    await asyncio.sleep(0.05)
    assert stuck.status == "running"

    # A second process reading the same database is what a restart looks like.
    again = manager_for(settings, engine)
    assert again.get(done.id).status == "succeeded"
    assert again.get(done.id).events
    restored = again.get(stuck.id)
    assert restored.status == "failed" and "restarted" in restored.error
    engine.gate.set()
    await manager.wait(stuck, 5)


def test_compose_message_only_briefs_new_agents(tmp_path):
    spec = RunSpec(job_id="j", task="do it", workspace="w", path=tmp_path, context="User: hi", instructions="Be terse.")
    fresh = compose_message(spec, True, "House rules.")
    assert "headless worker" in fresh and "House rules." in fresh and "User: hi" in fresh and fresh.endswith("do it")
    assert compose_message(spec, False, "House rules.") == "do it"


async def test_git_changes_reports_files_and_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("1")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    before = await git_head(repo)
    (repo / "a.txt").write_text("2")
    subprocess.run([*git, "commit", "-qam", "two"], check=True)
    (repo / "b.txt").write_text("new")
    changes = await git_changes(repo, before)
    assert changes["files"] == ["?? b.txt"]
    assert len(changes["commits"]) == 1 and changes["commits"][0].endswith("two")
    assert await git_changes(tmp_path, "") is None
