# Running the AutoGPT Platform locally

This guide gets the full platform — frontend, backend, and all supporting
services — running on your own machine. Pick the setup that fits what you're doing:

- **A. Full Docker stack** — everything runs in containers. The simplest option,
  and the best way to take the whole app for a quick end-to-end spin.
- **B. Core-in-Docker + app native** — the infrastructure (Postgres/Supabase,
  Redis, RabbitMQ, ClamAV) runs in Docker while the backend and frontend run
  directly on your machine with hot-reload. Best for active development.

Run every command from the `autogpt_platform/` directory unless a step says otherwise.

> **Local vs. deployed:** locally, the stack runs graph executions on **RabbitMQ**.
> A Render deployment runs them on Render Workflows instead
> (`EXECUTION_BACKEND=workflows`) — a deploy-only concern covered by `render.yaml`
> and the root [`README.md`](README.md). You don't need either of those to develop
> locally.

---

## Prerequisites

- **Docker** + **Docker Compose v2** (bundled with Docker Desktop)
- For option B also: **Python 3.13** + **Poetry**, **Node 24** + **pnpm**
  (`corepack enable && corepack prepare pnpm@latest --activate`)

---

## Environment files

`make init-env` creates **three** `.env` files from their `.env.default` templates — one
per part of the stack. They are **not interchangeable**: each is loaded by a different set
of services, so a key placed in the wrong file is silently ignored by the service that
needs it (this is the usual cause of "I set my key but the feature still says no key").

| File | Loaded by | Put here |
|------|-----------|----------|
| `backend/.env` | **every backend service** — `rest_server`, `executor`, `copilot_executor`, `scheduler_server`, `database_manager`, `websocket_server` (they share one `env_file`) | LLM/provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPEN_ROUTER_API_KEY`), `CHAT_*`, `EXECUTION_BACKEND`, DB/Redis/RabbitMQ overrides |
| `frontend/.env` | the `frontend` container only | `NEXT_PUBLIC_*` browser vars — **never secrets**, these are inlined into the client bundle |
| `.env` (root) | Docker Compose's own `${VAR}` interpolation + the Supabase stack (GoTrue / db / kong) | Supabase/auth infra values — **not** app or LLM keys |

A key needed by services in more than one group must be added to **each** file that serves
those services — they are separate files, not one shared file. (For example, a provider key
used by both a backend block and a frontend feature goes in `backend/.env` *and*
`frontend/.env`.) Most LLM/copilot keys are backend-only and belong in `backend/.env`.

> **After editing any `.env`, recreate the affected containers** — `env_file` is read when a
> container is *created*, not on restart, so a plain `docker compose restart` keeps the old
> values:
>
> ```bash
> docker compose up -d --force-recreate \
>   rest_server copilot_executor scheduler_server executor database_manager websocket_server
> ```
>
> In core-in-Docker mode (option B) the app runs natively — just restart `make run-backend`.

---

## A. Full Docker stack

```bash
cd autogpt_platform

# 1. Create the env files (root + backend + frontend) from their defaults.
make init-env
#    …or, root only:  cp .env.default .env

# 2. Start everything (Supabase, Redis, RabbitMQ, ClamAV, backend services,
#    frontend). The first run builds images and can take several minutes.
docker compose up -d

# 3. Watch the services come up until they report healthy.
docker compose ps
docker compose logs -f rest_server frontend
```

Once everything is healthy, open **http://localhost:3000** for the frontend. The
REST API is on **http://localhost:8006** (visit `/docs` for the Swagger UI).

Stop / clean up:

```bash
docker compose stop        # stop, keep containers + volumes
docker compose down        # remove containers + networks
docker compose down -v     # also drop volumes (wipes local DB/Redis data)
```

---

## B. Core-in-Docker + app native (hot-reload)

```bash
cd autogpt_platform

# 1. Env files for root + backend + frontend.
make init-env

# 2. Bring up just the infrastructure (Postgres/Supabase, Redis, RabbitMQ).
make start-core
make logs-core            # follow infra logs (optional)

