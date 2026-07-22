# Remaining Work — AutoGPT Platform on Render

Handoff for the next agents. Full context in [`plan.md`](plan.md); target arch in
[`arch.md`](arch.md). This file is the actionable to-do list: what's decided, what still
needs a human call, and the per-stream implementation tasks.

## Decisions already made (do not relitigate)

- **Auth → run the self-hosted GoTrue container on Render** (not a backend rewrite). Frontend
  is a URL re-point only.
- **Redis → add a standalone client path behind `REDIS_CLUSTER_MODE`** (default `true`;
  Render sets `false`). Keep managed Render Key Value; keep the cluster path intact.
- **Executor Workflow → created manually in the Render Dashboard**, not in `render.yaml`.
  Blueprint services reference it via env vars.

## Still needs a human decision (blocks the owning stream)

| # | Question | Owner stream | Blocks |
|---|----------|--------------|--------|
| 1 | Existing prod user base to migrate, or fresh template deploy? (Sets whether Stream B Phase 3 is a hard `auth.users` UUID migration or a no-op.) | B | B cutover |
| 2 | Postgres major version — confirm **16** matches current Supabase (immutable after create). | A | A provision |
| 3 | Media storage: **GCS** (`MEDIA_GCS_BUCKET_NAME` + creds) vs local disk (disk kills horizontal scaling / zero-downtime). | E | E scaling design |
| 4 | Triage the extra compose services: `database_manager`, `copilot_executor`, `notification_server`, `platform_linking_manager`, `falkordb`. Scheduler + copilot depend on the first two — are they in scope for the template? | E/C | E, scheduler, copilot |
| 5 | SMTP provider + Google OAuth app for GoTrue (Render has no managed email). | B | B email/OAuth flows |
| 6 | ClamAV instance plan (~4 GB RAM) + persistent-disk sizing; pinned image tag/version. | D | D provision |
| 7 | Concurrency ceiling for Workflows (base 20–100 runs; purchased 200–300) — enough for peak? | C | C sizing |

---

## Stream A — Data layer (Postgres + Key Value)  `feat/render-data-layer`
**Blocks B, C, E, F. Start first.**

Redis standalone path (RESOLVED approach — implement):
- [ ] Add `REDIS_CLUSTER_MODE` setting (default `true`) in `backend/util/settings.py`.
- [ ] `backend/data/redis_client.py`: build plain `Redis`/`AsyncRedis` in
      `connect()`/`connect_async()` when standalone; `resolve_shard_for_channel()` returns
      `(HOST, PORT)` directly (skip `get_node_from_key`).
- [ ] `backend/util/cache.py`: `_get_redis()` builds plain `Redis` when standalone; drop
      `target_nodes=RedisCluster.PRIMARIES` from both `scan_iter` calls.
- [ ] Verify `backend/copilot/rate_limit.py` + `pending_messages.py` work standalone (they
      only catch `RedisClusterException` / use `execute_command("SPUBLISH", …)`).
- [ ] Tests: cover standalone connect + pub/sub round-trip + cache scan/invalidate.
- [ ] Audit TTLs on lock/queue/pending-turn/rate-limit keys → confirm `noeviction` is safe.

Postgres:
- [ ] Declare `db` (Postgres 16, `ipAllowList: []`) in the `render.yaml` fragment.
- [ ] Wire `DATABASE_URL` + `DIRECT_URL` from `fromDatabase`; append `?schema=platform` via a
      wrapper var. Keep `DB_CONNECTION_LIMIT` budgeted across all processes × instances.
- [ ] Confirm `CREATE EXTENSION vector` + `pg_trgm` succeed as the app role; **verify
      unqualified `::vector` casts / `<=>` resolve** (search_path check).
- [ ] Replace `pg_cron` (not on Render): move the store materialized-view refresh to a Render
      Cron Job or the in-app scheduler.
- [ ] Decide migration owner: `prisma migrate deploy` (via `DIRECT_URL`) as **one** service's
      predeploy — coordinate with Stream E so rest/executor/scheduler don't race.

Key Value:
- [ ] Declare `keyvalue` (`maxmemoryPolicy: noeviction`, paid/disk-backed, `ipAllowList: []`).
- [ ] Wire `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD` via `fromService`; set
      `REDIS_CLUSTER_MODE=false`. Override `.env.default` port `17000` → `6379`.

Deliverable: migrations run clean on fresh Render Postgres; standalone Redis client connects.

---

## Stream B — Auth: GoTrue on Render  `feat/render-auth`
**Depends on A.**

