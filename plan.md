# Deploy AutoGPT Platform on Render — Build Plan

Turns `autogpt_platform/` into a Render deploy template: a fresh fork clicks **Deploy
to Render** and every service comes up green, wired over Render's private network from
a single `render.yaml`, with no managed RabbitMQ, no Supabase, and no hardcoded hosts.

Source of truth for the target architecture: [`arch.md`](arch.md). Each area below was
investigated by a dedicated owner agent; the concrete findings, env vars, and code
touchpoints are folded into the corresponding workstream.

## ⚠️ Findings that change this plan (read first)

The area investigations surfaced three things that contradict the naive arch.md framing:

1. **Auth: keep GoTrue, do NOT rewrite auth into the backend (Stream B). ✅ DECIDED —
   run the GoTrue container on Render.** The backend only *verifies* JWTs (a shared HMAC
   secret); it never issues or refreshes sessions, and the `auth.users → platform.User`
   trigger was already dropped (users are created lazily from JWT claims). The repo
   already ships a self-hosted **GoTrue** container. The self-issued-JWT rewrite is a
   rejected alternative, not the plan.
2. **Redis cluster mode is a hard blocker (Stream A). ✅ RESOLVED — add a standalone
   client path behind a flag.** The incompatibility is the *client class*, not the data
   or the pub/sub protocol: `RedisCluster`/`AsyncRedisCluster` issue `CLUSTER SLOTS` on
   connect, which single-node Render Key Value rejects. Sharded pub/sub
   (`SSUBSCRIBE`/`SPUBLISH`) is itself supported on standalone Valkey 8, so the fix is a
   bounded 3-file change gated by `REDIS_CLUSTER_MODE` (default `true`; Render sets
   `false`). See Stream A for the file-level plan.
3. **Render Blueprints do not support Workflows (Stream C). ✅ ACCEPTED — Workflow
   resource created manually in the Dashboard.** arch.md's "everything in one
   `render.yaml`" cannot include the executor; blueprint services reference it via env
   vars only.

