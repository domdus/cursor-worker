# Cursor Worker

A headless Cursor agent in a Docker container. You give it a task, it works in a workspace folder (files, shell, git, MCP), and returns the result, the changed files and any commits.

It has three ways in, all on one port and protected by one token:

| Endpoint | For | What it is |
|---|---|---|
| `/mcp` | Hermes, Claude Code, Open WebUI tools, AI Workspace, any MCP client | MCP server (Streamable HTTP) with a `cursor_agent` tool |
| `/v1` | Open WebUI, any OpenAI client | OpenAI-compatible `chat/completions` and `models`, so Cursor shows up as a chat model |
| `/jobs` | Scripts, CI, your own apps | Start a job, check it, stream its events, cancel it |

It runs on the official [Cursor SDK](https://cursor.com/docs) with your own `CURSOR_API_KEY`. Usage is billed to that Cursor account.

## Start

```bash
cp .env.example .env
# paste CURSOR_API_KEY, then:
echo "WORKER_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d
curl http://127.0.0.1:8787/health
```

The compose file publishes the port on `127.0.0.1` only. To reach the worker from other machines, change it to `"8787:8787"`. Every request except `/health` needs `Authorization: Bearer $WORKER_TOKEN`.

## Connect a client

### Claude Code

```bash
claude mcp add --transport http cursor-worker http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer $WORKER_TOKEN"
```

### Hermes Agent

Add the worker as an HTTP MCP server in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  cursor-worker:
    url: http://127.0.0.1:8787/mcp
    headers:
      Authorization: "Bearer <WORKER_TOKEN>"
```

Hermes then has `cursor_agent`, `cursor_job`, `cursor_cancel` and `cursor_workspaces` as tools.

### Open WebUI

There are two ways to connect it, and you can use both:

- **As a model:** Admin Settings → Connections → OpenAI API. URL `http://cursor-worker:8787/v1` (or `http://host.docker.internal:8787/v1`), API key = `WORKER_TOKEN`. Cursor's models show up in the model picker. The agent's steps appear as "thinking", and the stop button cancels the job.
- **As a tool for other models:** Admin Settings → External Tools → add an MCP server (Streamable HTTP) at `.../mcp` with Bearer auth.

`docker compose --profile webui up -d` starts Open WebUI already connected to the worker.

Set `ENABLE_FORWARD_USER_INFO_HEADERS=true` on Open WebUI. It then sends the chat ID, so each chat keeps one Cursor agent. Without the chat ID, every message starts a new agent and gets the earlier turns pasted in. Also give Open WebUI a cheap **task model** (for titles and tags). Otherwise those small background requests also run on a Cursor agent.

### Home Assistant AI Workspace

Add an MCP connection with URL `http://<worker-host>:8787/mcp` and the token as Bearer. Any agent (OpenAI, Claude, Ollama) can then hand work to Cursor with `cursor_agent`.

## Model and workspace from the chat side

- `model` picks the Cursor model, for example `composer-2.5` or `gpt-5`. If it is empty, `CURSOR_MODEL` is used.
- On `/v1`, `model@workspace` picks the workspace too, for example `composer-2.5@my-repo`. You can also send the header `X-Cursor-Workspace`.
- `X-Cursor-Tools: offline` (on `/v1`) or `tools: "offline"` (MCP and jobs) narrows what the agent may use for that request.

## The MCP tool

`cursor_agent(task, workspace?, session?, model?, tools?, mcp_servers?, wait_seconds?)`

- Waits up to `wait_seconds` (default `MCP_WAIT_SECONDS`, 20) for the agent to finish, then returns `result`, `changes` (`files` from `git status`, new `commits`) and `usage`.
- If the task takes longer, it returns `status: "running"` and a `job_id`. The caller then uses `cursor_job(job_id, wait_seconds)` to wait for it, or `cursor_cancel(job_id)` to stop it.
- `session`: calls with the same session reuse the same agent, so follow-ups remember earlier turns.
- `mcp_servers`: extra HTTP/SSE MCP servers for this task, in `{"name": {"url": ..., "headers": {...}}}` format.

## Job API

```bash
H="Authorization: Bearer $WORKER_TOKEN"
# start (wait up to 30 s for the result; 202 + job if still running)
curl -s -X POST localhost:8787/jobs -H "$H" -H 'Content-Type: application/json' \
  -d '{"task":"Add a --json flag to cli.py and a test for it","workspace":"my-repo","wait":30}'
curl -s localhost:8787/jobs/<id>?wait=60 -H "$H"          # status / result
curl -N  localhost:8787/jobs/<id>/events -H "$H"           # SSE: status, tool, text, result, end
curl -s -X POST localhost:8787/jobs/<id>/cancel -H "$H"
curl -s localhost:8787/jobs -H "$H"                         # recent jobs
curl -s localhost:8787/workspaces -H "$H"
```

`POST /jobs` body: `task`, `workspace`, `session`, `model`, `effort`, `tools`, `mcp_servers`, `images` (base64 or data URI, at most 4), `instructions`, `wait` (0–600 s).

Jobs and their events are stored in `/data/worker.sqlite` and survive restarts. A job that was running during a restart is marked failed.

## Workspaces

Each workspace is a folder under `/data/workspaces/<name>`. A name a caller has not used before is created empty, unless you set `CREATE_WORKSPACES=false`. Only one job that can edit files runs in a workspace at a time; the rest wait in a queue. Jobs limited to `mcp` or `none` tools don't take that lock.

- Set `WORKSPACE_DIR=/workspace` and mount a host folder there to make it the default workspace. The container runs as uid 1000, so that folder must be writable for uid 1000.
- `GIT_REPO` clones a repository into the default workspace on the first start. Authentication uses `GIT_TOKEN` (HTTPS) or `GIT_SSH_KEY`. The agent's own `git pull`/`git push` use the same credentials, and the secret is never written to `.git/config`.

## Security

- **Token:** Without `WORKER_TOKEN`, the worker answers only when it listens on loopback and the `Host` header is loopback. Otherwise every request is refused.
- **`TOOL_PROFILE`** is the upper limit (`full` > `local` > `offline` > `mcp` > `none`). Requests can narrow it but never widen it.
- **The agent can do whatever its tools allow inside the container,** including running shell commands, without asking. Treat the token like a shell login on the container. Don't mount anything you wouldn't hand to a shell.
- **MCP servers sent with a request** must be HTTP/SSE. Stdio servers (which start processes) and `${ENV}` substitution are only allowed in `/data/mcp.json`. Set `ALLOW_REQUEST_MCP=false` to accept only that file.
- **Loops:** The worker adds `X-Cursor-Worker-Depth` to every HTTP MCP server it hands to an agent. If an agent calls a worker back through MCP, that call is refused (`MAX_CALL_DEPTH`, default 1).

## Configuration

See [.env.example](.env.example). The most important settings: `CURSOR_API_KEY`, `WORKER_TOKEN`, `CURSOR_MODEL`, `TOOL_PROFILE`, `MAX_CONCURRENT_JOBS` (2), `JOB_TIMEOUT_SECONDS` (1800), `MCP_WAIT_SECONDS` (20), `WORKSPACE_DIR`, `GIT_REPO`, `WORKER_INSTRUCTIONS`, `REWRITE_LOOPBACK_HOST`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m cursor_worker.app      # HOST=127.0.0.1 DATA_DIR=./data for a local run
```

The tests use a fake engine, so they need neither a Cursor key nor network access.
