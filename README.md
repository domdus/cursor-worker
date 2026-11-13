# Agent Worker

A headless coding agent in a Docker container: Cursor or Claude Code. You give it a task, it works in a workspace folder (files, shell, git, MCP), and returns the result, the changed files and any commits.

It has three ways in, all on one port and protected by one token:

| Endpoint | For | What it is |
|---|---|---|
| `/mcp` | Hermes, Claude Code, Open WebUI tools, AI Workspace, any MCP client | MCP server (Streamable HTTP) with an `agent_run` tool |
| `/v1` | Open WebUI, any OpenAI client | OpenAI-compatible `chat/completions` and `models`, so Cursor and Claude Code show up as chat models |
| `/jobs` | Scripts, CI, your own apps | Start a job, check it, stream its events, cancel it |

It runs on the official [Cursor SDK](https://cursor.com/docs) with your own `CURSOR_API_KEY`. Usage is billed to that Cursor account. It can also run **Claude Code** (Anthropic's [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/)) with your Claude plan or an Anthropic API key: see [Claude Code](#claude-code). Each job picks one engine.

## Installation

You need Docker with Compose, and a Cursor user API key from [cursor.com/dashboard/integrations](https://cursor.com/dashboard/integrations) and/or a Claude credential (see [Claude Code](#claude-code)). The image `ghcr.io/domdus/agent-worker` is built for amd64 and arm64.

### 1. Make a folder with two files

```bash
mkdir agent-worker && cd agent-worker
```

`.env` (keep it private, it holds both secrets):

```bash
CURSOR_API_KEY=key_...
WORKER_TOKEN=          # fill with: openssl rand -hex 32
CURSOR_MODEL=composer-2.5
```

`docker-compose.yml`:

```yaml
services:
  agent-worker:
    image: ghcr.io/domdus/agent-worker:latest   # or pin a version, e.g. :0.2.0
    container_name: agent-worker
    env_file: .env
    ports:
      - "127.0.0.1:8787:8787"     # this machine only; "8787:8787" opens it to your LAN
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - agent-worker-data:/data  # jobs, sessions, workspaces
      # A project on the host as the default workspace (writable for uid 1000),
      # plus WORKSPACE_DIR=/workspace in .env:
      # - /path/to/project:/workspace
      # SSH to your servers (see "SSH to your servers"):
      # - ./ssh:/run/secrets/ssh:ro
    restart: unless-stopped

volumes:
  agent-worker-data:
```

All other settings go into `.env`. [.env.example](.env.example) lists every one of them with a short explanation.

### 2. Start it and check it

```bash
docker compose up -d
curl http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/v1/models -H "Authorization: Bearer $(grep ^WORKER_TOKEN= .env | cut -d= -f2)"
```

`/health` answers without a token. Every other request needs `Authorization: Bearer <WORKER_TOKEN>`. `/v1/models` should list the models of your Cursor account. If it lists only `CURSOR_MODEL`, the worker could not reach Cursor with your API key, and `docker compose logs -f` shows why.

### 3. Connect a client

See [Connect a client](#connect-a-client) below.

### Update

```bash
docker compose pull && docker compose up -d
```

Jobs, sessions and workspaces live in the `agent-worker-data` volume and are kept.

### From the repository instead

The repository's [docker-compose.yml](docker-compose.yml) does the same, can build the image itself, and has an optional Open WebUI already connected (`--profile webui`):

```bash
git clone https://github.com/domdus/agent-worker.git && cd agent-worker
cp .env.example .env    # set CURSOR_API_KEY and WORKER_TOKEN
docker compose up -d                   # or: docker compose --profile webui up -d
```

## Connect a client

### Claude Code

```bash
claude mcp add --transport http agent-worker http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer $WORKER_TOKEN"
```

### Hermes Agent

Add the worker as an HTTP MCP server in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent-worker:
    url: http://127.0.0.1:8787/mcp
    headers:
      Authorization: "Bearer <WORKER_TOKEN>"
```

Hermes then has `agent_run`, `agent_job`, `agent_cancel` and `agent_workspaces` as tools.

Until 0.2.0 this project was **cursor-worker** with `cursor_*` tools. From 0.3.0 the tools are `agent_run`, `agent_job`, `agent_cancel`, `agent_approve` and `agent_workspaces`, the image is `ghcr.io/domdus/agent-worker`, and the headers are `X-Agent-Workspace`, `X-Agent-Tools` and `X-Agent-Session`. Update existing client configs.

### Open WebUI

There are two ways to connect it, and you can use both:

- **As a model:** Admin Settings → Connections → OpenAI API. URL `http://agent-worker:8787/v1` (or `http://host.docker.internal:8787/v1`), API key = `WORKER_TOKEN`. Cursor's models and the `claude-code/…` models show up in the model picker. The agent's steps appear as "thinking", and the stop button cancels the job.
- **As a tool for other models:** Admin Settings → External Tools → add an MCP server (Streamable HTTP) at `.../mcp` with Bearer auth.

`docker compose --profile webui up -d` starts Open WebUI already connected to the worker.

Set `ENABLE_FORWARD_USER_INFO_HEADERS=true` on Open WebUI. It then sends the chat ID, so each chat keeps one Cursor agent. Without the chat ID, every message starts a new agent and gets the earlier turns pasted in. Also give Open WebUI a cheap **task model** (for titles and tags). Otherwise those small background requests also run on a Cursor agent.

### Home Assistant AI Workspace

Add an MCP connection with URL `http://<worker-host>:8787/mcp` and the token as Bearer. Any agent (OpenAI, Claude, Ollama) can then hand work to Cursor with `agent_run`.

## Claude Code

Jobs can run on Claude Code instead of Cursor. The worker image includes Claude Code. It runs in the job's workspace with the same tool profiles, workspaces, sessions, MCP servers, SSH guard and approvals as Cursor. It loads no Claude Code settings, `CLAUDE.md`, skills or hooks from disk, and subagents are off.

### Credentials

One of these, the first one set wins:

| Where | What |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | A Claude Plan token (`sk-ant-oat…`) from `claude setup-token`. Uses your Claude subscription (Pro, Max). Lasts one year. |
| `ANTHROPIC_API_KEY` | An Anthropic API key from the [Claude Console](https://console.anthropic.com/settings/keys), billed as API credits. |
| Sign in with Claude | The worker gets a Plan token itself and keeps it in the data volume. No Claude Code on your computer, no restart. |

Only the chosen credential reaches Claude Code, so a plan job never falls back to API billing or the other way round.

### Sign in with Claude

```bash
docker exec -it agent-worker python -m agent_worker.claude_login
```

It prints a sign-in link. Open it on any device, sign in with the Claude account that has your plan, and paste the code Anthropic shows. The Plan token is saved in `/data/claude/oauth-token` (owner-only) and used from the next job on.

The same over HTTP, for scripts or a UI:

```bash
curl -s -X POST localhost:8787/auth/claude/login -H "$H"                  # -> {"id": ..., "url": ...}
curl -s -X POST localhost:8787/auth/claude/login/<id> -H "$H" \
  -H 'Content-Type: application/json' -d '{"code": "<code from Anthropic>"}'   # saves the token
curl -s -X DELETE localhost:8787/auth/claude/token -H "$H"                # sign out again
```

The token is never returned or logged. The sign-in runs Anthropic's own `claude setup-token` in a temporary folder that is deleted afterwards. A code works once: if it is refused or older than 15 minutes, start again. `GET /health` shows which credential is in use (`claude.credential`: `env-plan`, `env-api`, `signed-in` or `none`).

Anthropic's terms for the Agent SDK say third-party products should use API keys and not offer Claude.ai sign-in ([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview), [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)). Whether your own Plan token on your own worker is fine for you is your decision.

### Choosing the engine

- **Jobs:** `"engine": "claude"` (or `"cursor"`) in `POST /jobs`.
- **MCP:** the `engine` argument of `agent_run`.
- **`/v1`:** Claude models are listed as `claude-code/claude-sonnet-5-5` and so on, and `claude-code/<model>@workspace` works like the Cursor names. Cursor's own Claude models (for example `claude-4.5-sonnet`) still run on Cursor.
- Without one, `DEFAULT_ENGINE` is used, else Cursor, else Claude when only Claude is set up.

`CLAUDE_MODEL` (default `claude-sonnet-5-5`) is the model when a Claude job names none. The tool profiles mean the same as for Cursor: `full` has files, the shell and the web; `local` has no web search or fetch; `offline` has no shell either; `mcp` has only MCP tools; `none` is chat only. Effort `fast`, `high` and `high_fast` become Claude Code's low, high and medium effort. A `session` resumes the Claude Code conversation; its state is kept in `/data/claude/config`.

Claude Code makes the image about 500 MB larger.

## Model and workspace from the chat side

- `model` picks the model, for example `composer-2.5` or `gpt-5` for Cursor, or `claude-opus-5-5` for Claude Code. If it is empty, `CURSOR_MODEL` or `CLAUDE_MODEL` is used.
- On `/v1`, `model@workspace` picks the workspace too, for example `composer-2.5@my-repo`. You can also send the header `X-Agent-Workspace`.
- `X-Agent-Tools: offline` (on `/v1`) or `tools: "offline"` (MCP and jobs) narrows what the agent may use for that request.

## The MCP tool

`agent_run(task, workspace?, session?, model?, tools?, mcp_servers?, wait_seconds?, engine?)`

- Waits up to `wait_seconds` (default `MCP_WAIT_SECONDS`, 20) for the agent to finish, then returns `result`, `changes` (`files` from `git status`, new `commits`) and `usage`.
- If the task takes longer, it returns `status: "running"` and a `job_id`. The caller then uses `agent_job(job_id, wait_seconds)` to wait for it, or `agent_cancel(job_id)` to stop it.
- `status: "needs_approval"`: a command on an approval host is waiting for the user (see [Approval for selected hosts](#approval-for-selected-hosts)).
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
curl -s -X POST localhost:8787/jobs/<id>/approve -H "$H" -H 'Content-Type: application/json' \
  -d '{"approve": true}'                                    # answer a job in needs_approval
curl -s localhost:8787/jobs -H "$H"                         # recent jobs
curl -s localhost:8787/workspaces -H "$H"
```

`POST /jobs` body: `task`, `engine` (`cursor` or `claude`), `workspace`, `session`, `model`, `effort`, `tools`, `mcp_servers`, `images` (base64 or data URI, at most 4), `instructions`, `wait` (0–600 s).

Jobs and their events are stored in `/data/worker.sqlite` and survive restarts. A job that was running during a restart is marked failed.

## Workspaces

Each workspace is a folder under `/data/workspaces/<name>`. A name a caller has not used before is created empty, unless you set `CREATE_WORKSPACES=false`. Only one job that can edit files runs in a workspace at a time; the rest wait in a queue. Jobs limited to `mcp` or `none` tools don't take that lock.

- Set `WORKSPACE_DIR=/workspace` and mount a host folder there to make it the default workspace. The container runs as uid 1000, so that folder must be writable for uid 1000.
- `GIT_REPO` clones a repository into the default workspace on the first start. Authentication uses `GIT_TOKEN` (HTTPS) or `GIT_SSH_KEY`. The agent's own `git pull`/`git push` use the same credentials, and the secret is never written to `.git/config`.

## SSH to your servers

The agent uses plain `ssh`, `scp`, `sftp`, `rsync` and git over SSH. You set up access with environment variables. Each value is either the content itself or a path to a mounted file:

| Variable | What it is |
|---|---|
| `SSH_KEY` | One private key |
| `SSH_KEYS_DIR` | A folder of private keys. Each one ends up at `~/.ssh/keys/<file name>` |
| `SSH_CONFIG` | A normal ssh config: `Host` aliases, `HostName`, `User`, `Port`, `IdentityFile` |
| `SSH_KNOWN_HOSTS` | Your servers' host keys. With it, unknown or changed keys are refused (`StrictHostKeyChecking yes`). Without it, the first key seen is kept and a later change is refused (`accept-new`) |
| `SSH_APPROVAL_HOSTS` | Patterns such as `prod-*,db01,10.0.1.*`, matched against the alias and its `HostName`. On these hosts the job pauses before the first command |

```yaml
# docker-compose.yml
    volumes:
      - ./ssh:/run/secrets/ssh:ro
# .env
SSH_KEYS_DIR=/run/secrets/ssh/keys
SSH_CONFIG=/run/secrets/ssh/config
SSH_KNOWN_HOSTS=/run/secrets/ssh/known_hosts
SSH_APPROVAL_HOSTS=prod-*
```

```
# ./ssh/config
Host web01
  HostName 10.0.0.5
  User deploy
  IdentityFile ~/.ssh/keys/web
Host prod-db
  HostName 10.0.9.9
  User root
  IdentityFile ~/.ssh/keys/prod
```

At startup the worker writes `~/.ssh` from these variables. Your config comes first. After it come defaults for every host: `BatchMode yes` (never a password prompt), a connect timeout, keep-alives, and connection reuse. Commands to the same host, user and port share one connection for 60 s after the last one ends, so a loop of quick commands doesn't log in each time. Every command still goes through the guard. Set `ControlMaster no` in your `SSH_CONFIG` to turn reuse off. `GIT_SSH_KEY` is added as one more key, so git over SSH works in every workspace. If you mount a read-only `~/.ssh` of your own, it is used as it is.

Use a key made for the worker, not your personal one. Ideally use one per server or group of servers, for a user that can do just what the worker needs, so you can revoke it alone.

### Every command is in the job

Before the real program runs, `ssh`, `scp` and `sftp` pass through a guard. `rsync` and git call `ssh`, so they pass through it too. The host, user and command show up as a `tool` event in the job. You can read them in `/jobs/<id>/events`, in Open WebUI's thinking stream, and in the MCP progress messages.

### Approval for selected hosts

When the agent first runs a command on a host in `SSH_APPROVAL_HOSTS`, the job switches to `needs_approval`. The command waits until a human answers or `SSH_APPROVAL_TIMEOUT` (900 s) runs out. Once a host is approved, it stays approved for the rest of that job. A denial or a timeout makes the command fail, and the agent is told to report that instead of trying again.

Where the question shows up:

- **MCP clients that support elicitation** (for example Claude Code) ask the user directly in their own UI. The answer comes from the person, not from the model that called the tool.
- **Other MCP clients (Hermes, Bifrost)** get `status: "needs_approval"` with the host and command, and are told to ask the user. The answer goes to `POST /jobs/<id>/approve`. If you set `APPROVAL_TOOL=true`, there is also a `agent_approve` tool for passing on the user's answer. It's off by default, because the calling model could otherwise approve on its own.
- **Open WebUI and other `/v1` chats:** the reply ends with the command and the question. Reply **approve** or **deny** in the same chat, and the same job continues.
- **Job API:** check with `GET /jobs/<id>` and answer with `POST /jobs/<id>/approve`.

The guard sees what the agent runs with the normal commands. It is a guardrail for an agent that follows its instructions, not a sandbox: a process that calls `/usr/bin/ssh` directly bypasses it. Hard limits belong on the server, for example a separate user, or `command=` and `from=` in `authorized_keys`.

## Security

- **Token:** Without `WORKER_TOKEN`, the worker answers only when it listens on loopback and the `Host` header is loopback. Otherwise every request is refused.
- **`TOOL_PROFILE`** is the upper limit (`full` > `local` > `offline` > `mcp` > `none`). Requests can narrow it but never widen it.
- **The agent can do whatever its tools allow inside the container,** including running shell commands, without asking. Treat the token like a shell login on the container. Don't mount anything you wouldn't hand to a shell.
- **MCP servers sent with a request** must be HTTP/SSE. Stdio servers (which start processes) and `${ENV}` substitution are only allowed in `/data/mcp.json`. Set `ALLOW_REQUEST_MCP=false` to accept only that file.
- **SSH:** The guard listens only to callers inside the container. The worker's keys never leave the container, and requests cannot send SSH keys or config. See [SSH to your servers](#ssh-to-your-servers) for what approvals do and don't cover.
- **Loops:** The worker adds `X-Agent-Worker-Depth` to every HTTP MCP server it hands to an agent. If an agent calls a worker back through MCP, that call is refused (`MAX_CALL_DEPTH`, default 1).

## Configuration

See [.env.example](.env.example). The most important settings: `CURSOR_API_KEY`, `WORKER_TOKEN`, `CURSOR_MODEL`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `DEFAULT_ENGINE`, `TOOL_PROFILE`, `MAX_CONCURRENT_JOBS` (2), `JOB_TIMEOUT_SECONDS` (1800), `MCP_WAIT_SECONDS` (20), `WORKSPACE_DIR`, `GIT_REPO`, `SSH_KEY`, `SSH_KEYS_DIR`, `SSH_CONFIG`, `SSH_KNOWN_HOSTS`, `SSH_APPROVAL_HOSTS`, `SSH_APPROVAL_TIMEOUT`, `APPROVAL_TOOL`, `WORKER_INSTRUCTIONS`, `REWRITE_LOOPBACK_HOST`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m agent_worker.app      # HOST=127.0.0.1 DATA_DIR=./data for a local run
```

The tests use a fake engine, so they need neither a Cursor key nor network access.
