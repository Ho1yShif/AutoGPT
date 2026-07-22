# Fix Plan — `feat/render-template-deploy`

Handoff for the next agent. This lists the outstanding issues found in a clean-code
review of the branch, each with a **conventional solution** and concrete touchpoints.
Ordered by priority. Items 1–5 are correctness/security; 6–10 are hygiene; 11 is repo
cleanup.

All paths are relative to `autogpt_platform/backend/` unless noted. Follow
`backend/AGENTS.md` conventions (top-level imports, Pydantic over dict, `%s` in debug
logs, early returns). Every fix should ship with a test (TDD: write the failing test
first — see `backend/AGENTS.md` "Test-Driven Development").

---

## ⇢ HANDOFF STATUS (updated mid-execution)

**Code changes for items 1–7 and 10 are DONE and pass `ruff check` + `black`.**
Item 5A was already satisfied in the codebase (the `role`-dropped rationale is
documented in `util/feature_flag.py`). Item 11 is intentionally DEFERRED (destructive
file-deletion + git-history squash — do only at the final publish step, per the user).

| Item | Status | Notes |
|------|--------|-------|
| 1 rate_limit slot-release | ✅ done + tested | `acquire_run_slot` returns `SlotAdmission`; `_dispatch_via_workflows` only releases when `ADMITTED`. Tests in `executor/utils_test.py` (`test_refreshed_slot_not_released_on_dispatch_failure`, `test_admitted_slot_released_on_dispatch_failure`, `test_rejected_slot_raises_rate_limit_and_never_dispatches`) — verified red→green by reverting the `owns_slot` guard. |
| 2 skipped_no_entry leak | ✅ done + tested | release + `clear_cancel` added in `tasks.py` entry-is-None branch. Test in `workflows/tasks_test.py` (`test_skipped_no_entry_releases_slot_and_clears_cancel`). Added `workflows/conftest.py` so workflows unit tests run without the server stack (mirrors `util/conftest.py`). |
| 3 stop_graph_execution ownership | ✅ done + tested | ownership check added up front (before cascade/cancel), regardless of `wait_timeout`; `request_cancel` doc note added. Test `test_stop_graph_execution_rejects_unowned_before_cancel` — verified red→green by reverting the up-front check. |
| 4 render.yaml Redis + ENCRYPTION_KEY | ✅ done | `REDIS_URL` wired from Key Value `connectionString` on all 4 backend svcs + `redis_client.py` prefers it (standalone). `ENCRYPTION_KEY` moved OUT of the shared group to per-service `sync:false` (4 svcs); `encryption.py` now fails fast on a bad Fernet key + logs a non-secret boot fingerprint; README updated. **NOTE:** item 4A's original premise was wrong — Render Key Value internal is unauthenticated by default, so the old `REDIS_PASSWORD:""` was not actually broken; the `REDIS_URL` change makes it robust if internal auth is ever enabled. |
| 5A LD `role` dropped | ✅ pre-existing | already documented in `feature_flag.py`; no change needed. |
| 5B not-found LD caching | ✅ done + tested | `_fetch_db_user_context` now catches the not-found `ValueError` and returns a (cacheable) anonymous context; transient errors still propagate uncached. Test `feature_flag_test.py::TestUserContextCacheDegradation::test_not_found_user_context_is_cached`. |
| 6 concurrency-cap semantics | ✅ done | divergence documented on the `Config` field in `settings.py`. |
| 7 AGPT_SERVER_URL port | ✅ done (documented) | Blueprint can't concatenate scheme+hostport+`/api`; port↔`$PORT` coupling now spelled out in render.yaml. **Fuller fix (frontend reads bare `hostport`, builds URL in app config) NOT done — needs a frontend decision.** |
| 8 render_sdk in Poetry | ✅ done | Poetry can't capture it — `render_sdk 0.7.0` → `openapi-python-client 0.26.x` hard-pins `ruff<0.14`, irreconcilable with repo `ruff ^0.15` (confirmed against PyPI). Applied the plan's documented fallback: pin + both artifact hashes centralized in `backend/render_sdk.requirements.txt`; Dockerfile installs `--no-deps --require-hashes -r render_sdk.requirements.txt` (reproducible + tamper-evident). |
| 9 manager `__all__` cleanup | ✅ done | 4 external importers migrated to `backend.executor.engine` (`blocks/orchestrator.py`, `blocks/helpers/review.py`, `api/features/admin/execution_analytics_routes.py`, `executor/automod/manager.py`); `manager.py` engine import trimmed to the 6 symbols it uses internally; `__all__` removed; stale docstring xref in `copilot/executor/processor.py` repointed to `engine`. |
| 10 typing + redis dedup | ✅ done | `edb` typed via `_RenderRunIdSetter` Protocol; `redis_client.py` shared `common` kwargs + `_env_bool` helper. |
| 11 remove internal docs | ⛔ DEFERRED | final publish step only. |

