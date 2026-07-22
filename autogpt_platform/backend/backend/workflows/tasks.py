"""Render Workflows task definitions for graph execution.

One `@app.task run_graph_execution` == one graph run == one instance, mirroring
the RabbitMQ "one consumer per run" model. The task drives the same
broker-agnostic engine (`backend.executor.engine.ExecutionProcessor`) the
classic path uses.

Retries are DISABLED (`max_retries=0`): pre-flight billing (`charge_usage`) is
NOT idempotent for a node that was already RUNNING when a run died — the engine
re-enqueues in-flight nodes on resume and would re-charge them. A crashed run is
therefore recovered by a deliberate admin requeue (existing diagnostics path),
not by automatic retry. `timeout_seconds` matches the RabbitMQ 24h consumer
timeout.
"""

import logging
import threading
import uuid

from render_sdk import Retry, Workflows

from backend.data import redis_client as redis
from backend.executor.cluster_lock import ClusterLock
from backend.executor.engine import ExecutionProcessor
from backend.util.settings import Settings
from backend.workflows import cancel as cancel_mod
from backend.workflows import entry_store, rate_limit
from backend.workflows.constants import MAX_RUN_SECONDS, TASK_NAME

logger = logging.getLogger(__name__)
settings = Settings()

# Retries DISABLED: pre-flight billing is not idempotent for a node that was
# already RUNNING when a run died (the engine re-enqueues + re-charges it on
# resume). Recovery is a deliberate admin requeue, not an automatic retry.
_NO_RETRY = Retry(max_retries=0, wait_duration_ms=0)

app = Workflows(
    default_retry=_NO_RETRY,
    default_timeout=MAX_RUN_SECONDS,
    default_plan="standard",
)


@app.task(
    name=TASK_NAME,
    retry=_NO_RETRY,
    timeout_seconds=MAX_RUN_SECONDS,
)
def run_graph_execution(graph_exec_id: str, user_id: str) -> dict[str, str]:
    """Execute a single graph run dispatched via Render Workflows.

    Args are ids only (4 MB arg cap); the full `GraphExecutionEntry` is reloaded
    from Redis. Returns a small JSON-serializable summary for the Dashboard.
    """
    executor_id = str(uuid.uuid4())
    entry = entry_store.load_execution_entry_sync(graph_exec_id)
    if entry is None:
        logger.error(
            f"[Workflows] Missing stored entry for graph_exec_id={graph_exec_id}; "
            "cannot run (blob expired or never written)."
        )
        return {"graph_exec_id": graph_exec_id, "status": "skipped_no_entry"}

    # Idempotency guard: cluster-wide lock so a duplicate dispatch of the same
    # execution cannot run twice. The engine refreshes this lock during long
    # runs, so it stays held for the run's lifetime.
    cluster_lock = ClusterLock(
        redis=redis.get_redis(),
        key=f"exec_lock:{graph_exec_id}",
        owner_id=executor_id,
        timeout=settings.config.cluster_lock_timeout,
    )
    owner = cluster_lock.try_acquire()
    if owner != executor_id:
        logger.warning(
            f"[Workflows] Skipping {graph_exec_id}: already owned by {owner} "
            "(duplicate dispatch or Redis unavailable)."
        )
        return {"graph_exec_id": graph_exec_id, "status": "skipped_locked"}

    cancel_event = threading.Event()
    poller_stop = threading.Event()
    poller = cancel_mod.start_cancel_poller(graph_exec_id, cancel_event, poller_stop)

    processor = ExecutionProcessor()
    processor.on_graph_executor_start()

    try:
        # Cooperative: engine flips DB status (TERMINATED on cancel), cleans up
        # reviews, and cascades to children via stop_graph_execution.
        processor.on_graph_execution(entry, cancel_event, cluster_lock)
        status = "cancelled" if cancel_event.is_set() else "completed"
        return {"graph_exec_id": graph_exec_id, "status": status}
    finally:
        poller_stop.set()
        poller.join(timeout=5)
        cluster_lock.release()
        rate_limit.release_run_slot_sync(user_id, graph_exec_id)
        cancel_mod.clear_cancel(graph_exec_id)
        entry_store.delete_execution_entry_sync(graph_exec_id)
