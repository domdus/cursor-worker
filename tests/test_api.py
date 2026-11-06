from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cursor_worker.app import create_app
from cursor_worker.openai_compat import messages_to_request, split_model


@pytest.fixture
def client(settings, engine):
    with TestClient(create_app(settings, engine), base_url="http://127.0.0.1:8787") as c:
        yield c


@pytest.fixture
def token_client(settings, engine):
    secured = replace(settings, host="0.0.0.0", token="s3cret")
    with TestClient(create_app(secured, engine), base_url="http://worker:8787") as c:
        yield c


def test_health_is_open(client):
    body = client.get("/health").json()
    assert body["ok"] and body["cursor"] and body["token_required"] is False


def test_loopback_mode_refuses_other_host_names(settings, engine):
    with TestClient(create_app(settings, engine), base_url="http://evil.example") as c:
        assert c.get("/health").status_code == 403


def test_token_required_off_loopback(token_client):
    assert token_client.get("/health").status_code == 200
    assert token_client.get("/jobs").status_code == 401
    assert token_client.get("/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert token_client.get("/jobs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert token_client.post("/mcp", json={}).status_code == 401
    assert token_client.get("/v1/models").status_code == 401


def test_no_token_off_loopback_refuses_everything(settings, engine):
    exposed = replace(settings, host="0.0.0.0")
    with TestClient(create_app(exposed, engine), base_url="http://worker:8787") as c:
        assert c.get("/jobs").status_code == 401


def test_job_lifecycle(client):
    created = client.post("/jobs", json={"task": "add tests", "wait": 5})
    assert created.status_code == 200
    job = created.json()
    assert job["status"] == "succeeded" and job["result"] == "Done: add tests"
    assert client.get(f"/jobs/{job['id']}").json()["changes"]["files"] == [" M README.md"]
    assert client.get("/jobs").json()["jobs"][0]["id"] == job["id"]
    events = client.get(f"/jobs/{job['id']}/events", params={"follow": "false"}).json()["events"]
    assert events[-1]["type"] == "result"


def test_job_events_sse(client):
    job = client.post("/jobs", json={"task": "x"}).json()
    with client.stream("GET", f"/jobs/{job['id']}/events") as resp:
        text = "".join(resp.iter_text())
    assert "event: tool" in text and "event: end" in text
    assert '"status": "succeeded"' in text


def test_bad_requests(client):
    assert client.post("/jobs", json={"task": "x", "workspace": "../etc"}).status_code == 400
    assert client.post("/jobs", json={"task": " "}).status_code == 400
    assert client.post("/jobs", json={"task": "x"}, headers={"X-Cursor-Worker-Depth": "1"}).status_code == 409
    assert client.get("/jobs/nope").status_code == 404


def test_workspaces(client):
    client.post("/jobs", json={"task": "x", "workspace": "repo-a", "wait": 5})
    names = [w["name"] for w in client.get("/workspaces").json()["workspaces"]]
    assert names == ["default", "repo-a"]


def test_openai_models(client):
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert ids == ["composer-2.5", "gpt-5"]


def test_openai_chat_non_stream(client, engine):
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "composer-2.5@repo-b",
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": [{"type": "text", "text": "second"}]},
            ],
        },
        headers={"X-OpenWebUI-Chat-Id": "chat-1"},
    )
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Done: second"
    assert body["usage"]["total_tokens"] == 15
    spec = engine.specs[-1]
    assert spec.workspace == "repo-b" and spec.session == "chat-1" and spec.model == "composer-2.5"
    assert spec.instructions == "Be terse." and "User: first" in spec.context and "Assistant: ok" in spec.context


def test_openai_chat_stream(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "composer-2.5", "stream": True, "stream_options": {"include_usage": True},
              "messages": [{"role": "user", "content": "hello"}]},
    ) as resp:
        lines = [line for line in resp.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks if c["choices"])
    assert content == "Done: hello"
    assert "Calling shell" in reasoning
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert chunks[-1]["usage"]["total_tokens"] == 15


def test_openai_failure_is_shown(client, engine):
    engine.fail = "Cursor said no"
    body = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}).json()
    assert "Cursor said no" in body["choices"][0]["message"]["content"]


def test_split_model():
    assert split_model("composer-2.5@repo") == ("composer-2.5", "repo")
    assert split_model("cursor/gpt-5") == ("gpt-5", "")
    assert split_model("cursor") == ("", "")


def test_messages_need_a_user_turn():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        messages_to_request([{"role": "system", "content": "x"}])
    task, context, _system, images = messages_to_request(
        [{"role": "user", "content": [{"type": "text", "text": "look"},
                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]}]
    )
    assert task == "look" and context == "" and images[0]["mime"] == "image/png"
