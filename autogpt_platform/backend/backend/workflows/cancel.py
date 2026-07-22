"""Cooperative cancellation for the Render Workflows path.

Cancellation of a graph run is COOPERATIVE (verified against the engine): the
running executor flips the DB status to TERMINATED, cancels pending reviews,
and cascades to children. On the RabbitMQ path the cancel signal arrives via a
FANOUT queue that sets an in-process `threading.Event`. Render Workflows has no
equivalent broadcast, so we replace it with a polled Redis flag:

* `stop_graph_execution` (workflows branch) SETs the flag for the target
  execution (and, via its own recursion, for each child).
* The running task polls the flag (`cancel_poller`) and sets the engine's
  `threading.Event` when it appears — driving the exact same cooperative
  shutdown the broker path uses.

`cancel_task_run(render_run_id)` is used only as a best-effort HARD backstop
after the cooperative wait window; it kills the instance without the graceful
DB cleanup, so it is never the primary mechanism.
"""

import logging
import threading
import time

from backend.data import redis_client as redis
from backend.workflows.constants import RUN_ARTIFACT_TTL_SECONDS

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0


def _key(graph_exec_id: str) -> str:
    return f"wf:exec_cancel:{graph_exec_id}"


async def request_cancel(graph_exec_id: str) -> None:
    """Set the cooperative cancel flag (called by the producer/stop path).

    UNAUTHENTICATED by design: it flips a Redis flag for whatever
    ``graph_exec_id`` it's handed and performs no ownership check. Callers MUST
    authorize that the requesting user owns ``graph_exec_id`` first (see
    ``stop_graph_execution``, which verifies ownership before calling this).
    """
    r = await redis.get_redis_async()
    await r.set(_key(graph_exec_id), "1", ex=RUN_ARTIFACT_TTL_SECONDS)


def is_cancel_requested_sync(graph_exec_id: str) -> bool:
    r = redis.get_redis()
    return bool(r.exists(_key(graph_exec_id)))


def clear_cancel(graph_exec_id: str) -> None:
    r = redis.get_redis()
    r.delete(_key(graph_exec_id))


def start_cancel_poller(
    graph_exec_id: str,
    cancel_event: threading.Event,
    stop_event: threading.Event,
) -> threading.Thread:
    """Poll the Redis cancel flag; set `cancel_event` when it appears.

    Runs as a daemon thread for the lifetime of a single task run. `stop_event`
    is set by the caller once the run finishes so the poller exits promptly.
    """

    def _poll() -> None:
        while not stop_event.is_set() and not cancel_event.is_set():
            try:
                if is_cancel_requested_sync(graph_exec_id):
                    cancel_event.set()
                    return
            except Exception as e:
                # Never let a transient Redis hiccup kill the run; try again.
                # Leave a breadcrumb so a persistent failure (e.g. Redis down for
                # the whole run, silently disabling cancellation) is observable.
                logger.debug("cancel poll failed for %s: %s", graph_exec_id, e)
            time.sleep(_POLL_INTERVAL_SECONDS)

    thread = threading.Thread(target=_poll, daemon=True)
    thread.start()
    return thread
