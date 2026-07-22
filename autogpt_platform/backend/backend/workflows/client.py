"""Producer-side Render Workflows client.

Wraps the Render SDK so `add_graph_execution` / `stop_graph_execution` can
dispatch and cancel graph-execution task runs. `render_sdk` is imported lazily
so the RabbitMQ path never needs the dependency. The synchronous `Render`
client is run via `asyncio.to_thread` from the async producers to avoid
blocking the event loop.
"""

import asyncio
import logging

from backend.util.settings import Settings

logger = logging.getLogger(__name__)
settings = Settings()

TASK_NAME = "run_graph_execution"


def _task_slug() -> str:
    slug = settings.config.render_workflow_slug
    if not slug:
        raise RuntimeError(
            "RENDER_WORKFLOW_SLUG is not set but EXECUTION_BACKEND=workflows. "
            "Set it to the slug of the manually-created Render Workflow "
            "(task id '{slug}/run_graph_execution')."
        )
    return f"{slug}/{TASK_NAME}"


def _get_client():
    # Lazy import: only the workflows deployment / producers need render_sdk.
    from render_sdk import Render

    return Render()


async def dispatch_graph_execution(graph_exec_id: str, user_id: str) -> str:
    """Start a Render Workflows run for a graph execution; return its run id.

    The full `GraphExecutionEntry` is expected to already be stored in Redis
    (see `entry_store.store_execution_entry`); only ids are passed as task
    arguments to stay under the 4 MB argument cap.
    """
    slug = _task_slug()

    def _start() -> str:
        client = _get_client()
        run = client.workflows.start_task(slug, [graph_exec_id, user_id])
        return run.id

    run_id = await asyncio.to_thread(_start)
    logger.info(
        f"Dispatched graph execution {graph_exec_id} to Render Workflows "
        f"run_id={run_id}"
    )
    return run_id


async def cancel_graph_execution_run(render_run_id: str) -> None:
    """Best-effort HARD cancel of a Render Workflows run.

    Used only as a backstop after the cooperative Redis-flag path; failures are
    swallowed since the cooperative signal is the primary mechanism.
    """

    def _cancel() -> None:
        client = _get_client()
        client.workflows.cancel_task_run(render_run_id)

    try:
        await asyncio.to_thread(_cancel)
        logger.info(f"Requested hard cancel of Render Workflows run {render_run_id}")
    except Exception as e:
        logger.warning(
            f"Best-effort cancel_task_run({render_run_id}) failed: "
            f"{type(e).__name__}: {e}"
        )
