"""Redis-backed store for the `GraphExecutionEntry` payload.

Render Workflows caps task arguments at 4 MB. A `GraphExecutionEntry` can
exceed that (large `nodes_input_masks`, big inputs), so on the workflows path
we NEVER pass the entry as a task argument. Instead the producer stashes the
serialized entry in Redis keyed by `graph_exec_id` and passes only the id +
user_id to `start_task`; the task reloads the entry here. The blob is read once
at run start (seconds after dispatch), well within the TTL.
"""

from typing import cast

from backend.data import redis_client as redis
from backend.data.execution import GraphExecutionEntry

# > 24h max run duration so the entry never disappears mid-dispatch; the blob
# is only actually read at run start, so this is a generous safety margin.
_ENTRY_TTL_SECONDS = 25 * 60 * 60


def _key(graph_exec_id: str) -> str:
    return f"wf:exec_entry:{graph_exec_id}"


def _decode(blob: str | bytes | None) -> str | None:
    if blob is None:
        return None
    return blob.decode() if isinstance(blob, bytes) else blob


async def store_execution_entry(entry: GraphExecutionEntry) -> None:
    r = await redis.get_redis_async()
    await r.set(
        _key(entry.graph_exec_id),
        entry.model_dump_json(),
        ex=_ENTRY_TTL_SECONDS,
    )


def load_execution_entry_sync(graph_exec_id: str) -> GraphExecutionEntry | None:
    """Sync variant used by the (sync) Workflows task at run start."""
    r = redis.get_redis()
    blob = _decode(cast("str | bytes | None", r.get(_key(graph_exec_id))))
    if blob is None:
        return None
    return GraphExecutionEntry.model_validate_json(blob)


def delete_execution_entry_sync(graph_exec_id: str) -> None:
    r = redis.get_redis()
    r.delete(_key(graph_exec_id))
