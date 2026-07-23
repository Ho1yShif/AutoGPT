"""App-side per-user execution rate limiting for the Workflows path.

Render Workflows has no native per-user concurrency cap, so we reimplement the
RabbitMQ consumer's `max_concurrent_graph_executions_per_user` gate with a
Redis-backed concurrency pool (`try_acquire_concurrency_slot`). A slot is keyed
by `graph_exec_id`, so resuming/requeueing the SAME execution refreshes its
slot rather than consuming a new one. Crashed holders are reclaimed by the
pool's stale-sweep, so a leaked slot cannot permanently consume capacity.

Enforced at DISPATCH time (`add_graph_execution`, workflows branch): over-cap
runs are rejected with `ExecutionRateLimitError` rather than silently delayed —
the honest app-side equivalent of the broker's requeue, given there is no
queue to hold them.
"""

import time

from backend.data import redis_client as redis
from backend.data.redis_helpers import SlotAdmission, try_acquire_concurrency_slot
from backend.util.settings import Settings
from backend.workflows.constants import RUN_ARTIFACT_TTL_SECONDS

settings = Settings()


class ExecutionRateLimitError(Exception):
    """Raised when a user is at their concurrent-execution cap (workflows path)."""


def _pool_key(user_id: str) -> str:
    # Hash-tag on user_id keeps the pool on one shard under cluster mode
    # (no-op on standalone Render Key Value) and colocates it for the Lua ZADD.
    return f"wf:run_slots:{{{user_id}}}"


async def acquire_run_slot(user_id: str, graph_exec_id: str) -> SlotAdmission:
    """Reserve a concurrency slot for the user.

    Returns the raw :class:`SlotAdmission` outcome so the caller can honor the
    release contract: only a caller that newly ``ADMITTED`` a slot owns its
    release. A ``REFRESHED`` result means the slot is already held by the
    still-running original run (e.g. a resume/requeue of the same
    ``graph_exec_id``) — that caller must NOT release it on dispatch failure,
    or it would ``zrem`` a slot the running execution still depends on and
    under-count the user. ``REJECTED`` means the pool was full.
    """
    now = time.time()
    r = await redis.get_redis_async()
    return await try_acquire_concurrency_slot(
        r,
        pool_key=_pool_key(user_id),
        slot_id=graph_exec_id,
        score=now,
        capacity=settings.config.max_concurrent_graph_executions_per_user,
        stale_before_score=now - RUN_ARTIFACT_TTL_SECONDS,
        ttl_seconds=RUN_ARTIFACT_TTL_SECONDS,
    )


async def release_run_slot(user_id: str, graph_exec_id: str) -> None:
    """Release the user's concurrency slot (async; e.g. dispatch-failure cleanup)."""
    r = await redis.get_redis_async()
    await r.zrem(_pool_key(user_id), graph_exec_id)


def release_run_slot_sync(user_id: str, graph_exec_id: str) -> None:
    """Release the user's concurrency slot (called in the sync task's finally)."""
    r = redis.get_redis()
    r.zrem(_pool_key(user_id), graph_exec_id)