The consolidated blocker/decision list is in [§ Cross-cutting blockers](#cross-cutting-blockers--decisions).

## Preflight (owned by the orchestrator, before any agent spawns)

- **render.com/templates check** — confirm Render doesn't already ship an AutoGPT
  template. If it does, recommend it and stop.
- **Secret-handling sweep** — scan working tree **and full git history** for real
  committed credentials and any plaintext-key mechanism (key pasted into a tracked
  file, secret persisted to DB/disk, secret shipped to the browser, literal secret or
  `sync: true` in a Blueprint). The `.env.template` hits under `classic/` are
  placeholder examples (`sk-xxx…`), not real keys — cleared. Re-run the history scan
  before spawning; if a real key ever surfaces, stop and follow the "copy-not-fork +
  squash + rotate" remediation in the skill. Note the demo/anon JWT keys in
  `.env.default` must never ship to Render.
- **Workspace** — target **shifra-workspace** (`tea-d50tvuidbo4c73cahs30`); select and
  verify it before creating any resource.

## Target topology (what `render.yaml` will declare)

```
                          ┌───────────────────────────┐
                          │   frontend (node, web)    │  Next.js, public HTTPS
                          └─────────────┬─────────────┘
                                        │ HTTPS (NEXT_PUBLIC_* → rest_server URL)
                                        ▼
   ┌──────────────────┐   WSS   ┌───────────────────────────┐      ┌──────────────┐
   │ websocket_server │◀───────▶│   rest_server (docker,web)│─────▶│ gotrue (auth)│
   │  (docker, web)   │         └───┬───────────┬────────┬───┘      └──────┬───────┘
   └──────────────────┘             │ SQL       │ cache  │ start_task()    │ SQL
                                    ▼           ▼        ▼                 │
                          ┌──────────────┐ ┌──────────┐ ┌────────────────────┐
                          │ db (postgres)│◀│ keyvalue │ │  Render Workflows  │  (Dashboard-only,
                          └──────┬───────┘ │ (redis)  │ │  executor @app.task │   not in blueprint)
                                 └─────────┘└──────────┘ └────────────────────┘
   ┌──────────────────┐  private     ┌───────────────────────────┐
   │ scheduler_server │  network     │   clamav (docker, private)│  file scanning
   │ (docker, private)│◀────────────▶│   reachable from backend  │
   └──────────────────┘              └───────────────────────────┘
```

All resources grouped under one Render **Project** (`autogpt-platform`), same region +
workspace (hard precondition for private DNS). Shared secrets (JWT signing secret,
`OPENAI_API_KEY`, `ENCRYPTION_KEY`, etc.) live in a named **environment group**
(`autogpt-platform-secrets`), referenced via `fromGroup`; service-to-service URLs wired
via `fromService`/`fromDatabase`. **Triage still open:** the compose stack also runs
`database_manager`, `copilot_executor`, `notification_server`, `platform_linking_manager`,
and `falkordb` — the scheduler and copilot paths depend on the first two.

## Workstreams (one agent per area)

Each workstream is a branch + PR. Ordering: **A → then B/C/D in parallel → then E → then
F integration**. Dependencies are called out per stream.

---

### Stream A — Data layer: Postgres + Key Value  `feat/render-data-layer`

Replace Supabase Postgres with Render Managed Postgres and Redis with Render Key Value.
Source: `backend/schema.prisma`, `backend/migrations/`, `backend/data/db.py`,
`backend/data/redis_client.py`, `backend/util/cache.py`, `backend/util/settings.py`.

**Postgres**
- **Version 16** (matches Supabase major; pgvector + pg_trgm available). Immutable after
  create — confirm first.
- Plan Standard/Pro (≥4 GB → ~100 conns). App is multi-process; total conns =
  `DB_CONNECTION_LIMIT` (default 12) × processes × instances. No built-in pooler — size
  the plan or lower the limit. HA needs Professional workspace + Pro plan.
- Storage ~20 GB start (pgvector HNSW + trigram GIN indexes); autoscales, can't shrink.
- Use the **internal** connection string; both `DATABASE_URL` and `DIRECT_URL` point at
  it. Append `?schema=platform` via a wrapper var (`fromDatabase.connectionString` omits
  it). App tables live in the non-public `platform` schema.
- **Extensions** (created by migrations, not on by default): `vector` (pgvector),
  `pg_trgm`. **`pg_cron` is NOT offered by Render** — move the store materialized-view
  refresh to a Render Cron Job or the in-app scheduler (the migration only warns if
  absent). **Verify** unqualified `::vector` casts / `<=>` resolve on Render, since the
  extension is created by the app role rather than a Supabase system role (top
  migration-time risk).
- Migrations: `prisma migrate deploy` (using `DIRECT_URL`) as a predeploy on exactly one
  backend service (see Stream E). Prisma client is generated at Docker build time.

**Key Value (Redis) — RESOLVED: standalone client path behind a flag**
- Root cause is the *client class*, not the pub/sub protocol: `RedisCluster` issues
  `CLUSTER SLOTS`/`SHARDS` on connect, which single-node Render Key Value rejects.
  Sharded pub/sub (`SSUBSCRIBE`/`SPUBLISH`) works on standalone Valkey 8, and standalone
  is strictly more permissive than cluster for multi-key pipelines. Fix, gated by
  `REDIS_CLUSTER_MODE` (default `true` to keep the existing prod cluster; Render sets
  `false`):
  1. `backend/data/redis_client.py` — `connect()`/`connect_async()` build plain
     `Redis`/`AsyncRedis` when standalone; `resolve_shard_for_channel()` returns
     `(HOST, PORT)` directly (skip `get_node_from_key`). The existing
     `connect_sharded_pubsub[_async]()` already use a plain client → point at the single
     node. `SPUBLISH`/`SSUBSCRIBE` via `execute_command` keep working.
  2. `backend/util/cache.py` — `_get_redis()` builds plain `Redis` when standalone; drop
     `target_nodes=RedisCluster.PRIMARIES` from the two `scan_iter` calls.
  3. `backend/copilot/rate_limit.py` / `pending_messages.py` — only *catch*
     `RedisClusterException` and use `execute_command("SPUBLISH", …)`; both function on
     standalone. Verify, no change expected.
  - Alternative (rejected): self-managed Valkey cluster as private services — heavier ops,
    loses managed Key Value, contradicts arch.md.
- Redis is used broadly: object cache, distributed locks, rate limiting, execution/WS
  pub/sub, pending-message/turn queue → **`maxmemoryPolicy: noeviction`** (LRU would drop
  locks/queued turns). Use a **paid** (disk-backed) instance. `ipAllowList: []`.
- App needs discrete host/port: wire `REDIS_HOST`/`REDIS_PORT` via `fromService`
  `host`/`port` (not just `connectionString`); inject `REDIS_PASSWORD` if auth is on.
  Override `.env.default` port `17000` → `6379`.

**Supabase→plain-Postgres gap (feeds Stream B):** no `auth` schema on Render. The signup
triggers are `IF EXISTS`-guarded (won't fail; also largely moot since the auto-user
trigger was dropped and users are created lazily). **No RLS policies exist** — authz is
app-layer, so RLS is not a blocker.

Deliverable: migrations run clean against fresh Render Postgres; standalone Redis client
connects. **Blocks B, C, E, F.**

---

### Stream B — Auth: run self-hosted GoTrue on Render  `feat/render-auth`

**Corrected scope (see Findings #1): keep GoTrue, do not rewrite.** The backend only
*verifies* JWTs (`autogpt_libs/auth/jwt_utils.py`, shared HMAC secret via
`JWT_VERIFY_KEY`); it never issues/refreshes sessions. The `auth.users→platform.User`
trigger was dropped (`20260311000000_drop_auto_user_trigger`); users are created lazily
from JWT claims via `get_or_create_user`. No live FK from `platform.User` to `auth.users`.
So the issuer is pluggable, and the repo already ships GoTrue (`supabase/gotrue:v2.170.0`
in `db/docker/docker-compose.yml`).

**Recommended approach (low-risk):**
- GoTrue as a private web service (or behind the existing Kong gateway), backed by the
  Stream-A Render Postgres (owns the `auth` schema in the same DB).
- Backend keeps `JWT_VERIFY_KEY` = GoTrue's `GOTRUE_JWT_SECRET` (or move to ES256 —
  `config.py` already supports it and warns against HS256).
- Re-point `SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_URL` to the GoTrue/Kong URL; the
  `@supabase/*` frontend libs keep working → **frontend is a URL re-point only**.
- Replace `feature_flag.py`'s `auth.admin.get_user_by_id` (runtime GoTrue admin call)
  with a direct admin call or a `platform.User` lookup.
- Stand up **SMTP** (Render has no managed email) for verification/reset/email-change,
  and register **Google OAuth** redirect URIs on the new domain.

**JWT claim contract any issuer must reproduce:** `sub` (UUID), `email`, `role`
(`authenticated`/`admin`), `phone`, optional `user_metadata.name`, `aud="authenticated"`,
`exp`.

**New env/secrets:** `JWT_VERIFY_KEY`/`GOTRUE_JWT_SECRET` (env-group `generateValue` or
ES256 keypair), `SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_URL`/`SUPABASE_PUBLIC_URL`,
`SUPABASE_SERVICE_ROLE_KEY`/`NEXT_PUBLIC_SUPABASE_ANON_KEY`, `GOTRUE_SMTP_*`,
`GOTRUE_EXTERNAL_GOOGLE_*`, `GOTRUE_SITE_URL`, `GOTRUE_URI_ALLOW_LIST`,
`GOTRUE_DB_DATABASE_URL` (dedicated `supabase_auth_admin` role).

**Rejected alternative — native FastAPI auth:** new `users`/`sessions`/`refresh_tokens`
tables, password hashing, JWT issuance+refresh, OAuth (authlib), email flows, and a full
rewrite of `frontend/src/lib/supabase/*` + API token attach + `middleware.ts`. 3–5× the
effort; only a later phase if dropping the Supabase footprint becomes a goal.

**Phases:** spike/parity → env-ize URLs/secrets + SMTP/OAuth → sever `feature_flag.py`
admin coupling → data migration (export `auth.users` + identities + refresh tokens,
**preserving `id` UUIDs** — every FK across ~40 `User` relations depends on it) →
RLS/trigger cleanup → cutover + forced re-login.

**Risks:** UUID/data migration, password portability, admin-role preservation, email
deliverability, OAuth redirect config, session invalidation at cutover. Deliverable: a
user registers, logs in, and calls an authed endpoint end-to-end. **Depends on A.**

---

### Stream C — RabbitMQ → Render Workflows  `feat/render-workflows-executor`

Remove the broker; move executor dispatch to Render Workflows. Entry point
`executor = "backend.exec:main"` → `run_processes(ExecutionManager())`.

**Current:** RabbitMQ (`pika`/`aio_pika`, `backend/data/rabbitmq.py`). Topology in
`backend/executor/utils.py`: `graph_execution` DIRECT queue (quorum,
`x-consumer-timeout=86_400_000` = 24h) + `graph_execution_cancel` FANOUT. Producer
`add_graph_execution` creates the `AgentGraphExecution` row, flips status to `QUEUED`
(race guard), publishes `GraphExecutionEntry` JSON. `stop_graph_execution` broadcasts a
`CancelExecutionEvent`. Consumer (`backend/executor/manager.py`) runs run+cancel threads
with manual ack/nack; `ExecutionProcessor.on_graph_execution` is a **stateful,
resume-capable in-process engine**. Cross-pod dedup via Redis `ClusterLock`; per-user
throttle `max_concurrent_graph_executions_per_user` (default 25). Publish call sites:
`api/features/v1.py`, `api/external/v1/routes.py`, `library/routes/presets.py`,
`executions/review/routes.py`, `integrations/router.py`, `admin/diagnostics_admin_routes.py`,
`executor/scheduler.py`, `copilot/tools/run_agent.py`, and **`blocks/agent.py`** (nested
sub-graphs).

**Mapping:** run queue → one `@app.task run_graph_execution`; each publish →
`start_task("autogpt-executor/run_graph_execution", [entry_json])`. Engine reused ~verbatim
(one instance == one run). nack+requeue → `@app.task(retry=Retry(...))` (resume makes
retries safe). 24h timeout → `timeout_seconds` (max 86,400). FANOUT cancel →
`cancel_task_run(run_id)` — **persist `graph_exec_id → run_id`** (new column/migration).
Nested runs → subtasks. Scheduled runs → scheduler calls `start_task` (or a cron job).

**Deployment caveat (Findings #3):** **not expressible in `render.yaml`.** Create the
Workflow service manually (Dashboard → New → Workflow, Root `backend/workflows/`, Start
`python main.py`). Add `render_sdk` to backend deps; producers need `RENDER_API_KEY`; wire
the workflow slug as an env var (`fromService` can't reference a Workflow).

**Honest mismatches:** 24h ceiling (parity, no headroom); **per-user rate limiting has no
native equivalent** (reimplement via Redis counter or drop); **cancellation** must be a
cooperative signal so the engine flips DB status to `TERMINATED` + cleans up reviews +
cascades to children (else add a polled Redis cancel flag); **4 MB argument cap** may be
exceeded by `GraphExecutionEntry` (pass `graph_exec_id` only + reload from DB);
fine-grained ack/requeue lost (non-retryable → `return`, not `raise`); broker not fully
removable until copilot-executor + notifications also migrate; Prometheus executor metrics
regress to the Workflows Dashboard.

**Phases:** spike SDK/task → extract the engine from `ExecutionManager` (pure refactor) →
task behind `EXECUTION_BACKEND=rabbitmq|workflows` flag + app-side rate limit + persist
`run_id` → convert nested/scheduled runs → cutover in staging (RabbitMQ rollback) →
decommission. **Depends on A**; coordinates with B for auth context.

---

### Stream D — ClamAV private service  `feat/render-clamav`

Run ClamAV as a Docker private service reachable only from the backend. Client:
`backend/util/virus_scanner.py` (`aioclamd`, INSTREAM over **TCP 3310**); call sites in
`util/file.py`, `util/workspace.py`, store/oauth media routes. Current image:
`clamav/clamav-debian:latest` (no custom Dockerfile).

- **`type: pserv`**, `runtime: image` pulling `clamav/clamav-debian` — **pin an immutable
  tag/digest** (`runtime: image` won't auto-redeploy a moving tag). Port 3310 (not
  reserved); `clamd` binds `0.0.0.0` via `CLAMD_CONF_TCPAddr=0.0.0.0`.
- **freshclam:** signatures go to `/var/lib/clamav`. **Attach a ~2 GB persistent disk**
  there so definitions survive restarts (avoids re-downloading ~200–300 MB + mirror
  rate-limits). Disk pins the service to a single instance (fine for a scanner).
- **Memory (FLAG):** `clamd` loads the whole signature DB into RAM (~2–4 GB RSS, growing)
  → **≥4 GB instance**; consider lowering `MaxThreads` from 12. Likely the priciest box.
- Backend wiring (no code change): `CLAMAV_SERVICE_HOST` / `CLAMAV_SERVICE_PORT` via
  `fromService`; keep `CLAMAV_SERVICE_ENABLED=true`,
  `CLAMAV_MARK_FAILED_SCANS_AS_CLEAN=false` (fail-closed). Needs outbound HTTPS for
  freshclam. **Independent — can start immediately.**

---

### Stream E — Backend services: rest_server, websocket_server, scheduler_server  `feat/render-backend-services`

Containerized backend services from `backend/Dockerfile`. **`dockerContext: .` (repo
root)** — the Dockerfile `COPY`s repo-root-relative paths. One image reused by all three,
differing by `dockerCommand`.

**rest_server → public web service**
- CMD `rest`. **App doesn't read `$PORT`** — `uvicorn` uses `agent_api_port` (env
  `AGENT_API_PORT`, default 8006); the Dockerfile's `ENV PORT=8000` is never read. Fix:
  `dockerCommand: /bin/sh -c 'exec env AGENT_API_PORT=$PORT rest'`.
- Health `/health` (503 until DB connected). **Eager-connects Redis at startup** → a bad
  Key Value config fails startup before `/health` responds (couples to Stream A blocker).
- Plan `standard`+ (bundled chromium/ffmpeg). Migrations: `prisma migrate deploy` as this
  service's `preDeployCommand` (or a Job from the `migrate` stage) — exactly one owner.
- Env: Postgres (`DATABASE_URL`/`DIRECT_URL` + `DB_SCHEMA=platform`, `DB_CONNECTION_LIMIT`,
  `PRISMA_SCHEMA=schema.prisma`), Redis (`REDIS_*`), Workflows (`start_task`, Stream C),
  ClamAV (`CLAMAV_SERVICE_HOST`), auth/JWT (`JWT_VERIFY_KEY` **hard-fails if empty**,
  `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`), core secrets (`ENCRYPTION_KEY` — **must
  stay stable or stored creds become undecryptable**, `UNSUBSCRIBE_SECRET_KEY`, VAPID
  keys), URLs/CORS (`PLATFORM_BASE_URL`, `FRONTEND_BASE_URL`,
  `BACKEND_CORS_ALLOW_ORIGINS`, `APP_ENV`), storage (`MEDIA_GCS_BUCKET_NAME` — local
  fallback needs a disk that kills scaling), optional integration keys (`sync:false`).

**websocket_server → public web service** (WSS)
- CMD `ws`. Browser connects directly (`NEXT_PUBLIC_AGPT_WS_SERVER_URL`), so it must be
  public. Binds `0.0.0.0:8001` — set `WEBSOCKET_SERVER_PORT=$PORT`. Health `/health`.
- Env: `DATABASE_URL`/`DIRECT_URL`, `REDIS_*` (pub/sub delivery), `ENABLE_AUTH=true` + JWT
  secrets (validates `?token=` JWT), `BACKEND_CORS_ALLOW_ORIGINS`. No Workflows dep. Can
  scale out (fan-out via Redis).

**scheduler_server → private service (`pserv`), NOT a cron job**
- CMD `scheduler`. Runs APScheduler **and** an `AppService` RPC server that
  rest/executor/copilot call via `get_scheduler_client()` → cron jobs can't receive
  inbound, so `pserv`. Holds a persistent Postgres jobstore + dynamic user schedules.
- **Port fix:** `PYRO_HOST=0.0.0.0`, expose 8003 (`EXECUTION_SCHEDULER_PORT=$PORT`).
  Health `/health_check`. **`numInstances: 1`** (no leader election — two would
  double-fire). Env: **`DIRECT_URL` required** (jobstore uses non-pooled), `DATABASE_URL`,
  `REDIS_*`, `DATABASE_MANAGER_HOST` (RPC dep — see triage), Workflows creds (its
  scheduled runs enqueue via `start_task`), `APP_ENV`. Allow a long health-check grace
  (startup runs an embedding backfill).

Deliverable: three services boot clean against the data layer + auth. **Depends on A, B, C.**

---

### Stream F — Frontend + Blueprint integration + README  `feat/render-frontend-blueprint`

Frontend web service, unify all fragments into one validated `render.yaml`, write the
template README. **Runs last / integrates the others.**

**Frontend (Web Service, NOT static)** — `next.config.mjs` sets `output: "standalone"`;
there's a server-side proxy route, `middleware.ts`, server components/actions.
- Root `autogpt_platform/frontend` (self-contained pnpm workspace). Runtime Node
  (`NODE_VERSION=24`), pnpm `10.20.0` via corepack.
- Build: `corepack enable && pnpm install --frozen-lockfile && pnpm generate:api && pnpm
  build`. **Must run `generate:api`** (generated client is git-ignored); use the
  committed `openapi.json`, **not** `generate:api:force`. Start `pnpm start`. Health
  `/health`.
- **Build memory ≥4 GB** — override `NODE_OPTIONS` to ~4096, set
  `NEXT_PUBLIC_SOURCEMAPS=false`, leave `SENTRY_AUTH_TOKEN` unset; do **not** set `VERCEL`.
- Backend discovery splits server-side (private URL via `AGPT_SERVER_URL`) vs client-side
  (`NEXT_PUBLIC_*`). **`NEXT_PUBLIC_*` are inlined at build time** → must be the **public**
  HTTPS/WSS URLs of rest/ws (resolvable at build). `NEXT_PUBLIC_AGPT_WS_SERVER_URL` must
  be `wss://<host>/ws` — can't be composed purely from `fromService … host` (use a
  `sync:false` value or the external URL).
- Auth = **URL re-point only** if Stream B keeps GoTrue (else a `src/lib/supabase/*`
  rewrite). Add the Render/public domain to `next.config.mjs` `images` + CORS `headers()`.

**Blueprint assembly** — one `render.yaml` at repo root, one Project, all resources same
region + workspace. References only: `fromDatabase`, `fromService`, `fromGroup`,
`generateValue`. `previews: { generation: off }`, explicit `region`, no `domains:` block.
Env group `autogpt-platform-secrets` (JWT secret via `generateValue`; note **`sync:false`
is ignored inside groups** — Dashboard-prompted secrets go on the service or are entered
on the group post-create). Backend web services bind Render's `PORT`. CORS scoped to the
frontend origin (never `*.onrender.com`). **The executor Workflow is NOT in the blueprint**
(Stream C) — reference its slug via env var. Rewrite `README.md` (H1, intro, Deploy
button, architecture diagram, env-group table, "Using the app"); add `.env.example`;
confirm `.env` gitignored. Run `render blueprints validate` until clean. **Depends on A–E.**

Skeleton:

```yaml
previews:
  generation: off
projects:
  - name: autogpt-platform
    environments:
      - name: production
        databases:
          - { name: db, plan: pro, postgresMajorVersion: "16", ipAllowList: [] }
        services:
          - { type: keyvalue, name: keyvalue, plan: standard, maxmemoryPolicy: noeviction, ipAllowList: [] }
          - type: pserv
            name: clamav
            runtime: image
            image: { url: clamav/clamav-debian:<pinned-tag> }
          - type: web
            name: rest-server
            runtime: docker
            plan: standard
            rootDir: .
            dockerContext: .
            dockerfilePath: autogpt_platform/backend/Dockerfile
            dockerCommand: /bin/sh -c 'exec env AGENT_API_PORT=$PORT rest'
            healthCheckPath: /health
            preDeployCommand: prisma migrate deploy
            envVars:
              - { key: DATABASE_URL, fromDatabase: { name: db, property: connectionString } }
              - { key: DIRECT_URL, fromDatabase: { name: db, property: connectionString } }
              - { key: REDIS_HOST, fromService: { name: keyvalue, type: keyvalue, property: host } }
              - { key: REDIS_PORT, fromService: { name: keyvalue, type: keyvalue, property: port } }
              - { key: CLAMAV_SERVICE_HOST, fromService: { name: clamav, type: pserv, property: host } }
              - { fromGroup: autogpt-platform-secrets }
          - type: web
            name: websocket-server
            runtime: docker
            plan: standard
            rootDir: .
            dockerContext: .
            dockerfilePath: autogpt_platform/backend/Dockerfile
            dockerCommand: ws
            envVars:
              - { key: DATABASE_URL, fromDatabase: { name: db, property: connectionString } }
              - { key: REDIS_HOST, fromService: { name: keyvalue, type: keyvalue, property: host } }
              - { fromGroup: autogpt-platform-secrets }
          - type: pserv
            name: scheduler-server
            runtime: docker
            plan: standard
            numInstances: 1
            rootDir: .
            dockerContext: .
            dockerfilePath: autogpt_platform/backend/Dockerfile
            dockerCommand: scheduler
            envVars:
              - { key: PYRO_HOST, value: "0.0.0.0" }
              - { key: DATABASE_URL, fromDatabase: { name: db, property: connectionString } }
              - { key: DIRECT_URL, fromDatabase: { name: db, property: connectionString } }
              - { key: REDIS_HOST, fromService: { name: keyvalue, type: keyvalue, property: host } }
              - { fromGroup: autogpt-platform-secrets }
          - type: web
            name: frontend
            runtime: node
            plan: standard
            rootDir: autogpt_platform/frontend
            buildCommand: corepack enable && pnpm install --frozen-lockfile && pnpm generate:api && pnpm build
            startCommand: pnpm start
            healthCheckPath: /health
            envVars:
              - { key: NEXT_PUBLIC_AGPT_SERVER_URL, sync: false }
              - { key: NEXT_PUBLIC_AGPT_WS_SERVER_URL, sync: false }
              - { key: AGPT_SERVER_URL, fromService: { name: rest-server, type: web, property: hostport } }
          # gotrue (auth) — Stream B; executor Workflow — Stream C, Dashboard-only (not here)
envVarGroups:
  - name: autogpt-platform-secrets
    envVars:
      - { key: JWT_VERIFY_KEY, generateValue: true }
      - { key: ENCRYPTION_KEY, value: "" }   # sync:false ignored in groups — set in Dashboard
```

---

## Cross-cutting blockers & decisions

Resolve before/at build:

1. **Redis cluster → standalone (A). ✅ RESOLVED** — standalone client path behind
   `REDIS_CLUSTER_MODE` (3-file change; see Stream A). Remaining: confirm `noeviction`
   given lock/queue keys' TTLs.
2. **Auth strategy (B). ✅ DECIDED — GoTrue on Render.** Remaining: existing prod user
   base to migrate, or fresh template deploy?
3. **Workflows outside the Blueprint (C). ✅ ACCEPTED** — manual Dashboard creation +
   env-var wiring of the slug; confirm preview-environment handling.
4. **Port binding (E).** `AGENT_API_PORT`/`WEBSOCKET_SERVER_PORT`/`EXECUTION_SCHEDULER_PORT`
   + `PYRO_HOST=0.0.0.0`.
5. **Migrations ownership (A/E).** Exactly one service runs `prisma migrate deploy`.
6. **Postgres major version 16** (immutable) — confirm vs Supabase.
7. **pg_cron replacement (A)** — Render Cron Job or in-app scheduler.
8. **DB connection budget (A/E)** — `DB_CONNECTION_LIMIT` × processes × instances < cap.
9. **Extra compose services** — triage `database_manager`, `copilot_executor`,
   `notification_server`, `platform_linking_manager`, `falkordb` (scheduler/copilot depend
   on the first two).
10. **Media storage (E)** — GCS vs local disk (disk kills scaling/zero-downtime).
11. **ClamAV sizing (D)** — ~4 GB RAM + persistent disk; pin image tag.
12. **SMTP + OAuth providers (B)** — required for GoTrue email flows + Google login.
13. **`ENCRYPTION_KEY` stability (E)** — never regenerate per deploy.
14. **WS public URL composition (E/F)** — `wss://<host>/ws` not buildable from
    `fromService … host` alone.

## Orchestration mapping

| Agent | Branch | Steps (checkpoints) | Depends on |
|---|---|---|---|
| A data-layer | `feat/render-data-layer` | plan, build, validate, open-pr | — |
| B auth (GoTrue) | `feat/render-auth` | plan, build, test, open-pr | A |
| C workflows | `feat/render-workflows-executor` | plan, build, test, open-pr | A |
| D clamav | `feat/render-clamav` | build, open-pr | — |
| E backend-svcs | `feat/render-backend-services` | build, open-pr | A,B,C |
| F frontend+blueprint | `feat/render-frontend-blueprint` | build, validate, open-pr | A–E |

Wave 1: **A + D** in parallel. Wave 2: **B + C** (after A). Wave 3: **E**. Wave 4: **F**.

## After all streams merge (orchestrator)

1. `/render-deploy` into shifra-workspace (`tea-d50tvuidbo4c73cahs30`).
2. Create the executor **Workflow** manually in the Dashboard (Stream C) and wire its slug.
3. `/render-template-quality-bar` full checklist.
4. Read logs for **every** service; confirm each is `live` and clean.
5. `/render-debug` loop on anything red → fix via PR → redeploy → re-read logs.