- [ ] Deploy GoTrue (`supabase/gotrue:v2.170.0` from `db/docker/docker-compose.yml`) as a
      Render service backed by the Stream-A Postgres (owns the `auth` schema; dedicated
      `supabase_auth_admin` role via `GOTRUE_DB_DATABASE_URL`).
- [ ] Phase 0: stand up in non-prod, confirm the existing frontend clients authenticate and
      the backend verifies tokens end-to-end. Decide HS256 vs ES256 signing.
- [ ] Env-ize all URLs/secrets: `JWT_VERIFY_KEY`/`GOTRUE_JWT_SECRET` (env-group
      `generateValue` or ES256 keypair), `SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_URL`/
      `SUPABASE_PUBLIC_URL`, `SUPABASE_SERVICE_ROLE_KEY`/`NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- [ ] Configure SMTP (`GOTRUE_SMTP_*`) + Google OAuth (`GOTRUE_EXTERNAL_GOOGLE_*`,
      `GOTRUE_SITE_URL`, `GOTRUE_URI_ALLOW_LIST`) for the new domain.
- [ ] Sever runtime admin coupling: replace `feature_flag.py`'s `auth.admin.get_user_by_id`
      with a direct GoTrue admin call or a `platform.User` lookup.
- [ ] Kong decision: keep it as the `/auth/v1` front door, or expose GoTrue directly.
- [ ] Preserve admin designation (`role:"admin"`) — confirm how admins are flagged today.
- [ ] If migrating users (decision #1): export `auth.users` + identities + refresh tokens,
      **preserving `id` UUIDs** (every FK across ~40 `User` relations depends on it).
- [ ] Cutover: forced re-login; verify OAuth callback, email verify, password reset,
      WebSocket token, admin impersonation.

Deliverable: register / log in / call an authed endpoint end-to-end, no managed Supabase.

---

## Stream C — RabbitMQ → Render Workflows  `feat/render-workflows-executor`
**Depends on A; coordinates with B.**

- [ ] Spike: install `render_sdk`, scaffold a throwaway Workflow, confirm
      `start_task`/`run_task`/`cancel_task_run` + a representative long graph run; validate
      the 4 MB payload path and 24h behavior.
- [ ] Refactor `backend/executor/manager.py` so the engine (`ExecutionProcessor.on_graph_execution`)
      is import-clean and free of RabbitMQ/`ExecutionManager` coupling (pure refactor).
- [ ] Add `backend/workflows/` with `run_graph_execution` (+ cancel/idempotency guard)
      wrapping the extracted engine. Add `EXECUTION_BACKEND=rabbitmq|workflows` flag;
      `add_graph_execution`/`stop_graph_execution` branch on it.
- [ ] Reimplement per-user rate limiting app-side (Redis counter) — no native equivalent.
- [ ] Persist `render_run_id` on `AgentGraphExecution` (new column + migration) for cancel.
- [ ] Confirm cancellation is a **cooperative** signal (engine flips DB → `TERMINATED`, cleans
      up reviews, cascades to children); if it's a hard kill, add a polled Redis cancel flag.
- [ ] Decide payload strategy: pass `graph_exec_id` only + reload from DB if `GraphExecutionEntry`
      exceeds 4 MB.
- [ ] Convert nested sub-graph runs (`blocks/agent.py`) to subtasks; point scheduler at
      `start_task` (or a cron job).
- [ ] Set `Retry(...)` conservatively (each run touches billing/DB — confirm charging is
      idempotent under re-run). Pick the Workflow plan.
- [ ] **Manual Dashboard step (orchestrator):** create the Workflow, Root `backend/workflows/`,
      Start `python main.py`; wire its slug + `RENDER_API_KEY` into producer services.
- [ ] Note: broker not fully removable until copilot-executor + notifications also migrate.

Deliverable: a graph run dispatched via Workflows executes and reports back, no RabbitMQ.

---

## Stream D — ClamAV private service  `feat/render-clamav`
**Independent — can start immediately.**

- [ ] Declare `clamav` as `type: pserv`, `runtime: image`, `clamav/clamav-debian:<pinned-tag>`,
      port 3310, `CLAMD_CONF_TCPAddr=0.0.0.0`.
- [ ] Attach a ~2 GB persistent disk at `/var/lib/clamav` (freshclam definitions survive
      restarts; single-instance tradeoff is fine).
- [ ] Size ≥4 GB RAM; consider lowering `MaxThreads` from 12.
- [ ] Wire backend `CLAMAV_SERVICE_HOST`/`CLAMAV_SERVICE_PORT` via `fromService` (no code
      change). Keep `CLAMAV_SERVICE_ENABLED=true`, `CLAMAV_MARK_FAILED_SCANS_AS_CLEAN=false`.
- [ ] Confirm outbound HTTPS egress for freshclam; pick a health check (TCP/`PING`).

Deliverable: backend reaches ClamAV over private DNS; a test upload gets scanned.

---

## Stream E — Backend services: rest / websocket / scheduler  `feat/render-backend-services`
**Depends on A, B, C.**

All three from `backend/Dockerfile`, `dockerContext: .` (repo root), one image, differ by
`dockerCommand`.

rest_server (public web):
- [ ] `dockerCommand: /bin/sh -c 'exec env AGENT_API_PORT=$PORT rest'`; `healthCheckPath: /health`.
- [ ] Own `prisma migrate deploy` as `preDeployCommand` (coordinate with A).
- [ ] Full env set: Postgres, Redis (`REDIS_CLUSTER_MODE=false`), Workflows (`start_task` +
      `RENDER_API_KEY` + slug), ClamAV, auth/JWT (`JWT_VERIFY_KEY` hard-fails if empty),
      core secrets (`ENCRYPTION_KEY` **must stay stable**), URLs/CORS, integrations.

websocket_server (public web, WSS):
- [ ] `dockerCommand: ws`; `WEBSOCKET_SERVER_PORT=$PORT`; `/health`.
- [ ] Env: Postgres, Redis, `ENABLE_AUTH=true` + JWT, `BACKEND_CORS_ALLOW_ORIGINS`.

scheduler_server (private `pserv`):
- [ ] `dockerCommand: scheduler`; `PYRO_HOST=0.0.0.0`, `EXECUTION_SCHEDULER_PORT=$PORT`;
      `healthCheckPath: /health_check`; **`numInstances: 1`** (no leader election).
- [ ] Env: **`DIRECT_URL` required** (jobstore), `DATABASE_URL`, Redis, `DATABASE_MANAGER_HOST`
      (if in scope — decision #4), Workflows creds. Allow long health-check grace (startup
      embedding backfill).

Deliverable: three services boot clean against the data layer + auth.

---

## Stream F — Frontend + Blueprint + README  `feat/render-frontend-blueprint`
**Runs last; depends on A–E.**

Frontend (Node web service):
- [ ] Root `autogpt_platform/frontend`, `NODE_VERSION=24`, pnpm via corepack.
- [ ] Build: `corepack enable && pnpm install --frozen-lockfile && pnpm generate:api && pnpm
      build` (must run `generate:api`, NOT `:force`). Start `pnpm start`; `/health`.
- [ ] Cap build memory: `NODE_OPTIONS=--max-old-space-size=4096`, `NEXT_PUBLIC_SOURCEMAPS=false`,
      leave `SENTRY_AUTH_TOKEN` unset, don't set `VERCEL`.
- [ ] `NEXT_PUBLIC_*` must be the **public** rest/ws URLs (inlined at build).
      `NEXT_PUBLIC_AGPT_WS_SERVER_URL = wss://<host>/ws` — compose manually (`sync:false`),
      can't come from `fromService … host` alone.
