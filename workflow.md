# workflow.md — Operator instructions & remaining steps

Runbook for finishing and publishing the AutoGPT → Render deploy template. Read
`AGENT.md` for repo layout and `arch.md`/`plan.md` for design rationale.

## ⚠️ MUST DO BEFORE ANY PUBLISH — squash the history

The remote (`Ho1yShif/AutoGPT`) is a fork carrying AutoGPT's full ~8,811-commit upstream
history, which contains **real-format leaked upstream credentials** (1 OpenAI, 3 Google,
2 GitHub PATs — history-only, not in the working tree; a `sk-ant-api03-xxxxx` placeholder
is harmless). These are old public upstream keys, almost certainly already auto-revoked;
decision was **not** to report them externally.

**Gate:** before any Deploy-to-Render listing / clean template publish, the history MUST
be squashed to a fresh no-ancestry history (orphan branch or copy files into a brand-new
empty repo). Do NOT publish the forked history. Pushing the working branch to the
existing fork is fine (no new exposure — the history is already there), but the clean
template must not carry it.

## Decisions on record (do not relitigate)

- Postgres **18** (fall back to 16 only if extensions break — verified 18 works).
- **Fresh template deploy** — no user migration (GoTrue starts with an empty auth schema).
- **Minimal services** — copilot_executor, notification_server, platform_linking_manager,
  falkordb are OUT of scope. `database_manager` IS in scope (scheduler hard-requires it).
- ClamAV `pro` (4 GB) + 2 GB disk, image pinned `clamav/clamav-debian:1.4.5`.
- Auth = self-hosted GoTrue (HS256, single shared JWT secret; Kong dropped).
- Executor = Render Workflows behind `EXECUTION_BACKEND` flag; broker path preserved.
- `ENCRYPTION_KEY`: kept as `generateValue` — **verifying it is a valid Fernet key is the
  #1 post-deploy check** (must be url-safe base64 of 32 bytes; regenerate if backend
  rejects the first credential op).

## Remaining steps (in order)

1. **Push the branch** (if not done): `git push -u origin feat/render-template-deploy`.
   (This is a protected action — run it yourself or approve it.)
2. **Squash the history** — the gate above. Prerequisite to everything public.
3. **Create the executor Workflow manually in the Render Dashboard** (not in render.yaml):
   - New → Workflow, link the repo, same workspace + region (oregon).
   - Root Directory: `autogpt_platform/backend`
   - Build: `poetry install && poetry run pip install --no-deps render_sdk`
   - Start: `poetry run python -m backend.workflows.main`
   - Env: `DATABASE_URL`/`DIRECT_URL` (+ `?schema=platform` wrapper), `REDIS_HOST`/`PORT`,
     `REDIS_PASSWORD=""`, `REDIS_CLUSTER_MODE=false`, `EXECUTION_BACKEND=workflows`,
     `RENDER_API_KEY`, `RENDER_WORKFLOW_SLUG` (its own slug, for nested runs),
     `ENCRYPTION_KEY` (MUST match rest-server), `JWT_VERIFY_KEY`, secrets group + provider keys.
   - After deploy, copy the Workflow slug and set `RENDER_WORKFLOW_SLUG` + `RENDER_API_KEY`
     + `EXECUTION_BACKEND=workflows` on **rest-server AND scheduler-server**, then redeploy them.
4. **Fill `sync:false` / Dashboard secrets** (see README table): `RENDER_API_KEY`,
   `RENDER_WORKFLOW_SLUG`, `PLATFORM_BASE_URL`, `FRONTEND_BASE_URL`,
   `BACKEND_CORS_ALLOW_ORIGINS`, the frontend `NEXT_PUBLIC_*` (own origin + anon JWT), and
   GoTrue `GOTRUE_SITE_URL`/`_API_EXTERNAL_URL`/`_URI_ALLOW_LIST`/`_SMTP_*`/`_EXTERNAL_GOOGLE_*`.
   After services get hostnames, set the real public URLs and **rebuild the frontend**
   (NEXT_PUBLIC_* are build-time inlined).
5. **Deploy**: `/render-deploy` into shifra-workspace (`tea-d50tvuidbo4c73cahs30`).
6. **Quality bar**: run `/render-template-quality-bar` (full checklist).
7. **Verify**: read logs for EVERY service, confirm each is `live`; `/render-debug` loop
   on anything red → fix via PR → redeploy → re-read logs. Priority checks: ENCRYPTION_KEY
   Fernet validity (#1), migrations ran on rest-server only, private DNS resolves.

## Things this template intentionally does NOT include

- The executor Workflow (Dashboard-only).
- RabbitMQ is not fully removed (copilot_executor + notification_server still use it, both
  out of scope) — graph execution moved to Workflows, the broker services did not.
- Internal docs (`arch.md`, `plan.md`, `remaining.md`, `render.fragments/`) should be
  pruned when squashing for publish.