# 3. Backend: install deps, run DB migrations, generate the Prisma client.
cd backend
poetry install
cd ..
make migrate              # prisma migrate deploy + generate + gen-prisma-stub

# 4. Run the backend (all in-process services: REST, ws, executor, scheduler…).
make run-backend          # = cd backend && poetry run app

# 5. In a second terminal, run the frontend dev server.
make run-frontend         # = cd frontend && pnpm dev
```

Frontend: **http://localhost:3000** · Backend REST: **http://localhost:8006**.

To run a single backend service instead of the whole app (`poetry run app`),
use its entry point from `backend/`: `poetry run rest`, `poetry run ws`,
`poetry run executor`, `poetry run scheduler`, `poetry run db`.

---

## LLM / copilot keys

Copilot chat and the AI blocks need an LLM credential. Locally these keys go in
`autogpt_platform/backend/.env` — **not** the root `.env`; the copilot service
(`copilot_executor`) and the executor only load `backend/.env` (see
[Environment files](#environment-files) above). `.env.default` already lists
`ANTHROPIC_API_KEY=` and `OPENAI_API_KEY=`. Everything else runs without them — those
features just return nothing until a key is present. **Recreate the backend containers
after editing** (`docker compose up -d --force-recreate copilot_executor rest_server executor`)
so the new values are picked up.

Copilot chat defaults to the **OpenRouter** transport (`CHAT_USE_OPENROUTER=true`). Pick
one of these recipes in `autogpt_platform/backend/.env`:

```bash
# autogpt_platform/backend/.env

# --- Option A: OpenRouter (default transport, one key) ---
OPEN_ROUTER_API_KEY=sk-or-...     # copilot chat + OpenRouter-routed blocks

# --- Option B: direct Anthropic (no OpenRouter) ---
# Copilot uses the Claude Agent SDK path, which requires CHAT_API_KEY to be
# set as well — ANTHROPIC_API_KEY alone raises "No API key configured".
ANTHROPIC_API_KEY=sk-ant-...      # SDK subprocess + Claude AI blocks
CHAT_API_KEY=sk-ant-...           # same value — clears the copilot key check
CHAT_USE_OPENROUTER=false         # route copilot straight to api.anthropic.com

# --- Independent of the copilot transport above ---
OPENAI_API_KEY=sk-...             # OpenAI-based blocks
```

Under Option B, leaving `CHAT_USE_OPENROUTER` at its default `true` would send your
Anthropic key to OpenRouter and 401 — you must set it to `false`. See the root
[`README.md`](README.md) for the full transport table and the Render (deployed) wiring.

---

## Optional — run the Render Workflows execution path locally

By default local dev executes graphs on **RabbitMQ** (simplest — nothing extra to
install). If you want to exercise the same path a Render deploy uses (graph execution
on Render Workflows), run the Workflows task server locally. You do **not** need a
`RENDER_API_KEY` — the SDK routes to the local server.

Prereqs: [Render CLI](https://render.com/docs/cli) 2.11.0+ (`brew install render`).

Set these in your **gitignored `autogpt_platform/backend/.env`** (your personal
override) — **not** `.env.default`, which is committed and keeps the template's shared
default of `rabbitmq` (the simplest, no-extra-tooling path for anyone cloning the repo):

```bash
# autogpt_platform/backend/.env — switch the backend to the Workflows path:
EXECUTION_BACKEND=workflows
RENDER_USE_LOCAL_DEV=true          # SDK targets the local task server (no API key)
RENDER_WORKFLOW_SLUG=local         # any non-empty value works locally
```

> **Use option B (core-in-Docker + native app) for this.** The task server runs on your
> host at `localhost:8120`, so the app must also run on the host to reach it and to share
> the same DB/Redis. In full-Docker mode (option A) the containers would need
> `RENDER_LOCAL_DEV_URL=http://host.docker.internal:8120` *and* the host task server can't
> resolve the Dockerised `db`/`redis` hostnames — not worth the rewiring. A run that falls
> back to RabbitMQ leaves `renderRunId` empty (see the verify step below).

