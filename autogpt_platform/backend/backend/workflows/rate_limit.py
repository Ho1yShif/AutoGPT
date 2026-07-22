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

settings = Settings()

# Stale-slot reclaim window: a slot whose holder crashed is swept after this
# many seconds. Matches the 24h max run duration + margin.
_SLOT_STALE_SECONDS = 25 * 60 * 60
_SLOT_TTL_SECONDS = 25 * 60 * 60


class ExecutionRateLimitError(Exception):
    """Raised when a user is at their concurrent-execution cap (workflows path)."""


def _pool_key(user_id: str) -> str:
    # Hash-tag on user_id keeps the pool on one shard under cluster mode
    # (no-op on standalone Render Key Value) and colocates it for the Lua ZADD.
    return f"wf:run_slots:{{{user_id}}}"


async def acquire_run_slot(user_id: str, graph_exec_id: str) -> bool:
    """Reserve a concurrency slot for the user. Returns False if at capacity."""
    now = time.time()
    r = await redis.get_redis_async()
    admission = await try_acquire_concurrency_slot(
        r,
        pool_key=_pool_key(user_id),
        slot_id=graph_exec_id,
        score=now,
        capacity=settings.config.max_concurrent_graph_executions_per_user,
        stale_before_score=now - _SLOT_STALE_SECONDS,
        ttl_seconds=_SLOT_TTL_SECONDS,
    )
    return admission != SlotAdmission.REJECTED


async def release_run_slot(user_id: str, graph_exec_id: str) -> None:
    """Release the user's concurrency slot (async; e.g. dispatch-failure cleanup)."""
    r = await redis.get_redis_async()
    await r.zrem(_pool_key(user_id), graph_exec_id)


def release_run_slot_sync(user_id: str, graph_exec_id: str) -> None:
    """Release the user's concurrency slot (called in the sync task's finally)."""
    r = redis.get_redis()
    r.zrem(_pool_key(user_id), graph_exec_id)
