# Deploy AutoGPT Platform on Render

Run the full [AutoGPT Platform](https://github.com/Significant-Gravitas/AutoGPT) — the
visual agent builder, marketplace, and execution engine — on [Render](https://render.com)
from a single `render.yaml` Blueprint. Managed Postgres and Key Value replace Supabase and
Redis, self-hosted GoTrue handles auth, ClamAV scans uploads, and Render Workflows run the
executor. No managed RabbitMQ, no managed Supabase, no hardcoded hosts.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Ho1yShif/AutoGPT)

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

| Resource                      | Type                  | Role                                                                      |
| ----------------------------- | --------------------- | ------------------------------------------------------------------------- |
| `autogpt-platform-db`         | Postgres 18           | App data (`platform` schema) + GoTrue's `auth` schema                     |
| `autogpt-platform-keyvalue`   | Key Value             | Locks, queues, pending-turn buffers, rate limits, cache (`noeviction`)    |
| `autogpt-platform-mv-refresh` | Cron                  | Refreshes store/suggested-block materialized views every 15 min           |
| `clamav`                      | Private (image)       | Virus scanning for uploads (raw TCP 3310)                                 |
| `autogpt-platform-gotrue`     | Private (image)       | Self-hosted Supabase Auth; reached only via the frontend `/auth/v1` proxy |
| `rest-server`                 | Web (Docker)          | FastAPI API; **sole owner of `prisma migrate deploy`**                    |
| `websocket-server`            | Web (Docker)          | WSS event fan-out via Redis pub/sub                                       |
| `scheduler-server`            | Private (Docker)      | APScheduler + RPC (`numInstances: 1`)                                     |
| `database-manager`            | Private (Docker)      | Centralized Prisma pool over RPC (scheduler's DB backend)                 |
| `frontend`                    | Web (Node)            | Next.js UI                                                                |
| **executor Workflow**         | **Workflow (manual)** | Runs agent graph executions; created in the Dashboard                     |

---

## Deploy

These are the click-by-click steps. Each one links into
**[Secrets & environment setup](#secrets--environment-setup)** below — the reference for what
every value is, where it lives, and how to generate it. Keep that section open alongside these
steps; the tables there are the source of truth.

> **The two things to prepare first:** the [executor Workflow](#manual-step--the-executor-workflow)
> (created by hand — the Blueprint needs its slug) and the
> [`autogpt-platform-byo-secrets` env group](#deployer-supplied-keys-bucket-3) (created before
> you apply). Because you make the Workflow first, its slug is known up front, so every value you
> supply — deploy secrets **and** LLM keys — goes into that one group. Everything else is entered
> in the Blueprint prompts or after the first apply.

1. Fork this repo and push it to your GitHub/GitLab account.
2. Create the executor Workflow shell ([part A](#manual-step--the-executor-workflow)) and note
   its slug. Its first deploy fails (no DB/Redis/JWT yet) — expected.
3. Create the `autogpt-platform-byo-secrets` env group (Dashboard → Env Groups → New) with
   `ENCRYPTION_KEY`, `RENDER_API_KEY`, the slug from step 2, and your LLM key(s). See
   [Deployer-supplied keys](#deployer-supplied-keys-bucket-3) and
   [LLM / Claude API keys](#manual-step--llm--claude-api-keys).
4. New → Blueprint, select your fork. Fill the phase-1 [per-service prompts](#per-service-prompts-bucket-4):
   `https://example.com` placeholders for the `NEXT_PUBLIC_*` frontend URLs and the anon key.
   (No SMTP needed — signups self-confirm by default.)
5. Apply. Services link to your group; Render auto-creates the `autogpt-platform-managed-secrets` group
   and `JWT_VERIFY_KEY`, and rest-server runs `prisma migrate deploy` on predeploy.

   > **First-deploy ordering.** Render brings all services up in parallel and does not order
   > them. `rest-server` is the sole owner of `prisma migrate deploy`, which creates the
   > `platform` schema. Any service that starts before that predeploy finishes can fail its
   > first deploy — most commonly **scheduler-server**, whose APScheduler jobstore tries to
   > create `platform.apscheduler_jobs` and errors with `schema "platform" does not exist`.
   > This is expected on the initial apply: once rest-server's migrate has completed, simply
   > **redeploy the failed service** (Manual Deploy → Deploy latest commit) and it goes green.

6. Set the real public URLs (phase 2) once services have their `*.onrender.com` hostnames, then
   redeploy the frontend and rest-server, then gotrue, ws, scheduler, and database-manager. See
   [per-service prompts](#per-service-prompts-bucket-4).
7. Finish the executor Workflow ([part B](#manual-step--the-executor-workflow)): now that
   Postgres, Key Value, and `JWT_VERIFY_KEY` exist, wire its env, link the
   `autogpt-platform-byo-secrets` group (it picks up `ENCRYPTION_KEY`, `RENDER_API_KEY`, and
   the LLM keys in one link), and deploy for real.

Optional, after the app is up: [require email verification](#require-email-verification) (SMTP)
and [enable Google login](#enable-google-login) — both off by default.

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
   `EXECUTION_BACKEND=workflows`, and `JWT_VERIFY_KEY` (copy from rest-server).
5. Link the `autogpt-platform-byo-secrets` group (Environment → Link Environment Group). That
   hands the Workflow `ENCRYPTION_KEY`, `RENDER_API_KEY`, and the LLM keys in one link — the same
   group values rest-server uses, so the boot fingerprints match by construction (no hand-paste).
6. Deploy for real. It should boot; graph execution works end-to-end.

### Manual step — LLM / Claude API keys

Two features need an LLM credential: copilot chat (`/api/chat/*`, on rest-server) and the AI
blocks (AI Text Generator, `claude_code`, `orchestrator`, on the executor Workflow). The
deploy succeeds with no key set — these features just return nothing until one is present.

These keys aren't in `render.yaml` (`sync: false` is invalid in a declared group, and the
Workflow isn't a Blueprint resource). They go in the same `autogpt-platform-byo-secrets` group
as the deploy secrets — one deployer-made group read by both consumers:

1. Add the keys to the `autogpt-platform-byo-secrets` group (the one you create in
   [step 3](#deploy) with the deploy secrets — don't add it to `render.yaml`). Pick one transport:
   - **OpenRouter** (default, recommended) — set `OPEN_ROUTER_API_KEY=<key>`; leave `CHAT_USE_OPENROUTER` unset or `true`. ✅ Works on Render.
   - **Direct Anthropic** — set `ANTHROPIC_API_KEY=<key>` and `CHAT_USE_OPENROUTER=false` (or use `CHAT_DIRECT_ANTHROPIC_API_KEY`). ✅ Works on Render.
   - **Subscription** (`claude login`) — set `CHAT_USE_CLAUDE_CODE_SUBSCRIPTION=true`. ⚠️ Advanced/dev only (see [Notes](#notes--tradeoffs)).
   - **OPENAI** - Add `OPENAI_API_KEY=<key>` too if OpenAI-based blocks are used (also the OpenRouter
     fallback). AI blocks read `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` directly.

2. Both consumers already read the group: rest-server (copilot chat) links it via `fromGroup` in
   `render.yaml`, and you link it to the executor Workflow (AI blocks) by hand in
   [Part B](#manual-step--the-executor-workflow). Nothing extra to wire — each key is entered once.

The unused `CHAT_*` / provider keys that also land on `database-manager` and `scheduler-server`
(they share the group via `fromGroup`) are harmless — see [Notes](#notes--tradeoffs).

---

## Secrets & environment setup

This is the reference behind the [Deploy](#deploy) steps above — what each value is, where it
lives, and when you set it. Every environment value falls into one of four buckets:

| #   | Bucket                                           | Where                   | Who sets it                | When                                 |
| --- | ------------------------------------------------ | ----------------------- | -------------------------- | ------------------------------------ |
| 1   | Automatic wires (`fromDatabase` / `fromService`) | each service            | Render, from `render.yaml` | at apply — nothing to do             |
| 2   | Env group `autogpt-platform-managed-secrets`     | Blueprint-managed group | Render (`generateValue`)   | at apply — never touched             |
| 3   | Env group `autogpt-platform-byo-secrets`         | group you create        | you, in the Dashboard      | **before apply**, after the Workflow |
| 4   | Per-service prompts (`sync: false`)              | each service            | you, in the Dashboard      | some at apply, some after URLs exist |

Buckets 3 and 4 need your input. The two env groups differ:

- `autogpt-platform-managed-secrets` — declared in `render.yaml`, auto-created, holds only
  `UNSUBSCRIBE_SECRET_KEY`. You never touch it.
- `autogpt-platform-byo-secrets` — you create it before applying with the deploy secrets
  (`ENCRYPTION_KEY`, `RENDER_API_KEY`, `RENDER_WORKFLOW_SLUG`) **and** the
  [LLM keys](#manual-step--llm--claude-api-keys); rest-server, database-manager, and
  scheduler-server link it via `fromGroup`, and you link it to the executor Workflow by hand, so
  each value is entered once. See [Deployer-supplied keys](#deployer-supplied-keys-bucket-3).

### What fans out and what doesn't

- Set once: everything in `autogpt-platform-byo-secrets` — the three deploy secrets and the
  LLM keys (via `fromGroup`) — plus `JWT_VERIFY_KEY` (`generateValue`
  on rest-server) and `FRONTEND_BASE_URL` (placeholder `value:` on rest-server), which other
  services pull via `fromService`.
- Per service: the remaining `sync: false` prompts — frontend URL keys and optional GoTrue keys.

The deploy secrets use a group, not `fromService`, because a `fromService` reference can't
resolve a `sync: false` source at apply (it rolls the apply back); a pre-created group's values
already exist, so `fromGroup` resolves. See [Notes](#notes--tradeoffs).

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

All deployer-supplied values go in one env group, `autogpt-platform-byo-secrets`, that you
create **before applying** the blueprint — every value is knowable ahead of time once the
[executor Workflow](#manual-step--the-executor-workflow) exists (create it first — you need its
slug). At apply, rest-server, database-manager, and scheduler-server link to it via `fromGroup`,
so each value is entered once.

| Key                    | Consumed by                                 | What to enter                                                                                                                                                                       |
| ---------------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ENCRYPTION_KEY`       | rest-server + database-manager (+ Workflow) | Fernet key for stored credentials — generate with `python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` ([details](#generating-encryption_key)) |
| `RENDER_API_KEY`       | rest-server + scheduler-server (+ Workflow) | Render workspace API key (Workflows dispatch) — Dashboard → Settings → API Keys                                                                                                     |
| `RENDER_WORKFLOW_SLUG` | rest-server + scheduler-server              | Executor Workflow slug — create the Workflow first so it's known before apply                                                                                                       |
| LLM keys               | rest-server + Workflow                      | Copilot chat + AI blocks — pick a transport in [LLM / Claude API keys](#manual-step--llm--claude-api-keys)                                                                          |

The manual Workflow can't use `fromGroup` (it isn't a blueprint resource), but it **can** link
the group by hand in the Dashboard ([Part B](#manual-step--the-executor-workflow)) — that's how
it picks up `ENCRYPTION_KEY`, `RENDER_API_KEY`, and the LLM keys without re-entering them, and it
guarantees its `ENCRYPTION_KEY` fingerprint matches rest-server's. `fromGroup` is all-or-nothing,
so database-manager and scheduler-server each receive every key in the group and use the subset
they need (the LLM/slug keys they ignore are harmless) — one group to manage instead of two.

### Per-service prompts (bucket 4)

The remaining `sync: false` prompts are entered per service, split by when their value is knowable.

Phase 1 — at apply, values you already control:

- Frontend URL keys — enter a valid `https://` placeholder (e.g. `https://example.com`); an
  empty or malformed value fails validation at boot
  (SMTP is not entered here — signups self-confirm by default. See
  [Require email verification](#require-email-verification) to turn it on.)

Phase 2 — after the first apply, values that need the `*.onrender.com` hostnames:

- Frontend: `NEXT_PUBLIC_AGPT_SERVER_URL`, `NEXT_PUBLIC_AGPT_WS_SERVER_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- rest-server: overwrite `FRONTEND_BASE_URL` with the real frontend origin
- gotrue: `GOTRUE_URI_ALLOW_LIST`

Then redeploy the frontend and rest-server, then gotrue, ws, scheduler, and database-manager
(they pull the new `FRONTEND_BASE_URL` on their next deploy). Derived vars
(`PLATFORM_BASE_URL`, `BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_FRONTEND_BASE_URL`, GoTrue's `GOTRUE_SITE_URL` / `API_EXTERNAL_URL`) are
not entered.

Full list of per-service prompts:

| Key                              | Service(s) | What to enter                                                                 |
| -------------------------------- | ---------- | ----------------------------------------------------------------------------- |
| `NEXT_PUBLIC_AGPT_SERVER_URL`    | frontend   | `https://<rest-server host>/api` (build-time)                                 |
| `NEXT_PUBLIC_AGPT_WS_SERVER_URL` | frontend   | `wss://<websocket-server host>/ws` (build-time)                               |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY`  | frontend   | Anon JWT signed with `JWT_VERIFY_KEY` (see [below](#generating-the-anon-key)) |
| `GOTRUE_URI_ALLOW_LIST`          | gotrue     | Allowed redirect URLs (e.g. `https://<frontend>/**`)                          |

Two GoTrue features are optional and off by default, so they aren't blueprint prompts —
turn them on after deploy if you want them:
[Require email verification](#require-email-verification) (SMTP) and
[Enable Google login](#enable-google-login) (OAuth).

`FRONTEND_BASE_URL` is not in this table — it's a placeholder `value:` on rest-server, not a
prompt (see [URL config layer](#url-config-layer)). Not prompted either: `PLATFORM_BASE_URL`,
`BACKEND_CORS_ALLOW_ORIGINS`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_FRONTEND_BASE_URL`,
GoTrue's `GOTRUE_SITE_URL` / `API_EXTERNAL_URL`, and every consumer's
`FRONTEND_BASE_URL` / `JWT_VERIFY_KEY` (all derived or pulled via `fromService`).

The authoritative source is [`render.yaml`](render.yaml) — each `sync: false` entry carries a
`# DEPLOYER:` comment and Render prompts for it at deploy. Local development doesn't use this
table; it runs from `.env.default` files via `make init-env` (see [`local.md`](local.md#environment-files)).

#### Require email verification

By default the blueprint sets `GOTRUE_MAILER_AUTOCONFIRM=true`, so signups self-confirm and the
app works with no email setup. The tradeoff: anyone can register with an address they don't own,
and password reset (which needs email) won't work. To require verified emails, add your own SMTP
relay (Render has no managed email) and flip the flag:

1. Open the **gotrue** service → **Environment** tab.
2. Add the six SMTP keys (values from your provider, e.g. Resend, Postmark, SendGrid, Mailgun):

   | Key                       | Example value                                    |
   | ------------------------- | ------------------------------------------------ |
   | `GOTRUE_SMTP_HOST`        | `smtp.resend.com`                                |
   | `GOTRUE_SMTP_PORT`        | `465` (SSL) or `587` (STARTTLS)                  |
   | `GOTRUE_SMTP_USER`        | provider username / API-key name                 |
   | `GOTRUE_SMTP_PASS`        | provider password / API key                      |
   | `GOTRUE_SMTP_SENDER_NAME` | `AutoGPT Platform`                               |
   | `GOTRUE_SMTP_ADMIN_EMAIL` | a verified sender, e.g. `noreply@yourdomain.com` |

3. Set `GOTRUE_MAILER_AUTOCONFIRM=false`.
4. **Save changes** — Render redeploys gotrue automatically.

The sender address (`GOTRUE_SMTP_ADMIN_EMAIL`) must be a domain/address your SMTP provider has
verified, or delivery is rejected. Once `AUTOCONFIRM=false` is live, new signups stall until they
click the emailed link — so confirm SMTP works before flipping it.

#### Enable Google login

Google OAuth is optional and off by default, so it's not a blueprint prompt. To turn it on
after deploy:

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create an
   **OAuth 2.0 Client ID** (type: Web application). Add authorized redirect URI
   `https://<gotrue host>.onrender.com/auth/v1/callback`.
2. On the **gotrue** service → **Environment** tab, add:

   | Key                                   | Value                                                 |
   | ------------------------------------- | ----------------------------------------------------- |
   | `GOTRUE_EXTERNAL_GOOGLE_ENABLED`      | `true`                                                |
   | `GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID`    | Client ID from Google                                 |
   | `GOTRUE_EXTERNAL_GOOGLE_SECRET`       | Client secret from Google                             |
   | `GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI` | `https://<gotrue host>.onrender.com/auth/v1/callback` |

3. Ensure the callback URL is also in `GOTRUE_URI_ALLOW_LIST`.
4. **Save changes** — gotrue redeploys with Google login enabled.

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

Put it in the `autogpt-platform-byo-secrets` group. Blueprint services pull it via `fromGroup`
and the Workflow reads the same group by linking it ([Part B](#manual-step--the-executor-workflow)),
so every consumer gets the identical value — no hand-copying. It can't be `generateValue` (may
emit chars Fernet rejects).

Each service with the key logs `ENCRYPTION_KEY loaded (fingerprint=<12 hex chars>)` at boot —
confirm rest-server, database-manager, and the Workflow print the same fingerprint; a mismatch
means decryption fails. A malformed key fails fast at startup, not as a runtime `InvalidToken`.

---

## Using the app

1. Open the frontend URL and **sign up**. Signups self-confirm by default (no email needed); if
   you've turned on [email verification](#require-email-verification), confirm via the emailed link.
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
- **Why the deploy secrets use a pre-made group:** `fromService … envVarKey` can't resolve a
  `sync: false` source at apply (errors with `environment variable not found` and rolls the
  apply back), so the three deployer secrets can't be fanned out from rest-server. A group
  created before apply holds them in the workspace, so `fromGroup` resolves cleanly and each is
  entered once. `JWT_VERIFY_KEY` (`generateValue`) and `FRONTEND_BASE_URL` (placeholder
  `value:`) still fan out via `fromService`. The group isn't declared in `render.yaml` (it's
  yours to own) — create it before you validate or apply so `fromGroup` resolves against it.
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