- [ ] Auth = re-point `NEXT_PUBLIC_SUPABASE_URL` to GoTrue (URL only; no rewrite).
- [ ] Add the Render/public domain to `next.config.mjs` `images` + CORS `headers()`.

Blueprint:
- [ ] Assemble one validated `render.yaml`: Project grouping, same region, env group
      `autogpt-platform-secrets`, `previews: { generation: off }`, no `domains:` block.
- [ ] Backend web services bind Render's `PORT`; CORS scoped to the frontend origin.
- [ ] Remember: JWT secret via `generateValue` in the group; **`sync:false` is ignored inside
      groups** — Dashboard-prompt secrets go on the service.
- [ ] `render blueprints validate` until clean. Rewrite `README.md` (H1, Deploy button,
      architecture diagram, env-group table, "Using the app"); add `.env.example`; confirm
      `.env` gitignored.

---

## After all streams merge (orchestrator)

1. Run the secret-handling / git-history sweep (see `plan.md` Preflight) before publishing.
2. `/render-deploy` into shifra-workspace (`tea-d50tvuidbo4c73cahs30`).
3. Create the executor **Workflow** manually in the Dashboard (Stream C) + wire its slug.
4. `/render-template-quality-bar` full checklist.
5. Read logs for **every** service; confirm each is `live`. `/render-debug` loop on anything
   red → fix via PR → redeploy → re-read logs.

## Sequencing

Wave 1: **A + D** (parallel). Wave 2: **B + C** (after A). Wave 3: **E**. Wave 4: **F**.
