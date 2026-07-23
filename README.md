# Deploy AutoGPT Platform on Render

Run the full [AutoGPT Platform](https://github.com/Significant-Gravitas/AutoGPT) — the
visual agent builder, marketplace, and execution engine — on [Render](https://render.com)
from a single `render.yaml` Blueprint. Managed Postgres and Key Value replace Supabase and
Redis, self-hosted GoTrue handles auth, ClamAV scans uploads, and Render Workflows run the
executor. No managed RabbitMQ, no managed Supabase, no hardcoded hosts.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Significant-Gravitas/AutoGPT)

---

## Architecture

Everything below is declared in [`render.yaml`](render.yaml) under one Render **Project**
(`autogpt-platform`), same region (`oregon`) and workspace — required for private
networking. The executor **Workflow** is the one piece created by hand (Blueprints can't
declare Workflows yet); the backend reaches it via `RENDER_WORKFLOW_SLUG`.

```
                       ┌──────────────────────────┐
   browser  ─────────▶ │  frontend (Next.js, web) │  public HTTPS
                       │  /auth/v1/* ─┐           │
                       └──────┬───────┼───────────┘
        NEXT_PUBLIC_* (HTTPS) │       │ private proxy
              ┌───────────────┘       ▼
              ▼                 ┌─────────────────────┐
   ┌──────────────────┐  WSS    │ gotrue (auth, pserv)│───┐
   │ websocket-server │◀──────▶ └─────────────────────┘   │
   │   (docker, web)  │                                   │ SQL
   └────────┬─────────┘         ┌───────────────────────┐ │
            │            ┌────▶ │ rest-server (docker,  │ │
            │            │      │ web) — owns migrations│─┤
            │            │      └──┬─────────┬────────┬─┘ │
            │ pub/sub    │  RPC    │ SQL     │ cache  │ start_task()
            ▼            │         ▼         ▼        ▼    ▼
   ┌──────────────┐  ┌───┴──────┐ ┌──────────┐ ┌──────────┐ ┌────────────────────┐
   │  keyvalue    │  │scheduler-│ │   db     │ │ keyvalue │ │  Render Workflows  │
   │  (redis)     │  │ server   │ │(postgres)│ │ (redis)  │ │  executor          │
   └──────────────┘  │ (pserv)  │ └────┬─────┘ └──────────┘ │ (Dashboard-only,   │
                     └────┬─────┘      │                    │  NOT in blueprint) │
                          │ RPC        │ SQL                └────────────────────┘
                          ▼            │
                 ┌──────────────────┐  │        ┌───────────────────────────┐
                 │ database-manager │──┘        │ clamav (image, pserv)     │
                 │     (pserv)      │           │ file scanning, 3310/TCP   │◀── rest-server
                 └──────────────────┘           └───────────────────────────┘

   cron: autogpt-platform-mv-refresh — refreshes store materialized views (pg_cron replacement)
```

| Resource | Type | Role |
|----------|------|------|
| `autogpt-platform-db` | Postgres 18 | App data (`platform` schema) + GoTrue's `auth` schema |
| `autogpt-platform-keyvalue` | Key Value | Locks, queues, pending-turn buffers, rate limits, cache (`noeviction`) |
| `autogpt-platform-mv-refresh` | Cron | Refreshes store/suggested-block materialized views every 15 min |
| `clamav` | Private (image) | Virus scanning for uploads (raw TCP 3310) |
| `autogpt-platform-gotrue` | Private (image) | Self-hosted Supabase Auth; reached only via the frontend `/auth/v1` proxy |
| `rest-server` | Web (Docker) | FastAPI API; **sole owner of `prisma migrate deploy`** |
| `websocket-server` | Web (Docker) | WSS event fan-out via Redis pub/sub |
| `scheduler-server` | Private (Docker) | APScheduler + RPC (`numInstances: 1`) |
| `database-manager` | Private (Docker) | Centralized Prisma pool over RPC (scheduler's DB backend) |
| `frontend` | Web (Node) | Next.js UI |
| **executor Workflow** | **Workflow (manual)** | Runs agent graph executions; created in the Dashboard |

---

## Secrets & environment

Every environment value falls into one of four buckets, which tells you where it lives and
when you set it:

| # | Bucket | Where | Who sets it | When |
|---|--------|-------|-------------|------|
| 1 | Automatic wires (`fromDatabase` / `fromService`) | each service | Render, from `render.yaml` | at apply — nothing to do |
| 2 | Env group `autogpt-platform-secrets` | Blueprint-managed group | Render (`generateValue`) | at apply — never touched |
| 3 | Per-service prompts (`sync: false`) | each service | you, in the Dashboard | some at apply, some after URLs exist |
| 4 | Env group `autogpt-platform-llm` | Dashboard group you create | you | last, after the Workflow exists |

Only buckets 3 and 4 need your input. The two env groups are not interchangeable:

- `autogpt-platform-secrets` — declared in `render.yaml`, created automatically, holds only
  `UNSUBSCRIBE_SECRET_KEY`. You never add keys to it.
- `autogpt-platform-llm` — you create it by hand for LLM keys (see
  [LLM / Claude API keys](#manual-step--llm--claude-api-keys)). The only group you touch.

Never put a `sync: false` key in an env group — `sync: false` is ignored inside groups, so
the secret silently becomes blank.

### What fans out and what doesn't

Some values are set once and shared; others must be entered on every service that needs them:

- Fanned out (set once): `JWT_VERIFY_KEY` is a `generateValue` on rest-server, and
  `FRONTEND_BASE_URL` is a placeholder `value:` on rest-server. Every other service pulls
  both via `fromService`.
- Entered per-service: `ENCRYPTION_KEY`, `RENDER_API_KEY`, and `RENDER_WORKFLOW_SLUG` each go
  directly on every service that needs them.

The difference: a `fromService … envVarKey` reference can only copy a source that holds a
value at apply time. A `sync: false` source has none when Render plans the apply, so the
reference fails with `environment variable not found` and rolls the entire apply back. Don't
"dedup" the per-service secrets into a `fromService` chain — it breaks the deploy.

### URL config layer

Most URL variables collapse to a few origins, which `render.yaml` derives in the build/start
commands, so you enter each origin at most once:

- Backend: one keyname, `FRONTEND_BASE_URL`, on rest-server only. It ships as a placeholder
  and you overwrite it with the real origin after the first deploy. rest-server derives
  `PLATFORM_BASE_URL` (from `RENDER_EXTERNAL_URL`) and `BACKEND_CORS_ALLOW_ORIGINS` (from it);
  gotrue, ws, scheduler, and database-manager pull it via `fromService`.
- Frontend: two keynames, `NEXT_PUBLIC_AGPT_SERVER_URL` (API) and
  `NEXT_PUBLIC_AGPT_WS_SERVER_URL` (WebSocket) — separate services, so they can't be derived.
  The frontend's own origin (for `NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_FRONTEND_BASE_URL`)
  is derived from its build-time `RENDER_EXTERNAL_URL`.

Each derived var uses `${VAR:-…}`, so a custom domain set in the Dashboard overrides the
default.

### Deployer-supplied keys (bucket 3)

These `sync: false` prompts split by when their value is knowable.

Phase 1 — at apply, values you already control:

- `ENCRYPTION_KEY` — same value on rest-server and database-manager (and the Workflow)
- `RENDER_API_KEY` — same value on rest-server and scheduler-server
- `RENDER_WORKFLOW_SLUG` — same slug on rest-server and scheduler-server (create the
  [executor Workflow](#manual-step--the-executor-workflow) first)
- `GOTRUE_SMTP_*` — if you have SMTP
- Frontend URL keys — enter a valid `https://` placeholder (e.g. `https://example.com`); an
  empty or malformed value fails validation at boot

Phase 2 — after the first apply, values that need the `*.onrender.com` hostnames:

- Frontend: `NEXT_PUBLIC_AGPT_SERVER_URL`, `NEXT_PUBLIC_AGPT_WS_SERVER_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- rest-server: overwrite `FRONTEND_BASE_URL` with the real frontend origin
- gotrue: `GOTRUE_URI_ALLOW_LIST`

Then redeploy the frontend and rest-server, then gotrue, ws, scheduler, and database-manager
(they pull the new `FRONTEND_BASE_URL` on their next deploy). Derived vars
(`PLATFORM_BASE_URL`, `BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_FRONTEND_BASE_URL`, GoTrue's `GOTRUE_SITE_URL` / `GOTRUE_API_EXTERNAL_URL`) are
not entered.

Full list — each lives on the named service(s); the same key on two services takes the same
value at each prompt:

| Key | Service(s) | What to enter |
|-----|-----------|---------------|
| `ENCRYPTION_KEY` | rest-server + database-manager (+ Workflow) | Fernet key for stored credentials (see [below](#generating-encryption_key)) |
| `RENDER_API_KEY` | rest-server + scheduler-server (+ Workflow) | Render workspace API key (Workflows dispatch) |
| `RENDER_WORKFLOW_SLUG` | rest-server + scheduler-server | Executor Workflow slug — create the Workflow first so it's known at apply |
| `NEXT_PUBLIC_AGPT_SERVER_URL` | frontend | `https://<rest-server host>/api` (build-time) |
| `NEXT_PUBLIC_AGPT_WS_SERVER_URL` | frontend | `wss://<websocket-server host>/ws` (build-time) |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | frontend | Anon JWT signed with `JWT_VERIFY_KEY` (see [below](#generating-the-anon-key)) |
| `GOTRUE_URI_ALLOW_LIST` | gotrue | Allowed redirect URLs (e.g. `https://<frontend>/**`) |
| `GOTRUE_SMTP_*` | gotrue | SMTP host/port/user/pass/sender/admin |
| `GOTRUE_EXTERNAL_GOOGLE_*` | gotrue | Optional Google OAuth (leave `ENABLED=false` to skip) |

`FRONTEND_BASE_URL` is not in this table — it's a placeholder `value:` on rest-server, not a
prompt (see [URL config layer](#url-config-layer)). Not prompted either: `PLATFORM_BASE_URL`,
`BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_FRONTEND_BASE_URL`,
GoTrue's `GOTRUE_SITE_URL` / `GOTRUE_API_EXTERNAL_URL`, and every consumer's
`FRONTEND_BASE_URL` / `JWT_VERIFY_KEY` (all derived or pulled via `fromService`).

The authoritative source is [`render.yaml`](render.yaml) — each `sync: false` entry carries a
`# DEPLOYER:` comment and Render prompts for it at deploy. Local development doesn't use this
table; it runs from `.env.default` files via `make init-env` (see [`local.md`](local.md#environment-files)).

#### Generating the anon key

`NEXT_PUBLIC_SUPABASE_ANON_KEY` is an HS256 JWT signed with `JWT_VERIFY_KEY`. Read that value
from rest-server's environment after the first deploy, then mint the token with stdlib:

```bash
JWT_VERIFY_KEY='<paste from rest-server env>' python3 - <<'PY'
import os, json, hmac, hashlib, base64
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
seg = lambda d: b64(json.dumps(d, separators=(",", ":")).encode())
h = seg({"alg": "HS256", "typ": "JWT"})
p = seg({"role": "anon", "iss": "supabase", "aud": "authenticated"})
sig = b64(hmac.new(os.environ["JWT_VERIFY_KEY"].encode(), h + b"." + p, hashlib.sha256).digest())
print((h + b"." + p + b"." + sig).decode())
PY
```

For a no-Kong deploy any non-empty value works, but a correct anon JWT is recommended.

#### Generating `ENCRYPTION_KEY`

`ENCRYPTION_KEY` is a `cryptography.fernet.Fernet` key — url-safe base64 of 32 bytes.
Generate one with stdlib:

```bash
python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Paste the same value into each service that decrypts stored credentials — rest-server,
database-manager, and the manual Workflow. ws and scheduler don't decrypt, so they omit it.
It can't be auto-generated (`generateValue` may emit chars Fernet rejects) or fanned out (a
`fromService` reference can't resolve a `sync: false` source at apply).

Each service with the key logs a fingerprint at boot —
`ENCRYPTION_KEY loaded (fingerprint=<12 hex chars>)`. Confirm rest-server, database-manager,
and the Workflow print the same fingerprint; a mismatch means credential decryption will
fail. A malformed key fails fast at startup rather than as a runtime `InvalidToken`.

---

## Deploy

Follow the buckets from [Secrets & environment](#secrets--environment) in order:

1. Fork this repo and push it to your GitHub/GitLab account.
2. Generate `ENCRYPTION_KEY` ([below](#generating-encryption_key)) and grab your
   `RENDER_API_KEY` from Dashboard → Settings → API Keys — having them ready avoids a second pass.
3. Create the executor Workflow shell ([part A](#manual-step--the-executor-workflow)) so its
   slug is known before you apply. Note the slug; its first deploy will fail (no DB/Redis/JWT
   yet — those come with the Blueprint), which is expected.
4. In Render, New → Blueprint, select your fork. Render prompts for every `sync: false` key.
   Fill the phase-1 prompts (`ENCRYPTION_KEY`, `RENDER_API_KEY`, `RENDER_WORKFLOW_SLUG` from
   step 3, and SMTP if any) per [Deployer-supplied keys](#deployer-supplied-keys-bucket-3).
   For the frontend URL keys, enter a valid `https://` placeholder (hostnames aren't known
   yet); leave `GOTRUE_URI_ALLOW_LIST` / `GOTRUE_SMTP_*` / `GOTRUE_EXTERNAL_GOOGLE_*` blank if
   unused.
5. Apply. All resources come up; Render auto-creates the `autogpt-platform-secrets` group and
   `JWT_VERIFY_KEY`, and rest-server runs `prisma migrate deploy` on predeploy. The real
   `RENDER_WORKFLOW_SLUG` was baked in at step 4, so there's no slug-wiring redeploy.
6. Set the real public URLs (phase 2) once services have their `*.onrender.com` hostnames, then
   redeploy the frontend and rest-server, then gotrue, ws, scheduler, and database-manager. See
   [Deployer-supplied keys](#deployer-supplied-keys-bucket-3).
7. Finish the executor Workflow ([part B](#manual-step--the-executor-workflow)): now that
   Postgres, Key Value, and `JWT_VERIFY_KEY` exist, wire its env and deploy for real.
8. Create the `autogpt-platform-llm` env group — see
   [LLM / Claude API keys](#manual-step--llm--claude-api-keys).

### Manual step — the executor Workflow

The executor is a Render Workflow, which Blueprints can't declare. The Blueprint needs its
slug and it needs the Blueprint's Postgres / Key Value / `JWT_VERIFY_KEY`, so it's created in
two parts around the apply.

Part A — create the shell (before the apply, step 3):

1. New → Workflow, link this repo, same workspace + region.
2. Set Root Directory `autogpt_platform/backend`, Build Command
   `poetry install && poetry run pip install --no-deps render_sdk==0.7.0`, Start Command
   `poetry run python -m backend.workflows.main`.
3. Create it and copy its slug (task id shows as `{slug}/run_graph_execution`). Its first
   deploy fails — Postgres/Redis/`JWT_VERIFY_KEY` don't exist yet. Expected; you only need the
   slug for the Blueprint prompt (step 4).

Part B — finish it (after the apply, step 7):

4. Give it the same wiring as the backend: `DATABASE_URL` + `DIRECT_URL` with
   `?schema=platform`, `REDIS_URL` (or the split `REDIS_*` vars), `REDIS_CLUSTER_MODE=false`,
   `EXECUTION_BACKEND=workflows`, `RENDER_API_KEY`, `JWT_VERIFY_KEY` (copy from rest-server),
   the same `ENCRYPTION_KEY` as rest-server (confirm the boot fingerprint matches), plus any
   provider API keys your graphs use.
5. Deploy for real. It should boot; graph execution works end-to-end.

### Manual step — LLM / Claude API keys

Two features need an LLM credential: copilot chat (`/api/chat/*`, on rest-server) and the AI
blocks (AI Text Generator, `claude_code`, `orchestrator`, on the executor Workflow). The
deploy succeeds with no key set — these features just return nothing until one is present.

Because `sync: false` is invalid in env groups and the Workflow isn't a Blueprint resource,
these keys aren't in `render.yaml`. Use one Dashboard env group read by both consumers:

1. Create the env group: Dashboard → Env Groups → New → `autogpt-platform-llm`. Don't add it
   to `render.yaml`.
2. Add the keys for one transport:

   | Transport | Env to set | Render? |
   |-----------|-----------|---------|
   | OpenRouter (default, recommended) | `OPEN_ROUTER_API_KEY=<key>` (leave `CHAT_USE_OPENROUTER` unset/`true`) | ✅ |
   | Direct Anthropic | `ANTHROPIC_API_KEY=<key>` and `CHAT_USE_OPENROUTER=false` (or `CHAT_DIRECT_ANTHROPIC_API_KEY`) | ✅ |
   | Subscription (`claude login`) | `CHAT_USE_CLAUDE_CODE_SUBSCRIPTION=true` | ⚠️ advanced/dev only (see Notes) |

   Add `OPENAI_API_KEY=<key>` too if OpenAI-based blocks are used (also the OpenRouter
   fallback). AI blocks read `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` directly.
3. Link the group to both consumers — rest-server (copilot chat) and the executor Workflow
   (AI blocks): Environment → Link Environment Group → `autogpt-platform-llm` → save & redeploy.
   Sharing the group means each key is entered once.

---

## Using the app

1. Open the frontend URL and **sign up**. GoTrue sends confirmation email via your SMTP
   (or set `GOTRUE_MAILER_AUTOCONFIRM=true` on gotrue for a no-SMTP demo).
2. Log in, open the **Builder**, and create or import an agent graph.
3. **Run** the agent — `rest-server` dispatches to the executor Workflow via
   `start_task`; progress streams back over the websocket-server.
4. Browse the **Marketplace** to try shared agents. File uploads are virus-scanned by
   ClamAV before processing.

---

## Local development

To run the stack on your own machine — full Docker or core-in-Docker with the app
native and hot-reloading — see [`local.md`](local.md). Local dev uses the repo's
`docker-compose.yml` and the **RabbitMQ** execution backend, **not** Render Workflows
(`EXECUTION_BACKEND=workflows` is a deploy-only concern handled by `render.yaml`).
You can still exercise the Workflows path locally before deploying — see
[Verify it — run a graph through Workflows](local.md#verify-it--run-a-graph-through-workflows).

Unlike the single Dashboard env groups used for the Render deploy above, local dev reads
**three separate `.env` files** — `backend/.env` (all backend services, incl. LLM/copilot
keys), `frontend/.env` (browser `NEXT_PUBLIC_*` only), and the root `.env` (Compose +
Supabase infra). Put each key in the file whose services need it, and recreate the affected
containers after editing. See [Environment files](local.md#environment-files) in `local.md`.

---

## Notes & tradeoffs

- **Region/workspace:** every resource is in `oregon` in one workspace — required for
  private DNS (`fromService`/`fromDatabase`).
- **Redis standalone:** `REDIS_CLUSTER_MODE=false` selects the standalone client path;
  Key Value uses `noeviction` so locks/queued turns are never dropped.
- **Migrations:** only `rest-server` runs `prisma migrate deploy` (predeploy). No other
  service migrates.
- **Why some secrets are entered per-service:** `fromService … envVarKey` can only copy a
  source that already holds a value at apply (`value:` / `generateValue`). It **cannot**
  resolve a `sync: false` source — the reference errors with `environment variable not found`
  and rolls the whole apply back. So deployer-supplied secrets (`ENCRYPTION_KEY`,
  `RENDER_API_KEY`, `RENDER_WORKFLOW_SLUG`) are entered on each service that needs them rather
  than fanned out from rest-server. Only `JWT_VERIFY_KEY` (a `generateValue`) and
  `FRONTEND_BASE_URL` (a placeholder `value:`) are fanned out, because both hold a value at
  apply. Don't "dedup" the per-service secrets back into a `fromService` chain — it will break
  the deploy.
- **Broker not fully gone:** RabbitMQ is still used by copilot-executor and the
  notification server (both out of scope for this template); only graph execution moved
  to Workflows.
- **Scaling:** `scheduler-server` and `database-manager` stay at `numInstances: 1`.
  `clamav` has a disk, so it can't scale horizontally. Re-budget `DB_CONNECTION_LIMIT`
  before raising instance counts.
- **Claude subscription transport (`CHAT_USE_CLAUDE_CODE_SUBSCRIPTION`):** advanced/dev
  only. It needs `claude login` OAuth tokens persisted under the CLI config dir
  (`$HOME/.claude` / `CLAUDE_CONFIG_DIR`), which an ephemeral container loses on redeploy.
  Running it on Render would require a persistent disk on rest-server at the CLI config dir
  (forcing single-instance, no zero-downtime deploys) plus a one-time `claude login` over
  SSH. Prefer the OpenRouter or direct-Anthropic transports for deployments.

For the AutoGPT product, docs, and community, see the
[upstream repository](https://github.com/Significant-Gravitas/AutoGPT) and
[docs.agpt.co](https://docs.agpt.co).
