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

Copilot chat and the AI blocks need an LLM credential. Locally, set the keys in
`autogpt_platform/backend/.env` (`.env.default` already lists `ANTHROPIC_API_KEY=`
and `OPENAI_API_KEY=`). Everything else runs without them — those features just
return nothing until a key is present.

```bash
# autogpt_platform/backend/.env
ANTHROPIC_API_KEY=sk-ant-...      # direct copilot + Claude AI blocks
OPENAI_API_KEY=sk-...             # OpenAI blocks + OpenRouter fallback
OPEN_ROUTER_API_KEY=sk-or-...     # default copilot transport (optional)
# CHAT_USE_OPENROUTER=false       # route copilot direct to Anthropic instead
```

Copilot chat defaults to the OpenRouter transport (`CHAT_USE_OPENROUTER=true`); set
it to `false` with `ANTHROPIC_API_KEY` (or `CHAT_DIRECT_ANTHROPIC_API_KEY`) to talk to
`api.anthropic.com` directly. See the root [`README.md`](README.md) for the full
transport table and the Render (deployed) wiring.

---

## Optional — run the Render Workflows execution path locally

By default local dev executes graphs on **RabbitMQ** (simplest — nothing extra to
install). If you want to exercise the same path a Render deploy uses (graph execution
on Render Workflows), run the Workflows task server locally. You do **not** need a
`RENDER_API_KEY` — the SDK routes to the local server.

Prereqs: [Render CLI](https://render.com/docs/cli) 2.11.0+ (`brew install render`).

```bash
# In autogpt_platform/backend/.env — switch the backend to the Workflows path:
EXECUTION_BACKEND=workflows
RENDER_USE_LOCAL_DEV=true          # SDK targets the local task server (no API key)
RENDER_WORKFLOW_SLUG=local         # any non-empty value works locally
```

```bash
# Terminal 1 (from autogpt_platform/backend): start the local Workflows task server.
render workflows dev -- poetry run python -m backend.workflows.main

# Terminal 2: run the backend + frontend as usual (make run-backend / make run-frontend).
```

Local runs/results are held in memory and lost when the task server stops (see the
[Workflows local-dev docs](https://render.com/docs/workflows-local-development)). To go
back to the default, unset these vars (or set `EXECUTION_BACKEND=rabbitmq`).

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
