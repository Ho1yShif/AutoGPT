# Deploy AutoGPT Platform on Render

Run the full [AutoGPT Platform](https://github.com/Significant-Gravitas/AutoGPT) — the
visual agent builder, marketplace, and execution engine — on [Render](https://render.com)
from a single `render.yaml` Blueprint. Managed Postgres and Key Value replace Supabase and
Redis, self-hosted GoTrue handles auth, ClamAV scans uploads, and Render Workflows run the
executor. No managed RabbitMQ, no managed Supabase, no hardcoded hosts.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Significant-Gravitas/AutoGPT)

> Replace the `repo=` URL above with your fork before publishing.

---

## Architecture

Everything below is declared in [`render.yaml`](render.yaml) under one Render **Project**
(`autogpt-platform`), same region (`oregon`) and workspace — required for private
networking. The executor **Workflow** is the one piece created by hand (Blueprints can't
declare Workflows yet); the backend reaches it via `RENDER_WORKFLOW_SLUG`.

```
                       ┌──────────────────────────┐
   browser  ─────────▶ │  frontend (Next.js, web) │  public HTTPS
                       │  /auth/v1/* ─┐            │
                       └──────┬───────┼────────────┘
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

### Auto-generated (env group `autogpt-platform-secrets`)

Created once by Render and shared across services via `fromGroup`; you never set these by hand.

| Key | Used by | Notes |
|-----|---------|-------|
| `JWT_VERIFY_KEY` | rest, ws, scheduler, db-manager, GoTrue | HS256 secret; copied into GoTrue's `GOTRUE_JWT_SECRET`. |
| `UNSUBSCRIBE_SECRET_KEY` | rest | Signs email unsubscribe links. |

> `ENCRYPTION_KEY` is **not** auto-generated — Render's `generateValue` is not guaranteed to
> be a valid Fernet key. It is deployer-supplied instead (see the table below).

### Deployer-supplied (Dashboard prompts, `sync: false`)

`sync: false` is ignored inside env groups, so these live on individual services and are
entered in the Dashboard at deploy time.

| Key | Service(s) | What to enter |
|-----|-----------|---------------|
| `ENCRYPTION_KEY` | rest, ws, scheduler, db-manager, **Workflow** | Fernet key for stored credentials — **generate once, paste the identical value into all 5** (see below) |
| `RENDER_API_KEY` | rest-server, scheduler-server | Render workspace API key (Workflows dispatch) |
| `RENDER_WORKFLOW_SLUG` | rest-server, scheduler-server | Slug of the manual executor Workflow — **unknown until it exists**; set + redeploy |
| `PLATFORM_BASE_URL` | rest-server | Backend (rest-server) public origin |
| `FRONTEND_BASE_URL` | rest-server | Frontend public origin |
| `BACKEND_CORS_ALLOW_ORIGINS` | rest-server, websocket-server | JSON array, e.g. `["https://your-frontend.onrender.com"]` — scope to the frontend origin only |
| `NEXT_PUBLIC_AGPT_SERVER_URL` | frontend | `https://<rest-server host>/api` (build-time) |
| `NEXT_PUBLIC_AGPT_WS_SERVER_URL` | frontend | `wss://<websocket-server host>/ws` (build-time) |
| `NEXT_PUBLIC_SUPABASE_URL` | frontend | The **frontend's own** public origin (auth proxies through it) |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | frontend | Anon JWT signed with `JWT_VERIFY_KEY` (see below) |
| `NEXT_PUBLIC_FRONTEND_BASE_URL` | frontend | The frontend's own public origin |
| `GOTRUE_SITE_URL`, `GOTRUE_API_EXTERNAL_URL`, `GOTRUE_URI_ALLOW_LIST` | gotrue | Frontend origin + allowed redirect URLs |
| `GOTRUE_SMTP_*` | gotrue | SMTP host/port/user/pass/sender/admin (email confirm, reset, change) |
| `GOTRUE_EXTERNAL_GOOGLE_*` | gotrue | Optional Google OAuth (leave `ENABLED=false` to skip) |

The authoritative list of deployer-supplied values is [`render.yaml`](render.yaml) itself —
every one is a `sync: false` entry annotated with a `# DEPLOYER:` comment, and Render prompts
for them when you deploy the Blueprint. The table above is the human-readable summary.

> **Local development** does not use this table — it runs from the committed `.env.default`
> files via `make init-env`. See [`local.md`](local.md#environment-files).

#### Generating the anon key

`NEXT_PUBLIC_SUPABASE_ANON_KEY` is a JWT signed with `JWT_VERIFY_KEY` (read the generated
value from the env group after the first deploy):

```
header:  {"alg":"HS256","typ":"JWT"}
payload: {"role":"anon","iss":"supabase","aud":"authenticated"}
```

Sign it with the group's `JWT_VERIFY_KEY` (e.g. via jwt.io). For a no-Kong deploy any
non-empty value works, but a correct anon JWT is recommended.

#### Generating `ENCRYPTION_KEY`

The backend loads `ENCRYPTION_KEY` as a `cryptography.fernet.Fernet` key — it must be
url-safe base64 of exactly 32 bytes, which Render's `generateValue` does **not** guarantee
(it can emit `+`/`/` chars that Fernet rejects). So it is deployer-supplied. Generate **one**
key and paste that **identical** value into all five places (`rest-server`,
`websocket-server`, `scheduler-server`, `database-manager`, and the executor Workflow):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Each backend service logs a non-secret fingerprint at boot —
`ENCRYPTION_KEY loaded (fingerprint=<12 hex chars>)`. Confirm every service prints the
**same** fingerprint; a mismatch means a service has a different key and credential
decryption will fail. If `ENCRYPTION_KEY` is malformed, the service fails fast at startup
with a clear error instead of an opaque runtime `InvalidToken`.

---

## Deploy

1. **Fork** this repo and push it to your GitHub/GitLab account.
2. In Render, **New → Blueprint**, select your fork. Render reads `render.yaml`.
3. Fill in the `sync: false` prompts you already know (SMTP, CORS placeholders, etc.).
   The frontend `NEXT_PUBLIC_*` URLs depend on the service hostnames — you can set
   placeholders now and correct them in step 5.
4. **Apply.** Postgres, Key Value, GoTrue, ClamAV, the four backend services, the cron,
   and the frontend come up. `rest-server` runs `prisma migrate deploy` on predeploy.
5. **Set the real public URLs.** Once services have their `*.onrender.com` hostnames (or
   your custom domains), set on the **frontend**: `NEXT_PUBLIC_AGPT_SERVER_URL`,
   `NEXT_PUBLIC_AGPT_WS_SERVER_URL`, `NEXT_PUBLIC_SUPABASE_URL`,
   `NEXT_PUBLIC_FRONTEND_BASE_URL`; and on **gotrue**: `GOTRUE_SITE_URL`,
   `GOTRUE_API_EXTERNAL_URL`, `GOTRUE_URI_ALLOW_LIST`; and `PLATFORM_BASE_URL` /
   `FRONTEND_BASE_URL` / `BACKEND_CORS_ALLOW_ORIGINS` on the backend. Redeploy the frontend
   (its `NEXT_PUBLIC_*` are inlined at build time).

### Manual step — the executor Workflow

The executor runs as a Render **Workflow**, which Blueprints cannot declare. After Postgres
and Key Value exist:

1. **New → Workflow**, link this repo, same workspace + region.
2. **Root Directory:** `autogpt_platform/backend`
   **Build Command:** `poetry install && poetry run pip install --no-deps render_sdk==0.7.0`
   **Start Command:** `poetry run python -m backend.workflows.main`
3. Give it the same DB / Redis / secret wiring as the backend (`DATABASE_URL` +
   `DIRECT_URL` with `?schema=platform`, `REDIS_URL` from the Key Value connection string
   (or the split `REDIS_*` vars), `REDIS_CLUSTER_MODE=false`,
   `EXECUTION_BACKEND=workflows`, `RENDER_API_KEY`, `JWT_VERIFY_KEY`, and the **same
   deployer-generated `ENCRYPTION_KEY` you set on the backend services** (confirm the boot
   fingerprint matches), plus provider API keys your graphs use).
4. Deploy it, copy its slug (task id shows as `{slug}/run_graph_execution`).
5. Set `RENDER_WORKFLOW_SLUG` (+ `RENDER_API_KEY`, `EXECUTION_BACKEND=workflows`) on
   `rest-server` and `scheduler-server`, then redeploy those two.

### Manual step — LLM / Claude API keys

Two features need an LLM credential: **copilot chat** (`/api/chat/*`, on `rest-server`)
and the **AI blocks** (AI Text Generator, `claude_code`, `orchestrator`, on the executor
Workflow). **The deploy succeeds with no key set** — copilot chat and AI blocks simply
return nothing until one is present, so this step is optional-but-required-for-those-features.

Because `sync: false` is invalid inside env groups and the executor Workflow isn't a
Blueprint resource, these keys are **not** in `render.yaml`. Instead use one
Dashboard-managed env group read by both consumers, so each key is entered exactly once:

1. **Create the env group.** Dashboard → Env Groups → New → name it
   **`autogpt-platform-llm`**. (Dashboard-managed; do **not** add it to `render.yaml`.)
2. **Add the keys for the transport you want** (pick one):

   | Transport | Env to set | Render? |
   |-----------|-----------|---------|
   | OpenRouter (default, recommended) | `OPEN_ROUTER_API_KEY=<key>` (leave `CHAT_USE_OPENROUTER` unset/`true`) | ✅ |
   | Direct Anthropic | `ANTHROPIC_API_KEY=<key>` **and** `CHAT_USE_OPENROUTER=false` (or `CHAT_DIRECT_ANTHROPIC_API_KEY`) | ✅ |
   | Subscription (`claude login`) | `CHAT_USE_CLAUDE_CODE_SUBSCRIPTION=true` | ⚠️ advanced/dev only (see Notes) |

   Also add `OPENAI_API_KEY=<key>` if OpenAI-based blocks will be used (it also serves as
   the OpenRouter fallback). AI blocks read `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` directly.
3. **Link the group to both LLM consumers:** the **rest-server** web service (copilot chat)
   and the **executor Workflow** (AI blocks). For each: service/Workflow → Environment →
   Link Environment Group → `autogpt-platform-llm` → save & redeploy.
4. Because both services share the group, **the same key value is read by rest-server and
   the Workflow — you enter it exactly once.**

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