### What the next agent must still do
1. **Full `poetry run test`** was NOT run here — this environment has no
   `autogpt_platform/.env` and the docker infra stack (postgres/redis/rabbitmq) is not
   provisioned, so the session `server` fixture (autouse `graph_cleanup`) can't start.
   Run the full suite in a properly provisioned environment before publish. What WAS
   verified locally:
   - `ruff check` + `black --check` clean on all 11 changed files.
   - Items 2 & 5B tests pass under their lightweight conftests (`workflows/`, `util/`).
   - Items 1 & 3 tests (in `executor/utils_test.py`, which has no server-fixture
     override) were verified via a **temporary** `executor/conftest.py` override
     (since removed): confirmed green on the fixed code and red after reverting each
     fix. To re-run them standalone, temporarily add the same `server`/`graph_cleanup`
     no-op overrides to `executor/conftest.py`, or just run the full docker-backed suite.
   - `render blueprints validate` still only YAML-parse-confirmed (CLI not run).
2. Item 11 at publish time only.

**Files changed (this session, items 1–3/5B tests + 8 + 9):**
`executor/utils_test.py` (new tests), `workflows/tasks_test.py` (new),
`workflows/conftest.py` (new), `util/feature_flag_test.py` (new 5B test),
`executor/manager.py` (trimmed engine import + removed `__all__`),
`blocks/orchestrator.py`, `blocks/helpers/review.py`,
`api/features/admin/execution_analytics_routes.py`, `executor/automod/manager.py`,
`copilot/executor/processor.py` (import/docstring migrations),
`backend/render_sdk.requirements.txt` (new, hash-pinned), `backend/Dockerfile`.

**Files changed in prior sessions (items 1–7, 10 code):** `workflows/rate_limit.py`,
`workflows/tasks.py`, `workflows/cancel.py`, `executor/utils.py`, `util/settings.py`,
`util/feature_flag.py`, `util/encryption.py`, `data/redis_client.py`, plus repo-root
`render.yaml` and `README.md`.

---

---

## 1. `workflows/rate_limit.py` — slot-release contract is broken

**Problem.** `acquire_run_slot` (line 36) collapses the three-way `SlotAdmission`
(`ADMITTED` / `REFRESHED` / `REJECTED`) into `admission != REJECTED` and returns a bool.
The caller `_dispatch_via_workflows` (`executor/utils.py`) releases the slot in its
`except BaseException` on **any** dispatch failure. When a resume/requeue of an
already-running `graph_exec_id` takes the `REFRESHED` path (the slot is owned by the
still-running original run) and dispatch then fails, the cleanup `zrem`s a slot that the
running execution still depends on — under-counting the user and letting them exceed
`max_concurrent_graph_executions_per_user`.

Only the caller that **newly `ADMITTED`** a slot owns its release — this is exactly the
contract documented on `SlotAdmission` in `data/redis_helpers.py:324`.

**Conventional solution.** Preserve the admission outcome across the boundary; release
only when newly admitted.

- In `rate_limit.py`, return the outcome instead of a bool:
  ```python
  async def acquire_run_slot(user_id: str, graph_exec_id: str) -> SlotAdmission:
      ...
      return admission  # ADMITTED | REFRESHED | REJECTED
  ```
