# AGENT.md — Orientation for the next agent

> This is a **handoff/orientation** doc for an agent picking up the AutoGPT Platform →
> Render deploy-template migration. It is distinct from `AGENTS.md` (the repo's general
> contribution guide) — read that too. Design context lives in `arch.md`, `plan.md`, and
> `remaining.md` at the repo root.

## What this branch is

Branch `feat/render-template-deploy` migrates the AutoGPT Platform off managed
Supabase / RabbitMQ / Redis-cluster onto Render-native services, packaged as a one-click
Render deploy template (`render.yaml` + `README.md` + `.env.example` at repo root).

The work was split into six streams (A–F) run as parallel agents in dependency waves:
**Wave 1** A (data) + D (ClamAV) → **Wave 2** B (auth) + C (executor) → **Wave 3** E
(backend services) → **Wave 4** F (frontend + blueprint). All six are complete and
committed; the blueprint live-validates (`render blueprints validate` → valid).

## Repo layout (what matters for this migration)

```
autogpt_platform/
  backend/                 FastAPI backend (Python, poetry)
    backend/data/redis_client.py   Redis: standalone path behind REDIS_CLUSTER_MODE
    backend/util/cache.py          @cached shared-cache; standalone scan path
    backend/util/settings.py       new: redis_cluster_mode, execution_backend,
                                   render_workflow_slug, render_api_key
    backend/util/feature_flag.py   auth-admin call replaced with platform.User lookup
    backend/executor/engine.py     NEW — broker-agnostic engine (extracted verbatim)
    backend/executor/manager.py    now RabbitMQ-only; re-exports engine symbols
    backend/executor/utils.py      add/stop_graph_execution branch on EXECUTION_BACKEND
    backend/workflows/             NEW — Render Workflows executor path (client, tasks,
                                   cancel, entry_store, rate_limit, main)
    migrations/20260721120000_add_render_run_id/   renderRunId column
    schema.prisma                  renderRunId on AgentGraphExecution
    Dockerfile                     installs render_sdk --no-deps in the server stage
  frontend/                Next.js (Node 24, pnpm 10.20.0)
    next.config.mjs        /auth/v1 rewrite to GoTrue, images allow-list, CORS headers
render.yaml                THE blueprint — 10 resources, region oregon
README.md                  template README (Deploy button, secrets table, "using the app")
.env.example               deployer-supplied secrets, documented
render.fragments/          SUPERSEDED per-stream fragments (folded into render.yaml);
                           safe to delete — kept only as provenance
arch.md / plan.md / remaining.md   design + handoff docs (internal — prune before publish)
```

## The 10 blueprint resources (region oregon, Project autogpt-platform)

`autogpt-platform-db` (Postgres 18) · `autogpt-platform-keyvalue` (Redis/Valkey,
noeviction) · `autogpt-platform-mv-refresh` (cron, replaces pg_cron) · `clamav` (pserv,
image `clamav/clamav-debian:1.4.5`) · `autogpt-platform-gotrue` (pserv, GoTrue auth) ·
`rest-server` (web) · `websocket-server` (web) · `scheduler-server` (pserv) ·
`database-manager` (pserv) · `frontend` (node web). The **executor Workflow is NOT in
render.yaml** — it is created manually in the Dashboard (see workflow.md).

## Cross-stream invariants — DO NOT break these

1. **Schema wrap**: `DATABASE_URL`/`DIRECT_URL` must be wrapped with `?schema=platform`
   in each backend service's start command (Render can't interpolate env in YAML).
   **Exception:** GoTrue owns the `auth` schema and must NOT get the wrapper.
2. **Redis**: `REDIS_CLUSTER_MODE=false`, `REDIS_PASSWORD=""`; NEVER set
   `REDIS_CLUSTER_HOST`/`REDIS_CLUSTER_PORT` (they win via AliasChoices and mispoint the
   standalone client). The cluster code path is preserved and default (`true`) for local.
3. **Migrations**: `rest-server` is the SOLE owner of `prisma migrate deploy` (its
   preDeploy). No other service may run it (races the `_prisma_migrations` lock).
4. **EXECUTION_BACKEND** flag defaults to `rabbitmq` (existing behavior). The Workflows
   path is additive; retries are DISABLED there because `charge_usage` isn't idempotent.
5. **ENCRYPTION_KEY** must be identical across rest-server, database-manager, and the
   manual Workflow, and must stay stable (stored creds become undecryptable otherwise).
6. All resources must stay in ONE region + workspace (private DNS requirement).

## Verification state

- Backend: Redis tests (10) + feature_flag tests (26) pass; ruff/isort/black/pyright clean.
  Full pytest suite needs the docker Postgres/Prisma harness (not run in agent envs).
- Frontend: `pnpm types` / `pnpm lint` / `pnpm test:unit` (3803) all pass on Node 24.
- Blueprint: `render blueprints validate` → valid (CLI v2.20.0).

## What's left — see workflow.md