```bash
# Terminal 1 (from autogpt_platform/backend): start the local Workflows task server.
render workflows dev -- poetry run python -m backend.workflows.main

# Terminal 2: run the backend + frontend natively (make run-backend / make run-frontend).
```

Local runs/results are held in memory and lost when the task server stops (see the
[Workflows local-dev docs](https://render.com/docs/workflows-local-development)). To go
back to the default, unset these vars (or set `EXECUTION_BACKEND=rabbitmq`).

### Verify it — run a graph through Workflows

With the task server running (Terminal 1) and the backend on the Workflows path:

1. **Build a graph.** Open http://localhost:3000/build, add one credential-free block
   (e.g. **Calculator** or **Store Value**), set an input, and **Save**.
2. **Run it.** Click **Run**. The run shows up under `/library/agents/<id>`.
3. **Get the ids and confirm it used Workflows (not RabbitMQ).** Terminal 1 streams the
   `run_graph_execution` logs and the backend prints `Dispatched graph execution
   <graph_exec_id> to Render Workflows run_id=<run_id>`. The definitive check is the
   `renderRunId` column — populated **only** on the Workflows path. Query the latest run
   (from `autogpt_platform/`):

   ```bash
   docker compose exec -T db psql -U postgres -d postgres -c \
     'SELECT id AS graph_exec_id, "userId" AS user_id, "executionStatus", "renderRunId" \
      FROM platform."AgentGraphExecution" ORDER BY "createdAt" DESC LIMIT 1;'
   ```

   `graph_exec_id` and `user_id` are your two task arguments. A **non-empty `renderRunId`**
   confirms it dispatched to Workflows. An **empty `renderRunId`** means it ran on
   RabbitMQ — the backend isn't on the Workflows path; re-check the env vars above (and in
   full-Docker mode, that the containers were recreated to pick them up).
4. **Cross-check in the CLI (optional).** From `backend/`: `render workflows tasks list
   --local` → `run_graph_execution` → `runs` → your run → `results`. The **input** is
   `["<graph_exec_id>", "<user_id>"]` and the **result** is `{"status": "completed"}`.

Those two ids are exactly what a manual re-invocation of the task expects. Re-running by
hand only works within the entry's TTL — the producer deletes the Redis payload
(`wf:exec_entry:<graph_exec_id>`) once a run finishes.

---

## Everyday commands

```bash
# Formatting + linting (backend Black/isort/ruff + frontend prettier/eslint)
make format

# Backend tests (spins up a throwaway Postgres + Prisma)
cd backend && poetry run test
# …a single test:
cd backend && poetry run pytest backend/workflows/tasks_test.py -q

# Frontend tests
cd frontend && pnpm test:unit      # Vitest + RTL + MSW (integration, default)
cd frontend && pnpm test           # Playwright E2E
cd frontend && pnpm types          # type-check

# Seed local test data (users/agents) — requires the stack running
make test-data

# Reset the local database
make reset-db
```

---

## Ports

| Service               | URL / Port                      |
| --------------------- | ------------------------------- |
| Frontend              | http://localhost:3000           |
| REST API              | http://localhost:8006 (`/docs`) |
| WebSocket server      | ws://localhost:8001             |
| Supabase (Kong gateway)| http://localhost:8000          |
| Postgres              | localhost:5432                  |
| RabbitMQ (AMQP)       | localhost:5672                  |

(Ports are defined in `docker-compose.platform.yml`; confirm there if you've
changed the defaults.)

---

## Troubleshooting

- **A service won't start:** `docker compose logs -f <service>` (e.g.
  `rest_server`, `executor`, `db`). Missing env vars and DB-not-ready are the
  usual causes.
- **DB schema out of date / Prisma errors:** re-run `make migrate`.
- **Port already in use:** stop whatever is using the port, or change the
  published ports in `docker-compose.platform.yml` (see the Ports table above).
- **Rebuild one service after code changes (full-stack mode):**
  `docker compose build <service> && docker compose up -d --no-deps <service>`.
- **Clean slate:** `docker compose down -v` then start again (wipes local data).