- In `executor/utils.py` `_dispatch_via_workflows`, branch on it and record whether this
  call owns the slot:
  ```python
  admission = await wf_rate_limit.acquire_run_slot(user_id, graph_exec_id)
  if admission == SlotAdmission.REJECTED:
      raise wf_rate_limit.ExecutionRateLimitError(...)
  owns_slot = admission == SlotAdmission.ADMITTED
  try:
      ...
  except BaseException:
      if owns_slot:  # never release a slot the running original owns
          await wf_rate_limit.release_run_slot(user_id, graph_exec_id)
      raise
  ```
- Import `SlotAdmission` from `backend.data.redis_helpers` at top level of `utils.py`.

**Test.** Simulate `try_acquire_concurrency_slot` returning `REFRESHED`, force dispatch to
raise, assert `release_run_slot` is **not** called (mock/spy on `zrem` or the release fn).
Add a companion test for the `ADMITTED` path asserting release **is** called.

---

## 2. `workflows/tasks.py:64` — concurrency slot leaked on `skipped_no_entry`

**Problem.** The `try/finally` that releases the slot, clears the cancel flag, and
deletes the entry starts at ~line 90. The `entry is None` early return
(`return {"graph_exec_id": ..., "status": "skipped_no_entry"}`, line 64) happens **before**
it. When the entry blob expired or was never written, the slot reserved at dispatch is
never released and is only reclaimed by the 25h stale-sweep — a user silently loses a
concurrency slot for a day.

**Conventional solution.** Release the slot (and clear the cancel flag) in the
`skipped_no_entry` branch before returning. The `skipped_locked` branch (lines ~76–81)
is correct as-is — the *owning* run keeps its slot.

```python
if entry is None:
    logger.error(...)
    rate_limit.release_run_slot_sync(user_id, graph_exec_id)
    cancel_mod.clear_cancel(graph_exec_id)
    return {"graph_exec_id": graph_exec_id, "status": "skipped_no_entry"}
```

Alternatively, restructure so slot acquisition/release brackets the whole task body via a
context manager — but the minimal targeted release above is the low-risk fix.

**Test.** Call `run_graph_execution` with no stored entry (mock
`entry_store.load_execution_entry_sync -> None`); assert `release_run_slot_sync` was
called and the return status is `skipped_no_entry`.

---

## 3. `executor/utils.py` `stop_graph_execution` — cancel lacks an ownership check

**Problem.** `request_cancel` (workflows path) / the RabbitMQ fan-out publish both fire
**before** any `user_id` ownership verification, and when `wait_timeout` is falsy the
function returns immediately afterward — never validating that `user_id` owns
`graph_exec_id`. The only ownership-scoped read
(`db.get_graph_execution_meta(execution_id=..., user_id=user_id)`) lives inside the wait
loop, which is skipped when `wait_timeout` is 0/None. A caller passing an arbitrary
`graph_exec_id` can terminate another tenant's execution.

This shape is **pre-existing** on the RabbitMQ path; the new workflows path inherits it.
Fixing it in `stop_graph_execution` closes both.

**Conventional solution.** Verify ownership *first*, before emitting any cancel signal,
regardless of `wait_timeout`. Reuse the existing lookup:

```python
graph_exec = await db.get_graph_execution_meta(
    execution_id=graph_exec_id, user_id=user_id
)
if not graph_exec:
    raise NotFoundError(f"Graph execution #{graph_exec_id} not found.")
# ...only now emit the cancel signal (request_cancel / publish)...
```

Do this once up front and reuse the result in the wait loop. Also document on
`workflows/cancel.request_cancel` that callers MUST authorize the `graph_exec_id` first
(the primitive itself is unauthenticated by design).

**Test.** Call `stop_graph_execution(user_id="other", graph_exec_id=<not owned>,
wait_timeout=0)`; assert it raises `NotFoundError` and that `request_cancel` / the queue
publish was **never** invoked.

---

## 4. `render.yaml` — insecure Redis + non-Fernet encryption key

