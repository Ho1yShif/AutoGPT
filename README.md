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

Every environment value falls into exactly one of **four buckets**. Knowing the bucket
tells you *where* the value lives and *when* you set it. Two of the buckets are env groups.

### Overview

| # | Bucket | Where it lives | Who sets it | When |
|---|--------|----------------|-------------|------|
| 1 | **Automatic wires** | on each service, via `fromDatabase` / `fromService` | Render, from `render.yaml` | at apply — nothing to do |
| 2 | **Env group `autogpt-platform-secrets`** | the Blueprint-managed group | Render (`generateValue`) | at apply — never touched by you |
| 3 | **Per-service Dashboard prompts** (`sync: false`) | on the individual service | **you**, in the Dashboard | some at apply, some after URLs exist |
| 4 | **Env group `autogpt-platform-llm`** | a Dashboard-only group **you create** | **you** | last, after the Workflow exists |

There are **only two env groups**, and they are not interchangeable:

- **`autogpt-platform-secrets`** — declared in `render.yaml`, created automatically, holds
  only `UNSUBSCRIBE_SECRET_KEY` (`generateValue`). **You never add keys to it.**
- **`autogpt-platform-llm`** — **not** in `render.yaml`; you create it by hand in the
  Dashboard and link it to two consumers. It is the **only** group you touch. LLM keys go
  here (see [Manual step — LLM / Claude API keys](#manual-step--llm--claude-api-keys)).

Everything else is per-service: bucket 1 (wires) needs no action, bucket 3 (`sync: false`
prompts) is the list you actually fill in. **Do not** put bucket-3 keys in an env group —
`sync: false` is ignored inside groups, so a secret placed there silently becomes blank.

> Two shared secrets are **not** in the `autogpt-platform-secrets` group even though they
> look like they should be — a group can't hold them (`sync: false` is ignored inside
> groups, a literal group value is clobbered on every re-sync, and `generateValue` produces
> a value that isn't a valid Fernet key). Both instead live on **rest-server** as the single
> owner, and every other service pulls them via `fromService` (bucket 1):
> `JWT_VERIFY_KEY` (a `generateValue` on rest-server) and `ENCRYPTION_KEY` (deployer-supplied
> `sync: false` on rest-server — you enter it once; see bucket 3 below).

### The URL config layer — fewer keys to enter

Every service internally wants a handful of URL variables (`PLATFORM_BASE_URL`,
`BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_FRONTEND_BASE_URL`,
…), but almost all of them collapse to a few origins. `render.yaml` derives the rest in the
build/start commands so **you enter each origin at most once**:

- **Backend → one keyname: `FRONTEND_BASE_URL`** (the frontend's public origin, `F`), set
  once on `rest-server`. Its start command derives `PLATFORM_BASE_URL` from Render's
  auto-injected `RENDER_EXTERNAL_URL` (the API's own origin) and
  `BACKEND_CORS_ALLOW_ORIGINS` from `F`. `websocket-server`, `scheduler-server`, and
  `database-manager` pull `FRONTEND_BASE_URL` from `rest-server` via `fromService` — you
  never re-enter it.
- **Frontend → two keynames: `NEXT_PUBLIC_AGPT_SERVER_URL` (API, `R`) and
  `NEXT_PUBLIC_AGPT_WS_SERVER_URL` (WebSocket, `W`).** These are separate Render services,
  so they can't be derived. The frontend's *own* origin (used for
  `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_FRONTEND_BASE_URL`) is derived from its
  build-time `RENDER_EXTERNAL_URL`, so those two are **not** prompted for.

Each derived var uses `${VAR:-…}`, so if you later add a **custom domain** you can set the
real value in the Dashboard and it overrides the derived default.

### Set the deployer-supplied keys (bucket 3)

`sync: false` prompts split by *when their value is knowable*:

1. **Before / at apply — values you already control:** `ENCRYPTION_KEY` (generate once,
   enter it **only on rest-server** — ws/scheduler/db-manager pull it via `fromService`;
   the manual Workflow gets the same value pasted separately), `RENDER_API_KEY` (rest +
   scheduler), and SMTP (`GOTRUE_SMTP_*`) if you have it. For the URL-dependent keys below,
   enter a **valid `https://` placeholder** now (e.g. `https://example.com`) — an empty or
   malformed value fails URL/CORS validation at boot.
2. **After the first apply — values that need the `*.onrender.com` hostnames:** on the
   **frontend**, `NEXT_PUBLIC_AGPT_SERVER_URL` (`https://<rest-server host>/api`),
   `NEXT_PUBLIC_AGPT_WS_SERVER_URL` (`wss://<websocket-server host>/ws`), and
   `NEXT_PUBLIC_SUPABASE_ANON_KEY` (mint it from the now-generated `JWT_VERIFY_KEY`); on
   **rest-server**, `FRONTEND_BASE_URL` (the frontend origin — the single backend keyname);
   on **gotrue**, `GOTRUE_URI_ALLOW_LIST` (allowed redirect URLs). Then **redeploy the
   frontend and rest-server** (`NEXT_PUBLIC_*` are inlined at build; the backend derives its
   URLs at start), then **redeploy gotrue** (it pulls the new `FRONTEND_BASE_URL`).
   `PLATFORM_BASE_URL`, `BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`,
   `NEXT_PUBLIC_FRONTEND_BASE_URL`, and GoTrue's `GOTRUE_SITE_URL` /
   `GOTRUE_API_EXTERNAL_URL` are **derived — do not enter them**.
3. **After the Workflow exists:** `RENDER_WORKFLOW_SLUG` (rest + scheduler), then redeploy
   those two. See [Manual step — the executor Workflow](#manual-step--the-executor-workflow).

### The full deployer-supplied list (bucket 3)

These live on individual services and are required to enter in the Dashboard at deploy time. Other environment variables are either optional or added in a later step.

| Key | Service(s) | What to enter |
|-----|-----------|---------------|
| `ENCRYPTION_KEY` | rest-server (+ **Workflow**) | Fernet key for stored credentials — **enter once on rest-server**; ws/scheduler/db-manager pull it via `fromService`. Generate with: `python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` and paste the **same value** into the manual Workflow too |
| `RENDER_API_KEY` | rest-server, scheduler-server | Render workspace API key (Workflows dispatch) |
| `RENDER_WORKFLOW_SLUG` | rest-server, scheduler-server | Slug of the manual executor Workflow — **unknown until it exists**; set + redeploy |
| `FRONTEND_BASE_URL` | rest-server | Frontend public origin — **the single backend URL keyname**; ws/scheduler/db-manager pull it via `fromService`, and `PLATFORM_BASE_URL` + `BACKEND_CORS_ALLOW_ORIGINS` are derived from it |
| `NEXT_PUBLIC_AGPT_SERVER_URL` | frontend | `https://<rest-server host>/api` (build-time) |
| `NEXT_PUBLIC_AGPT_WS_SERVER_URL` | frontend | `wss://<websocket-server host>/ws` (build-time) |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | frontend | Anon JWT signed with `JWT_VERIFY_KEY` (see below) |
| `GOTRUE_URI_ALLOW_LIST` | gotrue | Allowed redirect URLs (e.g. `https://<frontend>/**`). `GOTRUE_SITE_URL` + `GOTRUE_API_EXTERNAL_URL` are **not** prompted — they pull `FRONTEND_BASE_URL` from rest-server |
| `GOTRUE_SMTP_*` | gotrue | SMTP host/port/user/pass/sender/admin (email confirm, reset, change) |
| `GOTRUE_EXTERNAL_GOOGLE_*` | gotrue | Optional Google OAuth (leave `ENABLED=false` to skip) |

**Derived / wired — not prompted for** (see [The URL config layer](#the-url-config-layer--fewer-keys-to-enter)):
`PLATFORM_BASE_URL` and `BACKEND_CORS_ALLOW_ORIGINS` (from `FRONTEND_BASE_URL` +
`RENDER_EXTERNAL_URL`), `NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_FRONTEND_BASE_URL` (from
the frontend's own `RENDER_EXTERNAL_URL`), and GoTrue's `GOTRUE_SITE_URL` /
`GOTRUE_API_EXTERNAL_URL` (pulled from rest-server's `FRONTEND_BASE_URL` via `fromService`).
`ENCRYPTION_KEY` on ws/scheduler/db-manager is likewise pulled from rest-server. Set the URL
vars in the Dashboard only to override the derived default for a custom domain.

The authoritative list of deployer-supplied values is [`render.yaml`](render.yaml) itself —
every one is a `sync: false` entry annotated with a `# DEPLOYER:` comment, and Render prompts
for them when you deploy the Blueprint. The table above is the human-readable summary.

> **Local development** does not use this table — it runs from the committed `.env.default`
> files via `make init-env`. See [`local.md`](local.md#environment-files).

#### Generating the anon key

`NEXT_PUBLIC_SUPABASE_ANON_KEY` is an HS256 JWT signed with `JWT_VERIFY_KEY`. Read that
value from **rest-server**'s environment after the first deploy (it's the service-level
`generateValue`, not an env-group value), then mint the token with stdlib — no dependencies:

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

The backend loads `ENCRYPTION_KEY` as a `cryptography.fernet.Fernet` key — it must be
url-safe base64 of exactly 32 bytes, which Render's `generateValue` does **not** guarantee
(it can emit `+`/`/` chars that Fernet rejects). So it is deployer-supplied. Generate **one**
key and enter it **only on `rest-server`** (the owner) — `websocket-server`,
`scheduler-server`, and `database-manager` pull that exact value via `fromService`, so there
is no second paste and no mismatch risk inside the Blueprint. The executor Workflow lives
outside the Blueprint, so paste the **same** value there by hand. A Fernet key is just
url-safe base64 of 32 random bytes, so stdlib produces one — no `cryptography` install
needed:

```bash
python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

(Equivalent to `cryptography.fernet.Fernet.generate_key()` if you happen to have that
package installed: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.)

Each backend service logs a non-secret fingerprint at boot —
`ENCRYPTION_KEY loaded (fingerprint=<12 hex chars>)`. Confirm every service prints the
**same** fingerprint; a mismatch means a service has a different key and credential
decryption will fail. If `ENCRYPTION_KEY` is malformed, the service fails fast at startup
with a clear error instead of an opaque runtime `InvalidToken`.

---

## Deploy

Follow the buckets from [Secrets & environment](#secrets--environment) in order:

1. **Fork** this repo and push it to your GitHub/GitLab account.
2. **Generate `ENCRYPTION_KEY` first** (see [below](#generating-encryption_key)) and grab
   your `RENDER_API_KEY` from Dashboard → Settings → API Keys. You'll paste these into the
   Blueprint prompts in the next step — having them ready avoids a second pass.
3. In Render, **New → Blueprint**, select your fork. Render reads `render.yaml` and prompts
   for every `sync: false` key (bucket 3).
4. **Fill the bucket-3 "phase 1" prompts** — `ENCRYPTION_KEY` (once, on rest-server only),
   `RENDER_API_KEY` (rest + scheduler), and SMTP if you have it. For the URL-dependent keys
   (`FRONTEND_BASE_URL`, the frontend `NEXT_PUBLIC_AGPT_*`, `GOTRUE_URI_ALLOW_LIST`, anon
   key) enter a **valid `https://` placeholder** now (e.g. `https://example.com`) — you
   can't know the hostnames yet, and an empty value fails URL/CORS validation at boot. Leave
   `RENDER_WORKFLOW_SLUG` as a placeholder. You are **not** prompted for the derived/wired
   vars (`PLATFORM_BASE_URL`, `BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`,
   `NEXT_PUBLIC_FRONTEND_BASE_URL`, `GOTRUE_SITE_URL`, `GOTRUE_API_EXTERNAL_URL`, or
   `ENCRYPTION_KEY` on the three consumer services).
5. **Apply.** Postgres, Key Value, GoTrue, ClamAV, the four backend services, the cron, and
   the frontend come up. Render auto-creates the `autogpt-platform-secrets` group (bucket 2)
   and the service-level `JWT_VERIFY_KEY`; `rest-server` runs `prisma migrate deploy` on
   predeploy.
6. **Set the real public URLs (bucket 3, phase 2).** Once services have their
   `*.onrender.com` hostnames (or your custom domains), set on the **frontend**:
   `NEXT_PUBLIC_AGPT_SERVER_URL`, `NEXT_PUBLIC_AGPT_WS_SERVER_URL`, and
   `NEXT_PUBLIC_SUPABASE_ANON_KEY` (mint from `JWT_VERIFY_KEY` — see below); on
   **rest-server**: `FRONTEND_BASE_URL` (the frontend origin); on **gotrue**:
   `GOTRUE_URI_ALLOW_LIST`. Then redeploy the **frontend** (its `NEXT_PUBLIC_*` are inlined
   at build) and **rest-server** (it derives `PLATFORM_BASE_URL` / `BACKEND_CORS_ALLOW_ORIGINS`
   at start), then **gotrue** and ws/scheduler/db-manager (they pick up `FRONTEND_BASE_URL`
   — and GoTrue its `GOTRUE_SITE_URL` / `GOTRUE_API_EXTERNAL_URL` — on their next deploy).
   `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_FRONTEND_BASE_URL` are derived from the
   frontend's own origin — leave them unset unless you use a custom domain.

Then do the two manual steps below **in order**: create the executor Workflow (sets
`RENDER_WORKFLOW_SLUG`, bucket 3 phase 3), then create the `autogpt-platform-llm` env group
(bucket 4) — it's created last because it links to the Workflow.

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
   deployer-generated `ENCRYPTION_KEY` you set on rest-server** (confirm the boot
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
   **`autogpt-platform-llm`**. This is **bucket 4 — the only env group you create by hand**
   (Dashboard-managed; do **not** add it to `render.yaml`).
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