**Problem A — empty Redis password.** `REDIS_PASSWORD value: ""` on all four backend
services (rest, ws, scheduler, database-manager). Render Key Value provisions an AUTH
password even on the private network, so this either connects unauthenticated or fails
AUTH.

**Solution A.** Wire the credential from the Key Value service instead of hardcoding an
empty string. Prefer a single connection string:
```yaml
- key: REDIS_URL
  fromService:
    name: autogpt-platform-keyvalue
    type: keyvalue
    property: connectionString   # embeds host, port, and password
```
If the app requires split host/port, additionally pull `property: password` rather than
`value: ""`. Confirm the app reads whichever var you wire.

**Problem B — `ENCRYPTION_KEY: generateValue: true`.** `ENCRYPTION_KEY` is a Fernet key:
exactly 32 url-safe-base64 bytes (44 chars ending `=`). Render's `generateValue` emits a
random alphanumeric string not guaranteed to be a valid Fernet key, so credential
decryption can fail on first boot. The in-file comment already admits the value must be
verified.

**Solution B.** Make it deployer-supplied instead of auto-generated:
```yaml
- key: ENCRYPTION_KEY
  sync: false   # deployer pastes output of `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
```
Document the generation command in `README.md` deploy steps.

**Test/verify.** Post-deploy smoke: a service that decrypts a stored credential starts
without an `InvalidToken`/AUTH error. Add a note to the deploy runbook.

---

## 5. `util/feature_flag.py` — two silent behavior changes

**Problem A — `role` attribute dropped.** The LaunchDarkly context no longer sets
`role` / `custom.role`. Any LD flag rule that targets on `role` now evaluates differently
for every user, silently. (Admin *authorization* is unaffected — that is JWT-based — but
flag *targeting* is.)

**Solution A.** Before shipping, audit LD flag rules for any `role` targeting. If none
exist, add a code comment recording that `role` was intentionally dropped (GoTrue no
longer supplies it) so the omission is not "fixed" back later. If targeting exists,
re-populate `role` from the appropriate GoTrue/user lookup.

**Problem B — not-found users are no longer cached.** `get_user_by_id` now raises
`ValueError` for a missing user; the caller falls to a degraded anonymous path that is
deliberately **not** cached. A valid-UUID-but-deleted user id therefore re-hits the DB on
every flag evaluation, unbounded.

**Solution B.** Distinguish "not found" (a stable, cacheable anonymous result) from
"lookup failed" (transient — don't cache). Catch the not-found case explicitly and cache
the anonymous context for it; only skip caching on genuine transient errors.

**Test.** (A) build a context for a user and assert whether `role` is present matches the
intended decision. (B) evaluate a flag twice for a deleted-but-valid UUID and assert the
user lookup runs at most once (mock/spy on `get_user_by_id`).

---

## 6. Concurrency cap has different semantics per backend

**Problem.** Same config knob, two meanings:
- RabbitMQ (`executor/manager.py:376`): counts `RUNNING` executions **per (user_id,
  graph_id)** at **consume** time.
- Workflows (`workflows/rate_limit.acquire_run_slot`): a **per-user global** slot at
  **dispatch** time.

`max_concurrent_graph_executions_per_user` thus means different things depending on
`EXECUTION_BACKEND`.

**Conventional solution.** Pick one semantic and make both paths call a shared limiter so
the cap is backend-independent. Decide explicitly whether the limit is per-user or
per-(user, graph) and document it on the `Config` field in `util/settings.py`. Lowest-risk
first step: align the semantics and add a docstring; unifying the implementation behind
one helper is the fuller fix.

---

## 7. `render.yaml:454` — hardcoded internal port for `AGPT_SERVER_URL`

**Problem.** `AGPT_SERVER_URL value: http://rest-server:10000/api` hardcodes `:10000`,
while the sibling `GOTRUE_INTERNAL_URL` correctly derives host/port via
`fromService … property: hostport`. If rest-server's `$PORT` ever differs, SSR silently
breaks.

**Conventional solution.** Derive it the same way as GoTrue:
```yaml
- key: AGPT_SERVER_URL
  fromService:
    name: rest-server
    type: web
    property: hostport      # -> "rest-server:10000"
# then prefix scheme + /api suffix in app config, or use a small wrapper var
```
If the platform can't concatenate `/api` in-Blueprint, document that the port must track
rest-server's `$PORT`.

---

## 8. `backend/Dockerfile:173` — `render_sdk` installed outside Poetry

**Problem.** `RUN poetry run pip install --no-deps render_sdk==0.7.0` installs a runtime
dependency outside the lockfile: unlocked (no hash pinning), invisible to `poetry.lock`,
bypasses resolution. The comment justifies it (a ruff conflict via
`openapi-python-client`) and pins the version, but a reviewer will object to un-locked
prod deps in the image.

**Conventional solution.** Move `render_sdk` into a dedicated Poetry dependency group
with the conflicting transitive deps constrained/excluded, so it's captured in
`poetry.lock`:
```toml
[tool.poetry.group.workflows.dependencies]
render_sdk = "0.7.0"
```
If a hard resolver conflict truly blocks that, keep the `pip` install but track the pin in
one documented place and add a hash. Behavior-preserving at runtime either way.

---

## 9. `executor/manager.py` — dead re-exports in `__all__`

**Problem.** `__all__` re-exports 17 engine symbols "for backward compatibility," but ~6
(`increment_execution_count`, `synchronized`, `update_graph_execution_state`,
`update_node_execution_status`, `send_execution_update`,
`async_update_graph_execution_state`) have no remaining importers — internal or external.
Only 4 are imported via `backend.executor.manager` externally (`ExecutionProcessor`,
`async_update_node_execution_status`, `get_db_async_client`,
`send_async_execution_update`).

**Conventional solution.** Migrate the ~4 external call sites (`orchestrator.py`,
`blocks/helpers/review.py`, `admin/execution_analytics_routes.py`,
`automod/manager.py`) to import directly from `backend.executor.engine`, then shrink
`__all__` to only what's genuinely still re-exported. Low priority (compat shim).

---

## 10. Minor: untyped param + duplicated builder kwargs

- **`executor/utils.py` `_dispatch_via_workflows(edb)`** — `edb` is untyped and is passed
  either the `execution` module or a `DatabaseManagerAsyncClient` (structural duck-typing).
  Annotate it (e.g. a `Protocol` exposing `set_render_run_id`, or the concrete client type
  under `TYPE_CHECKING`) so the interface is explicit.
- **`data/redis_client.py`** — `connect()` / `connect_async()` duplicate ~8 identical
  kwargs across the standalone vs cluster branches; only `host/port` vs
  `startup_nodes`+`address_remap` differ. Extract a shared `common` kwargs dict, keeping
  the per-branch comments (esp. the async redis-py 6.x `retry` note) intact.
- **`data/redis_client.py`** — env-bool parsing is inconsistent: `USE_ANNOUNCED_ADDRESS`
  uses `in ("1","true","yes")` while `CLUSTER_MODE` uses `not in ("0","false","no")`.
  Introduce one `_env_bool(name, default)` helper and use it for both.

All behavior-preserving.

---

## 11. Remove root-level agent-coordination docs before publishing

**Problem.** `plan.md` (this file), `remaining.md`, `workflow.md`, `arch.md`, `AGENT.md`
at the repo root are internal handoff artifacts, not template documentation. For a
**public, forkable deploy template** they should not ship. In particular **`workflow.md`
documents the leaked-upstream-credential situation and the history-squash requirement** —
exactly the content that must never appear in a public repo.

**Conventional solution.** Delete these five files (or move them to an untracked
`docs/internal/` that's gitignored) as part of the pre-publish cleanup. This aligns with
the standing constraint: **squash the forked git history to a fresh no-ancestry history
before any push/publish**, since upstream history carries real-format leaked credentials.
Do the history squash and the file removal together in the final publish step.

**Verify.** After cleanup, `git log` shows no upstream ancestry and the working tree
contains only `render.yaml`, `README.md`, `.env.example`, and the application code.
