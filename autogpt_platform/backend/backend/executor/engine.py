"""
Graph execution engine — broker-agnostic.

This module contains the stateful, resume-capable in-process execution engine
(`ExecutionProcessor`) and its DB/event-bus helper functions. It has NO
knowledge of RabbitMQ, Render Workflows, or any specific dispatch transport —
callers (the RabbitMQ `ExecutionManager` in `manager.py`, or the Render
Workflows task in `backend/workflows/`) construct a `GraphExecutionEntry`, a
`threading.Event` for cooperative cancellation, and a `ClusterLock`, then call
`execute_graph(...)` / `ExecutionProcessor.on_graph_execution(...)`.

Importing this module must NOT pull in `pika` / RabbitMQ.
"""

import asyncio
import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import Future
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Optional, TypeVar, cast

from prometheus_client import Gauge
from redis.asyncio.lock import Lock as AsyncRedisLock
from sentry_sdk.api import capture_exception as _sentry_capture_exception
from sentry_sdk.api import flush as _sentry_flush
from sentry_sdk.api import get_current_scope as _sentry_get_current_scope

from backend.blocks import get_block
from backend.blocks._base import BlockSchema
from backend.blocks.agent import AgentExecutorBlock
from backend.blocks.mcp.block import MCPToolBlock
from backend.data import redis_client as redis
from backend.data.block import BlockInput, BlockOutput, BlockOutputEntry
from backend.data.dynamic_fields import parse_execution_output
from backend.data.execution import (
    ExecutionContext,
    ExecutionQueue,
    ExecutionStatus,
    GraphExecution,
    GraphExecutionEntry,
    NodeExecutionEntry,
    NodeExecutionResult,
    NodesInputMasks,
)
from backend.data.graph import Link, Node
from backend.data.model import GraphExecutionStats, NodeExecutionStats
from backend.data.redis_helpers import incr_with_ttl_sync
from backend.executor.cost_tracking import log_system_credential_cost
from backend.integrations.creds_manager import IntegrationCredentialsManager
from backend.util import json
from backend.util.clients import (
    get_async_execution_event_bus,
    get_database_manager_async_client,
    get_database_manager_client,
    get_execution_event_bus,
)
from backend.util.decorator import (
    async_error_logged,
    async_time_measured,
    error_logged,
    time_measured,
)
from backend.util.exceptions import (
    GraphNotFoundError,
    InsufficientBalanceError,
    ModerationError,
    NotFoundError,
)
from backend.util.file import clean_exec_files
from backend.util.logging import TruncatedLogger, configure_logging
from backend.util.process import set_service_name
from backend.util.retry import func_retry, send_rate_limited_discord_alert
from backend.util.settings import Settings

from . import billing
from .activity_status_generator import generate_activity_status_for_execution
from .auto_credentials import acquire_auto_credentials
from .automod.manager import automod_manager
from .cluster_lock import ClusterLock
from .simulator import get_dry_run_credentials, prepare_dry_run, simulate_block
from .utils import (
    ExecutionOutputEntry,
    LogMetadata,
    NodeExecutionProgress,
    validate_exec,
)

if TYPE_CHECKING:
    from backend.data.db_manager import (
        DatabaseManagerAsyncClient,
        DatabaseManagerClient,
    )


_logger = logging.getLogger(__name__)
logger = TruncatedLogger(_logger, prefix="[GraphExecutor]")
settings = Settings()


active_runs_gauge = Gauge(
    "execution_manager_active_runs", "Number of active graph runs"
)
pool_size_gauge = Gauge(
    "execution_manager_pool_size", "Maximum number of graph workers"
)
utilization_gauge = Gauge(
    "execution_manager_utilization_ratio",
    "Ratio of active graph runs to max graph workers",
)


# Thread-local storage for ExecutionProcessor instances
_tls = threading.local()


def init_worker():
    """Initialize ExecutionProcessor instance in thread-local storage"""
    _tls.processor = ExecutionProcessor()
    _tls.processor.on_graph_executor_start()


def execute_graph(
    graph_exec_entry: "GraphExecutionEntry",
    cancel_event: threading.Event,
    cluster_lock: ClusterLock,
):
    """Execute graph using thread-local ExecutionProcessor instance"""
    processor: ExecutionProcessor = _tls.processor
    return processor.on_graph_execution(graph_exec_entry, cancel_event, cluster_lock)


T = TypeVar("T")


async def execute_node(
    node: Node,
    data: NodeExecutionEntry,
    execution_processor: "ExecutionProcessor",
    execution_stats: NodeExecutionStats | None = None,
    nodes_input_masks: Optional[NodesInputMasks] = None,
    nodes_to_skip: Optional[set[str]] = None,
) -> BlockOutput:
    """
    Execute a node in the graph. This will trigger a block execution on a node,
    persist the execution result, and return the subsequent node to be executed.

    Args:
        db_client: The client to send execution updates to the server.
        creds_manager: The manager to acquire and release credentials.
        data: The execution data for executing the current node.
        execution_stats: The execution statistics to be updated.

    Returns:
        The subsequent node to be enqueued, or None if there is no subsequent node.
    """
    user_id = data.user_id
    graph_exec_id = data.graph_exec_id
    graph_id = data.graph_id
    graph_version = data.graph_version
    node_exec_id = data.node_exec_id
    node_id = data.node_id
    node_block = node.block
    execution_context = data.execution_context
    creds_manager = execution_processor.creds_manager

    log_metadata = LogMetadata(
        logger=_logger,
        user_id=user_id,
        graph_eid=graph_exec_id,
        graph_id=graph_id,
        node_eid=node_exec_id,
        node_id=node_id,
        block_name=node_block.name,
    )

    if node_block.disabled:
        raise ValueError(f"Block {node_block.id} is disabled and cannot be executed")

    # Sanity check: validate the execution input.
    input_data, error = validate_exec(
        node, data.inputs, resolve_input=False, dry_run=execution_context.dry_run
    )
    if input_data is None:
        log_metadata.warning(f"Skip execution, input validation error: {error}")
        yield "error", error
        return

    # Re-shape the input data for agent block.
    # AgentExecutorBlock specially separate the node input_data & its input_default.
    if isinstance(node_block, AgentExecutorBlock):
        _input_data = AgentExecutorBlock.Input(**node.input_default)
        _input_data.inputs = input_data
        if nodes_input_masks:
            _input_data.nodes_input_masks = nodes_input_masks
        _input_data.user_id = user_id
        input_data = _input_data.model_dump()
    elif isinstance(node_block, MCPToolBlock):
        _mcp_data = MCPToolBlock.Input(**node.input_default)
        # Dynamic tool fields are flattened to top-level by validate_exec
        # (via get_input_defaults). Collect them back into tool_arguments.
        tool_schema = _mcp_data.tool_input_schema
        tool_props = set(tool_schema.get("properties", {}).keys())
        merged_args = {**_mcp_data.tool_arguments}
        for key in tool_props:
            if key in input_data:
                merged_args[key] = input_data[key]
        _mcp_data.tool_arguments = merged_args
        input_data = _mcp_data.model_dump()
    data.inputs = input_data

    # Execute the node
    input_data_str = json.dumps(input_data)
    input_size = len(input_data_str)
    log_metadata.debug("Executed node with input", input=input_data_str)

    # Create node-specific execution context to avoid race conditions
    # (multiple nodes can execute concurrently and would otherwise mutate shared state)
    execution_context = execution_context.model_copy(
        update={"node_id": node_id, "node_exec_id": node_exec_id}
    )

    # Inject extra execution arguments for the blocks via kwargs
    # Keep individual kwargs for backwards compatibility with existing blocks
    extra_exec_kwargs: dict = {
        "graph_id": graph_id,
        "graph_version": graph_version,
        "node_id": node_id,
        "graph_exec_id": graph_exec_id,
        "node_exec_id": node_exec_id,
        "user_id": user_id,
        "execution_context": execution_context,
        "execution_processor": execution_processor,
        "nodes_to_skip": nodes_to_skip or set(),
    }

    # For special blocks in dry-run, prepare_dry_run returns a (possibly
    # modified) copy of input_data so the block executes for real.  For all
    # other blocks it returns None -> use LLM simulator.
    # OrchestratorBlock uses the platform's simulation model + OpenRouter key
    # so no user credentials are needed.
    _dry_run_input: dict[str, Any] | None = None
    if execution_context.dry_run:
        _dry_run_input = prepare_dry_run(node_block, input_data)
    if _dry_run_input is not None:
        input_data = _dry_run_input

    # Check for dry-run platform credentials (OrchestratorBlock uses the
    # platform's OpenRouter key instead of user credentials).
    _dry_run_creds = get_dry_run_credentials(input_data) if _dry_run_input else None

    # Last-minute fetch credentials + acquire a system-wide read-write lock to prevent
    # changes during execution. ⚠️ This means a set of credentials can only be used by
    # one (running) block at a time; simultaneous execution of blocks using same
    # credentials is not supported.
    creds_locks: list[AsyncRedisLock] = []
    input_model = cast(type[BlockSchema], node_block.input_schema)

    # Handle regular credentials fields
    for field_name, input_type in input_model.get_credentials_fields().items():
        # Dry-run platform credentials bypass the credential store.
        # Keep the existing credential metadata so _execute's input_schema(**...)
        # doesn't fail on the required field.  If no metadata is present,
        # synthesize a minimal placeholder from the platform credentials.
        if _dry_run_creds is not None:
            if input_data.get(field_name) is None:
                input_data[field_name] = {
                    "id": _dry_run_creds.id,
                    "provider": _dry_run_creds.provider,
                    "type": _dry_run_creds.type,
                    "title": _dry_run_creds.title,
                }
            extra_exec_kwargs[field_name] = _dry_run_creds
            continue

        field_value = input_data.get(field_name)
        if not field_value or (
            isinstance(field_value, dict) and not field_value.get("id")
        ):
            # No credentials configured — nullify so JSON schema validation
            # doesn't choke on the empty default `{}`.
            input_data[field_name] = None
            continue  # Block runs without credentials

        credentials_meta = input_type(**field_value)
        # Write normalized values back so JSON schema validation also passes
        # (model_validator may have fixed legacy formats like "ProviderName.MCP")
        input_data[field_name] = credentials_meta.model_dump(mode="json")
        try:
            credentials, lock = await creds_manager.acquire(
                user_id, credentials_meta.id
            )
        except ValueError:
            # Credential was deleted or doesn't exist.
            # If the field has a default, run without credentials.
            if input_model.model_fields[field_name].default is not None:
                log_metadata.warning(
                    f"Credentials #{credentials_meta.id} not found, "
                    "running without (field has default)"
                )
                input_data[field_name] = None
                continue
            raise
        creds_locks.append(lock)
        extra_exec_kwargs[field_name] = credentials

    # Handle auto-generated credentials (e.g., from GoogleDriveFileInput)
    auto_extra_kwargs, auto_locks = await acquire_auto_credentials(
        input_model=input_model,
        input_data=input_data,
        creds_manager=creds_manager,
        user_id=user_id,
    )
    extra_exec_kwargs.update(auto_extra_kwargs)
    creds_locks.extend(auto_locks)

    output_size = 0

    # sentry tracking nonsense to get user counts for blocks because isolation scopes don't work :(
    scope = _sentry_get_current_scope()

    # save the tags
    original_user = scope._user
    original_tags = dict(scope._tags) if scope._tags else {}
    # Set user ID for error tracking
    scope.set_user({"id": user_id})

    scope.set_tag("graph_id", graph_id)
    scope.set_tag("node_id", node_id)
    scope.set_tag("block_name", node_block.name)
    scope.set_tag("block_id", node_block.id)
    for k, v in execution_context.model_dump().items():
        scope.set_tag(f"execution_context.{k}", v)

    try:
        if execution_context.dry_run and _dry_run_input is None:
            block_iter = simulate_block(node_block, input_data, user_id=user_id)
        else:
            block_iter = node_block.execute(input_data, **extra_exec_kwargs)

        async for output_name, output_data in block_iter:
            output_data = json.to_dict(output_data)
            output_size += len(json.dumps(output_data))
            log_metadata.debug("Node produced output", **{output_name: output_data})
            yield output_name, output_data
    except Exception as ex:
        # Only capture unexpected errors to Sentry, not user-caused ones.
        # Most ValueError subclasses here are expected (BlockExecutionError,
        # InsufficientBalanceError, plain ValueError for auth/disabled blocks, etc.)
        # but NotFoundError/GraphNotFoundError could indicate real platform issues.
        is_expected = isinstance(ex, ValueError) and not isinstance(
            ex, (NotFoundError, GraphNotFoundError)
        )
        if not is_expected:
            _sentry_capture_exception(error=ex, scope=scope)
            _sentry_flush()
        # Re-raise to maintain normal error flow
        raise
    finally:
        # Ensure all credentials are released even if execution fails
        for creds_lock in creds_locks:
            if (
                creds_lock
                and (await creds_lock.locked())
                and (await creds_lock.owned())
            ):
                try:
                    await creds_lock.release()
                except Exception as e:
                    log_metadata.error(f"Failed to release credentials lock: {e}")

        # Update execution stats
        if execution_stats is not None:
            execution_stats += node_block.execution_stats
            execution_stats.input_size = input_size
            execution_stats.output_size = output_size

        # Restore scope AFTER error has been captured
        scope._user = original_user
        scope._tags = original_tags


async def _enqueue_next_nodes(
    db_client: "DatabaseManagerAsyncClient",
    node: Node,
    output: BlockOutputEntry,
    user_id: str,
    graph_exec_id: str,
    graph_id: str,
    graph_version: int,
    log_metadata: LogMetadata,
    nodes_input_masks: Optional[NodesInputMasks],
    execution_context: ExecutionContext,
) -> list[NodeExecutionEntry]:
    async def add_enqueued_execution(
        node_exec_id: str, node_id: str, block_id: str, data: BlockInput
    ) -> NodeExecutionEntry:
        await async_update_node_execution_status(
            db_client=db_client,
            exec_id=node_exec_id,
            status=ExecutionStatus.QUEUED,
            execution_data=data,
        )
        return NodeExecutionEntry(
            user_id=user_id,
            graph_exec_id=graph_exec_id,
            graph_id=graph_id,
            graph_version=graph_version,
            node_exec_id=node_exec_id,
            node_id=node_id,
            block_id=block_id,
            inputs=data,
            execution_context=execution_context,
        )

    async def register_next_executions(node_link: Link) -> list[NodeExecutionEntry]:
        try:
            return await _register_next_executions(node_link)
        except Exception as e:
            log_metadata.exception(f"Failed to register next executions: {e}")
            return []

    async def _register_next_executions(node_link: Link) -> list[NodeExecutionEntry]:
        enqueued_executions = []
        next_output_name = node_link.source_name
        next_input_name = node_link.sink_name
        next_node_id = node_link.sink_id

        output_name, _ = output
        next_data = parse_execution_output(
            output, next_output_name, next_node_id, next_input_name
        )
        if next_data is None and output_name != next_output_name:
            return enqueued_executions
        next_node = await db_client.get_node(next_node_id)

        # Multiple node can register the same next node, we need this to be atomic
        # To avoid same execution to be enqueued multiple times,
        # Or the same input to be consumed multiple times.
        async with synchronized(f"upsert_input-{next_node_id}-{graph_exec_id}"):
            # Add output data to the earliest incomplete execution, or create a new one.
            next_node_exec, next_node_input = await db_client.upsert_execution_input(
                node_id=next_node_id,
                graph_exec_id=graph_exec_id,
                input_name=next_input_name,
                input_data=next_data,
            )
            next_node_exec_id = next_node_exec.node_exec_id
            await send_async_execution_update(next_node_exec)

            # Complete missing static input pins data using the last execution input.
            static_link_names = {
                link.sink_name
                for link in next_node.input_links
                if link.is_static and link.sink_name not in next_node_input
            }
            if static_link_names and (
                latest_execution := await db_client.get_latest_node_execution(
                    next_node_id, graph_exec_id
                )
            ):
                for name in static_link_names:
                    next_node_input[name] = latest_execution.input_data.get(name)

            # Apply node input overrides
            node_input_mask = None
            if nodes_input_masks and (
                node_input_mask := nodes_input_masks.get(next_node.id)
            ):
                next_node_input.update(node_input_mask)

            # Validate the input data for the next node.
            next_node_input, validation_msg = validate_exec(
                next_node, next_node_input, dry_run=execution_context.dry_run
            )
            suffix = f"{next_output_name}>{next_input_name}~{next_node_exec_id}:{validation_msg}"

            # Incomplete input data, skip queueing the execution.
            if not next_node_input:
                log_metadata.info(f"Skipped queueing {suffix}")
                return enqueued_executions

            # Input is complete, enqueue the execution.
            log_metadata.info(f"Enqueued {suffix}")
            enqueued_executions.append(
                await add_enqueued_execution(
                    node_exec_id=next_node_exec_id,
                    node_id=next_node_id,
                    block_id=next_node.block_id,
                    data=next_node_input,
                )
            )

            # Next execution stops here if the link is not static.
            if not node_link.is_static:
                return enqueued_executions

            # If link is static, there could be some incomplete executions waiting for it.
            # Load and complete the input missing input data, and try to re-enqueue them.
            for iexec in await db_client.get_node_executions(
                node_id=next_node_id,
                graph_exec_id=graph_exec_id,
                statuses=[ExecutionStatus.INCOMPLETE],
            ):
                idata = iexec.input_data
                ineid = iexec.node_exec_id

                static_link_names = {
                    link.sink_name
                    for link in next_node.input_links
                    if link.is_static and link.sink_name not in idata
                }
                for input_name in static_link_names:
                    idata[input_name] = next_node_input[input_name]

                # Apply node input overrides
                if node_input_mask:
                    idata.update(node_input_mask)

                idata, msg = validate_exec(
                    next_node, idata, dry_run=execution_context.dry_run
                )
                suffix = f"{next_output_name}>{next_input_name}~{ineid}:{msg}"
                if not idata:
                    log_metadata.info(f"Enqueueing static-link skipped: {suffix}")
                    continue
                log_metadata.info(f"Enqueueing static-link execution {suffix}")
                enqueued_executions.append(
                    await add_enqueued_execution(
                        node_exec_id=iexec.node_exec_id,
                        node_id=next_node_id,
                        block_id=next_node.block_id,
                        data=idata,
                    )
                )
            return enqueued_executions

    return [
        execution
        for link in node.output_links
        for execution in await register_next_executions(link)
    ]


class ExecutionProcessor:
    """
    This class contains event handlers for the process pool executor events.

    The main events are:
        on_graph_executor_start: Initialize the process that executes the graph.
        on_graph_execution: Execution logic for a graph.
        on_node_execution: Execution logic for a node.

    The execution flow:
        1. Graph execution request is added to the queue.
        2. Graph executor loop picks the request from the queue.
        3. Graph executor loop submits the graph execution request to the executor pool.
      [on_graph_execution]
        4. Graph executor initialize the node execution queue.
        5. Graph executor adds the starting nodes to the node execution queue.
        6. Graph executor waits for all nodes to be executed.
      [on_node_execution]
        7. Node executor picks the node execution request from the queue.
        8. Node executor executes the node.
        9. Node executor enqueues the next executed nodes to the node execution queue.
    """

    # Per-graph-execution state, populated by on_graph_execution.
    nodes_input_masks: Optional[NodesInputMasks] = None

    @async_error_logged(swallow=True)
    async def on_node_execution(
        self,
        node_exec: NodeExecutionEntry,
        node_exec_progress: NodeExecutionProgress,
        nodes_input_masks: Optional[NodesInputMasks],
        graph_stats_pair: tuple[GraphExecutionStats, threading.Lock],
        nodes_to_skip: Optional[set[str]] = None,
    ) -> NodeExecutionStats:
        log_metadata = LogMetadata(
            logger=_logger,
            user_id=node_exec.user_id,
            graph_eid=node_exec.graph_exec_id,
            graph_id=node_exec.graph_id,
            node_eid=node_exec.node_exec_id,
            node_id=node_exec.node_id,
            block_name=b.name if (b := get_block(node_exec.block_id)) else "-",
        )
        db_client = get_db_async_client()
        node = await db_client.get_node(node_exec.node_id)
        execution_stats = NodeExecutionStats()

        timing_info, status = await self._on_node_execution(
            node=node,
            node_exec=node_exec,
            node_exec_progress=node_exec_progress,
            stats=execution_stats,
            db_client=db_client,
            log_metadata=log_metadata,
            nodes_input_masks=nodes_input_masks,
            nodes_to_skip=nodes_to_skip,
        )
        if isinstance(status, BaseException):
            raise status

        execution_stats.walltime = timing_info.wall_time
        execution_stats.cputime = timing_info.cpu_time

        # Log platform cost + reconcile dynamic billing BEFORE graph/node stats
        # are aggregated and persisted — otherwise the reconciled delta never
        # lands in `graph_stats.cost` or the persisted node stats. RUN-only
        # blocks produce a zero delta; dynamic types (SECOND/ITEMS/COST_USD/
        # TOKENS) settle their post-flight charge or refund here. Dry runs
        # skip reconciliation so simulation never touches the user's wallet.
        # Reconcile on FAILED / TERMINATED too — partial work consumed real
        # provider tokens, and the pre-flight charge should be refunded down
        # to the actually-tracked usage rather than being absorbed wholesale
        # by the user.
        if status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TERMINATED,
        ):
            await log_system_credential_cost(
                node_exec=node_exec,
                block=node.block,
                stats=execution_stats,
                db_client=db_client,
            )
            if not node_exec.execution_context.dry_run:
                reconciled_delta, _ = await billing.charge_reconciled_usage(
                    node_exec=node_exec,
                    stats=execution_stats,
                    pre_flight_charge=node_exec.pre_flight_charge,
                )
                if reconciled_delta != 0:
                    execution_stats.reconciled_cost_delta += reconciled_delta

        graph_stats, graph_stats_lock = graph_stats_pair
        with graph_stats_lock:
            graph_stats.node_count += 1 + execution_stats.extra_steps
            graph_stats.nodes_cputime += execution_stats.cputime
            graph_stats.nodes_walltime += execution_stats.walltime
            graph_stats.cost += (
                execution_stats.cost + execution_stats.reconciled_cost_delta
            )
            if isinstance(execution_stats.error, Exception):
                graph_stats.node_error_count += 1

        node_error = execution_stats.error
        node_stats = execution_stats.model_dump()
        if node_error and not isinstance(node_error, str):
            node_stats["error"] = str(node_error) or node_stats.__class__.__name__

        await async_update_node_execution_status(
            db_client=db_client,
            exec_id=node_exec.node_exec_id,
            status=status,
            stats=node_stats,
        )
        await async_update_graph_execution_state(
            db_client=db_client,
            graph_exec_id=node_exec.graph_exec_id,
            stats=graph_stats,
        )

        # If the node failed because a nested tool charge raised IBE,
        # send the user notification so they understand why the run stopped.
        if status == ExecutionStatus.FAILED and isinstance(
            execution_stats.error, InsufficientBalanceError
        ):
            await billing.try_send_insufficient_funds_notif(
                node_exec.user_id,
                node_exec.graph_id,
                execution_stats.error,
                log_metadata,
            )

        return execution_stats

    @async_time_measured
    async def _on_node_execution(
        self,
        node: Node,
        node_exec: NodeExecutionEntry,
        node_exec_progress: NodeExecutionProgress,
        stats: NodeExecutionStats,
        db_client: "DatabaseManagerAsyncClient",
        log_metadata: LogMetadata,
        nodes_input_masks: Optional[NodesInputMasks] = None,
        nodes_to_skip: Optional[set[str]] = None,
    ) -> ExecutionStatus:
        status = ExecutionStatus.RUNNING

        async def persist_output(output_name: str, output_data: Any) -> None:
            await db_client.upsert_execution_output(
                node_exec_id=node_exec.node_exec_id,
                output_name=output_name,
                output_data=output_data,
            )
            if exec_update := await db_client.get_node_execution(
                node_exec.node_exec_id
            ):
                await send_async_execution_update(exec_update)

            node_exec_progress.add_output(
                ExecutionOutputEntry(
                    node=node,
                    node_exec_id=node_exec.node_exec_id,
                    data=(output_name, output_data),
                )
            )

        async def _drive_execution() -> None:
            async for output_name, output_data in execute_node(
                node=node,
                data=node_exec,
                execution_processor=self,
                execution_stats=stats,
                nodes_input_masks=nodes_input_masks,
                nodes_to_skip=nodes_to_skip,
            ):
                await persist_output(output_name, output_data)

        try:
            log_metadata.info(f"Start node execution {node_exec.node_exec_id}")
            await async_update_node_execution_status(
                db_client=db_client,
                exec_id=node_exec.node_exec_id,
                status=ExecutionStatus.RUNNING,
            )

            # Per-block wall-clock cap on `run`. Leaf compute blocks inherit
            # the default cap; coordination blocks (AgentExecutor, AutoPilot)
            # opt out by overriding `execution_timeout_seconds = None`. Their
            # sub-graphs and inner LLM calls have their own bounds, so the
            # outer cap would false-positive on legitimately long runs.
            block_timeout = node.block.execution_timeout_seconds
            if block_timeout is None:
                await _drive_execution()
            else:
                await asyncio.wait_for(_drive_execution(), timeout=block_timeout)

            log_metadata.info(f"Finished node execution {node_exec.node_exec_id}")
            status = ExecutionStatus.COMPLETED

        except asyncio.TimeoutError:
            block_timeout = node.block.execution_timeout_seconds
            stats.error = TimeoutError(
                f"Node execution exceeded {block_timeout}s wall-clock cap"
            )
            log_metadata.warning(
                f"Node execution {node_exec.node_exec_id} timed out after "
                f"{block_timeout}s — marking FAILED"
            )
            status = ExecutionStatus.FAILED

        except BaseException as e:
            stats.error = e

            if isinstance(e, ValueError):
                # Avoid user error being marked as an actual error.
                log_metadata.info(
                    f"Expected failure on node execution {node_exec.node_exec_id}: {type(e).__name__} - {e}"
                )
                status = ExecutionStatus.FAILED
            elif isinstance(e, Exception):
                # If the exception is not a ValueError, it is unexpected.
                log_metadata.exception(
                    f"Unexpected failure on node execution {node_exec.node_exec_id}: {type(e).__name__} - {e}"
                )
                status = ExecutionStatus.FAILED
            else:
                # CancelledError or SystemExit
                log_metadata.warning(
                    f"Interruption error on node execution {node_exec.node_exec_id}: {type(e).__name__}"
                )
                status = ExecutionStatus.TERMINATED

        finally:
            if status == ExecutionStatus.FAILED and stats.error is not None:
                await persist_output(
                    "error", str(stats.error) or type(stats.error).__name__
                )
        return status

    @func_retry
    def on_graph_executor_start(self):
        configure_logging()
        set_service_name("GraphExecutor")
        self.tid = threading.get_ident()
        self.creds_manager = IntegrationCredentialsManager()
        self.node_execution_loop = asyncio.new_event_loop()
        self.node_evaluation_loop = asyncio.new_event_loop()
        self.node_execution_thread = threading.Thread(
            target=self.node_execution_loop.run_forever, daemon=True
        )
        self.node_evaluation_thread = threading.Thread(
            target=self.node_evaluation_loop.run_forever, daemon=True
        )
        self.node_execution_thread.start()
        self.node_evaluation_thread.start()
        logger.info(f"[GraphExecutor] {self.tid} started")

    @error_logged(swallow=False)
    def on_graph_execution(
        self,
        graph_exec: GraphExecutionEntry,
        cancel: threading.Event,
        cluster_lock: ClusterLock,
    ):
        log_metadata = LogMetadata(
            logger=_logger,
            user_id=graph_exec.user_id,
            graph_eid=graph_exec.graph_exec_id,
            graph_id=graph_exec.graph_id,
            node_id="*",
            node_eid="*",
            block_name="-",
        )
        db_client = get_db_client()

        exec_meta = db_client.get_graph_execution_meta(
            user_id=graph_exec.user_id,
            execution_id=graph_exec.graph_exec_id,
        )
        if exec_meta is None:
            log_metadata.warning(
                f"Skipped graph execution #{graph_exec.graph_exec_id}, the graph execution is not found."
            )
            return

        if exec_meta.status in [ExecutionStatus.QUEUED, ExecutionStatus.INCOMPLETE]:
            log_metadata.info(f"⚙️ Starting graph execution #{graph_exec.graph_exec_id}")
            exec_meta.status = ExecutionStatus.RUNNING
            send_execution_update(
                db_client.update_graph_execution_start_time(graph_exec.graph_exec_id)
            )
        elif exec_meta.status == ExecutionStatus.RUNNING:
            log_metadata.info(
                f"⚙️ Graph execution #{graph_exec.graph_exec_id} is already running, continuing where it left off."
            )
        elif exec_meta.status == ExecutionStatus.REVIEW:
            exec_meta.status = ExecutionStatus.RUNNING
            log_metadata.info(
                f"⚙️ Graph execution #{graph_exec.graph_exec_id} was waiting for review, resuming execution."
            )
            update_graph_execution_state(
                db_client=db_client,
                graph_exec_id=graph_exec.graph_exec_id,
                status=ExecutionStatus.RUNNING,
            )
        elif exec_meta.status == ExecutionStatus.FAILED:
            exec_meta.status = ExecutionStatus.RUNNING
            log_metadata.info(
                f"⚙️ Graph execution #{graph_exec.graph_exec_id} was disturbed, continuing where it left off."
            )
            update_graph_execution_state(
                db_client=db_client,
                graph_exec_id=graph_exec.graph_exec_id,
                status=ExecutionStatus.RUNNING,
            )
        else:
            log_metadata.warning(
                f"Skipped graph execution {graph_exec.graph_exec_id}, the graph execution status is `{exec_meta.status}`."
            )
            return

        if exec_meta.stats is None:
            exec_stats = GraphExecutionStats(
                is_dry_run=graph_exec.execution_context.dry_run,
            )
        else:
            exec_stats = exec_meta.stats.to_db()
            exec_stats.is_dry_run = graph_exec.execution_context.dry_run

        timing_info, status = self._on_graph_execution(
            graph_exec=graph_exec,
            cancel=cancel,
            log_metadata=log_metadata,
            execution_stats=exec_stats,
            cluster_lock=cluster_lock,
        )
        exec_stats.walltime += timing_info.wall_time
        exec_stats.cputime += timing_info.cpu_time

        try:
            # Failure handling
            if isinstance(status, BaseException):
                raise status
            exec_meta.status = status

            if status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]:
                activity_response = asyncio.run_coroutine_threadsafe(
                    generate_activity_status_for_execution(
                        graph_exec_id=graph_exec.graph_exec_id,
                        graph_id=graph_exec.graph_id,
                        graph_version=graph_exec.graph_version,
                        execution_stats=exec_stats,
                        db_client=get_db_async_client(),
                        user_id=graph_exec.user_id,
                        execution_status=status,
                    ),
                    self.node_execution_loop,
                ).result(timeout=60.0)
            else:
                activity_response = None
            if activity_response is not None:
                exec_stats.activity_status = activity_response["activity_status"]
                exec_stats.correctness_score = activity_response["correctness_score"]
                log_metadata.info(
                    f"Generated activity status: {activity_response['activity_status']} "
                    f"(correctness: {activity_response['correctness_score']:.2f})"
                )
            else:
                log_metadata.debug(
                    "Activity status generation disabled, not setting fields"
                )
        finally:
            # Communication handling
            billing.handle_agent_run_notif(db_client, graph_exec, exec_stats)

            update_graph_execution_state(
                db_client=db_client,
                graph_exec_id=graph_exec.graph_exec_id,
                status=exec_meta.status,
                stats=exec_stats,
            )

    async def charge_node_usage(
        self,
        node_exec: NodeExecutionEntry,
    ) -> tuple[int, int]:
        return await billing.charge_node_usage(node_exec)

    @time_measured
    def _on_graph_execution(
        self,
        graph_exec: GraphExecutionEntry,
        cancel: threading.Event,
        log_metadata: LogMetadata,
        execution_stats: GraphExecutionStats,
        cluster_lock: ClusterLock,
    ) -> ExecutionStatus:
        """
        Returns:
            dict: The execution statistics of the graph execution.
            ExecutionStatus: The final status of the graph execution.
            Exception | None: The error that occurred during the execution, if any.
        """
        execution_status: ExecutionStatus = ExecutionStatus.RUNNING
        error: Exception | None = None
        db_client = get_db_client()
        execution_stats_lock = threading.Lock()

        # State holders ----------------------------------------------------
        self.running_node_execution: dict[str, NodeExecutionProgress] = defaultdict(
            NodeExecutionProgress
        )
        self.running_node_evaluation: dict[str, Future] = {}
        self.execution_stats = execution_stats
        self.execution_stats_lock = execution_stats_lock
        self.nodes_input_masks = graph_exec.nodes_input_masks
        execution_queue = ExecutionQueue[NodeExecutionEntry]()

        running_node_execution = self.running_node_execution
        running_node_evaluation = self.running_node_evaluation

        try:
            if (
                not graph_exec.execution_context.dry_run
                and db_client.get_credits(graph_exec.user_id) <= 0
            ):
                raise InsufficientBalanceError(
                    user_id=graph_exec.user_id,
                    message="You have no credits left to run an agent.",
                    balance=0,
                    amount=1,
                )

            # Input moderation
            try:
                if moderation_error := asyncio.run_coroutine_threadsafe(
                    automod_manager.moderate_graph_execution_inputs(
                        db_client=get_db_async_client(),
                        graph_exec=graph_exec,
                    ),
                    self.node_evaluation_loop,
                ).result(timeout=30.0):
                    raise moderation_error
            except asyncio.TimeoutError:
                log_metadata.warning(
                    f"Input moderation timed out for graph execution {graph_exec.graph_exec_id}, bypassing moderation and continuing execution"
                )
                # Continue execution without moderation

            # ------------------------------------------------------------
            # Pre‑populate queue ---------------------------------------
            # ------------------------------------------------------------
            for node_exec in db_client.get_node_executions(
                graph_exec.graph_exec_id,
                statuses=[
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.TERMINATED,
                    ExecutionStatus.REVIEW,
                ],
            ):
                node_entry = node_exec.to_node_execution_entry(
                    graph_exec.execution_context
                )
                execution_queue.add(node_entry)

            # ------------------------------------------------------------
            # Main dispatch / polling loop -----------------------------
            # ------------------------------------------------------------

            while not execution_queue.empty():
                if cancel.is_set():
                    break

                queued_node_exec = execution_queue.get()

                # Check if this node should be skipped due to optional credentials
                if queued_node_exec.node_id in graph_exec.nodes_to_skip:
                    log_metadata.info(
                        f"Skipping node execution {queued_node_exec.node_exec_id} "
                        f"for node {queued_node_exec.node_id} - optional credentials not configured"
                    )
                    # Mark the node as completed without executing
                    # No outputs will be produced, so downstream nodes won't trigger
                    update_node_execution_status(
                        db_client=db_client,
                        exec_id=queued_node_exec.node_exec_id,
                        status=ExecutionStatus.COMPLETED,
                    )
                    continue

                log_metadata.debug(
                    f"Dispatching node execution {queued_node_exec.node_exec_id} "
                    f"for node {queued_node_exec.node_id}",
                )

                # Charge usage (may raise) — skipped for dry runs
                try:
                    if not graph_exec.execution_context.dry_run:
                        (
                            cost,
                            remaining_balance,
                            block_pre_flight,
                        ) = billing.charge_usage(
                            node_exec=queued_node_exec,
                            execution_count=increment_execution_count(
                                graph_exec.user_id
                            ),
                        )
                        # Pin the reconciliation baseline to what was just
                        # billed — protects against a hot-swap of the
                        # estimates JSON between charge and reconcile.
                        queued_node_exec.pre_flight_charge = block_pre_flight
                        with execution_stats_lock:
                            execution_stats.cost += cost
                        # Check if we crossed the low balance threshold
                        billing.handle_low_balance(
                            db_client=db_client,
                            user_id=graph_exec.user_id,
                            current_balance=remaining_balance,
                            transaction_cost=cost,
                        )
                except InsufficientBalanceError as balance_error:
                    error = balance_error  # Set error to trigger FAILED status
                    node_exec_id = queued_node_exec.node_exec_id
                    db_client.upsert_execution_output(
                        node_exec_id=node_exec_id,
                        output_name="error",
                        output_data=str(error),
                    )
                    update_node_execution_status(
                        db_client=db_client,
                        exec_id=node_exec_id,
                        status=ExecutionStatus.FAILED,
                    )

                    billing.handle_insufficient_funds_notif(
                        db_client,
                        graph_exec.user_id,
                        graph_exec.graph_id,
                        error,
                    )
                    # Gracefully stop the execution loop
                    break

                # Add input overrides -----------------------------
                node_id = queued_node_exec.node_id
                if (nodes_input_masks := graph_exec.nodes_input_masks) and (
                    node_input_mask := nodes_input_masks.get(node_id)
                ):
                    queued_node_exec.inputs.update(node_input_mask)

                # Kick off async node execution -------------------------
                node_execution_task = asyncio.run_coroutine_threadsafe(
                    self.on_node_execution(
                        node_exec=queued_node_exec,
                        node_exec_progress=running_node_execution[node_id],
                        nodes_input_masks=nodes_input_masks,
                        graph_stats_pair=(
                            execution_stats,
                            execution_stats_lock,
                        ),
                        nodes_to_skip=graph_exec.nodes_to_skip,
                    ),
                    self.node_execution_loop,
                )
                running_node_execution[node_id].add_task(
                    node_exec_id=queued_node_exec.node_exec_id,
                    task=node_execution_task,
                )

                # Poll until queue refills or all inflight work done ----
                while execution_queue.empty() and (
                    running_node_execution or running_node_evaluation
                ):
                    if cancel.is_set():
                        break

                    # --------------------------------------------------
                    # Handle inflight evaluations ---------------------
                    # --------------------------------------------------
                    node_output_found = False
                    for node_id, inflight_exec in list(running_node_execution.items()):
                        if cancel.is_set():
                            break

                        # node evaluation future -----------------
                        if inflight_eval := running_node_evaluation.get(node_id):
                            if not inflight_eval.done():
                                continue
                            try:
                                inflight_eval.result(timeout=0)
                                running_node_evaluation.pop(node_id)
                            except Exception as e:
                                log_metadata.error(f"Node eval #{node_id} failed: {e}")

                        # node execution future ---------------------------
                        if inflight_exec.is_done():
                            running_node_execution.pop(node_id)
                            continue

                        if output := inflight_exec.pop_output():
                            node_output_found = True
                            running_node_evaluation[node_id] = (
                                asyncio.run_coroutine_threadsafe(
                                    self._process_node_output(
                                        output=output,
                                        node_id=node_id,
                                        graph_exec=graph_exec,
                                        log_metadata=log_metadata,
                                        nodes_input_masks=nodes_input_masks,
                                        execution_queue=execution_queue,
                                    ),
                                    self.node_evaluation_loop,
                                )
                            )
                    if (
                        not node_output_found
                        and execution_queue.empty()
                        and (running_node_execution or running_node_evaluation)
                    ):
                        cluster_lock.refresh()
                        time.sleep(0.1)

            # loop done --------------------------------------------------

            # Output moderation
            try:
                if moderation_error := asyncio.run_coroutine_threadsafe(
                    automod_manager.moderate_graph_execution_outputs(
                        db_client=get_db_async_client(),
                        graph_exec_id=graph_exec.graph_exec_id,
                        user_id=graph_exec.user_id,
                        graph_id=graph_exec.graph_id,
                    ),
                    self.node_evaluation_loop,
                ).result(timeout=30.0):
                    raise moderation_error
            except asyncio.TimeoutError:
                log_metadata.warning(
                    f"Output moderation timed out for graph execution {graph_exec.graph_exec_id}, bypassing moderation and continuing execution"
                )
                # Continue execution without moderation

            # Determine final execution status based on whether there was an error or termination
            if cancel.is_set():
                execution_status = ExecutionStatus.TERMINATED
            elif error is not None:
                execution_status = ExecutionStatus.FAILED
            else:
                if db_client.has_pending_reviews_for_graph_exec(
                    graph_exec.graph_exec_id
                ):
                    execution_status = ExecutionStatus.REVIEW
                else:
                    execution_status = ExecutionStatus.COMPLETED

            if error:
                execution_stats.error = str(error) or type(error).__name__

            return execution_status

        except BaseException as e:
            error = (
                e
                if isinstance(e, Exception)
                else Exception(f"{e.__class__.__name__}: {e}")
            )
            if not execution_stats.error:
                execution_stats.error = str(error)

            known_errors = (InsufficientBalanceError, ModerationError)
            if isinstance(error, known_errors):
                return ExecutionStatus.FAILED

            execution_status = ExecutionStatus.FAILED
            log_metadata.exception(
                f"Failed graph execution {graph_exec.graph_exec_id}: {error}"
            )

            # Send rate-limited Discord alert for unknown/unexpected errors
            send_rate_limited_discord_alert(
                "graph_execution",
                error,
                "unknown_error",
                f"🚨 **Unknown Graph Execution Error**\n"
                f"User: {graph_exec.user_id}\n"
                f"Graph ID: {graph_exec.graph_id}\n"
                f"Execution ID: {graph_exec.graph_exec_id}\n"
                f"Error Type: {type(error).__name__}\n"
                f"Error: {str(error)[:200]}{'...' if len(str(error)) > 200 else ''}\n",
            )

            raise

        finally:
            self._cleanup_graph_execution(
                execution_queue=execution_queue,
                running_node_execution=running_node_execution,
                running_node_evaluation=running_node_evaluation,
                execution_status=execution_status,
                error=error,
                graph_exec_id=graph_exec.graph_exec_id,
                log_metadata=log_metadata,
                db_client=db_client,
            )

    @error_logged(swallow=True)
    def _cleanup_graph_execution(
        self,
        execution_queue: ExecutionQueue[NodeExecutionEntry],
        running_node_execution: dict[str, "NodeExecutionProgress"],
        running_node_evaluation: dict[str, Future],
        execution_status: ExecutionStatus,
        error: Exception | None,
        graph_exec_id: str,
        log_metadata: LogMetadata,
        db_client: "DatabaseManagerClient",
    ) -> None:
        """
        Clean up running node executions and evaluations when graph execution ends.
        This method is decorated with @error_logged(swallow=True) to ensure cleanup
        never fails in the finally block.
        """
        # Cancel and wait for all node executions to complete
        for node_id, inflight_exec in running_node_execution.items():
            if inflight_exec.is_done():
                continue
            log_metadata.info(f"Stopping node execution {node_id}")
            inflight_exec.stop()

        for node_id, inflight_exec in running_node_execution.items():
            try:
                inflight_exec.wait_for_done(timeout=3600.0)
            except TimeoutError:
                log_metadata.exception(
                    f"Node execution #{node_id} did not stop in time, "
                    "it may be stuck or taking too long."
                )

        # Wait the remaining inflight evaluations to finish
        for node_id, inflight_eval in running_node_evaluation.items():
            try:
                inflight_eval.result(timeout=3600.0)
            except TimeoutError:
                log_metadata.exception(
                    f"Node evaluation #{node_id} did not stop in time, "
                    "it may be stuck or taking too long."
                )

        while queued_execution := execution_queue.get_or_none():
            update_node_execution_status(
                db_client=db_client,
                exec_id=queued_execution.node_exec_id,
                status=execution_status,
                stats={"error": str(error)} if error else None,
            )

        clean_exec_files(graph_exec_id)

    @async_error_logged(swallow=True)
    async def _process_node_output(
        self,
        output: ExecutionOutputEntry,
        node_id: str,
        graph_exec: GraphExecutionEntry,
        log_metadata: LogMetadata,
        nodes_input_masks: Optional[NodesInputMasks],
        execution_queue: ExecutionQueue[NodeExecutionEntry],
    ) -> None:
        """Process a node's output, update its status, and enqueue next nodes.

        Args:
            output: The execution output entry to process
            node_id: The ID of the node that produced the output
            graph_exec: The graph execution entry
            log_metadata: Logger metadata for consistent logging
            nodes_input_masks: Optional map of node input overrides
            execution_queue: Queue to add next executions to
        """
        db_client = get_db_async_client()

        log_metadata.debug(f"Enqueue nodes for {node_id}: {output}")

        for next_execution in await _enqueue_next_nodes(
            db_client=db_client,
            node=output.node,
            output=output.data,
            user_id=graph_exec.user_id,
            graph_exec_id=graph_exec.graph_exec_id,
            graph_id=graph_exec.graph_id,
            graph_version=graph_exec.graph_version,
            log_metadata=log_metadata,
            nodes_input_masks=nodes_input_masks,
            execution_context=graph_exec.execution_context,
        ):
            execution_queue.add(next_execution)


def get_db_client() -> "DatabaseManagerClient":
    return get_database_manager_client()


def get_db_async_client() -> "DatabaseManagerAsyncClient":
    return get_database_manager_async_client()


@func_retry
async def send_async_execution_update(
    entry: GraphExecution | NodeExecutionResult | None,
) -> None:
    if entry is None:
        return
    await get_async_execution_event_bus().publish(entry)


@func_retry
def send_execution_update(entry: GraphExecution | NodeExecutionResult | None):
    if entry is None:
        return
    return get_execution_event_bus().publish(entry)


async def async_update_node_execution_status(
    db_client: "DatabaseManagerAsyncClient",
    exec_id: str,
    status: ExecutionStatus,
    execution_data: BlockInput | None = None,
    stats: dict[str, Any] | None = None,
) -> NodeExecutionResult:
    """Sets status and fetches+broadcasts the latest state of the node execution"""
    exec_update = await db_client.update_node_execution_status(
        exec_id, status, execution_data, stats
    )
    await send_async_execution_update(exec_update)
    return exec_update


def update_node_execution_status(
    db_client: "DatabaseManagerClient",
    exec_id: str,
    status: ExecutionStatus,
    execution_data: BlockInput | None = None,
    stats: dict[str, Any] | None = None,
) -> NodeExecutionResult:
    """Sets status and fetches+broadcasts the latest state of the node execution"""
    exec_update = db_client.update_node_execution_status(
        exec_id, status, execution_data, stats
    )
    send_execution_update(exec_update)
    return exec_update


async def async_update_graph_execution_state(
    db_client: "DatabaseManagerAsyncClient",
    graph_exec_id: str,
    status: ExecutionStatus | None = None,
    stats: GraphExecutionStats | None = None,
) -> GraphExecution | None:
    """Sets status and fetches+broadcasts the latest state of the graph execution"""
    graph_update = await db_client.update_graph_execution_stats(
        graph_exec_id, status, stats
    )
    if graph_update:
        await send_async_execution_update(graph_update)
    else:
        logger.error(f"Failed to update graph execution stats for {graph_exec_id}")
    return graph_update


def update_graph_execution_state(
    db_client: "DatabaseManagerClient",
    graph_exec_id: str,
    status: ExecutionStatus | None = None,
    stats: GraphExecutionStats | None = None,
) -> GraphExecution | None:
    """Sets status and fetches+broadcasts the latest state of the graph execution"""
    graph_update = db_client.update_graph_execution_stats(graph_exec_id, status, stats)
    if graph_update:
        send_execution_update(graph_update)
    else:
        logger.error(f"Failed to update graph execution stats for {graph_exec_id}")
    return graph_update


@asynccontextmanager
async def synchronized(key: str, timeout: int = settings.config.cluster_lock_timeout):
    r = await redis.get_redis_async()
    lock: AsyncRedisLock = r.lock(f"lock:{key}", timeout=timeout)
    try:
        await lock.acquire()
        yield
    finally:
        if await lock.locked() and await lock.owned():
            try:
                await lock.release()
            except Exception as e:
                logger.warning(f"Failed to release lock for key {key}: {e}")


def increment_execution_count(user_id: str) -> int:
    """
    Increment the execution count for a given user,
    this will be used to charge the user for the execution cost.

    Uses :func:`incr_with_ttl_sync` so INCR and EXPIRE run atomically via
    MULTI/EXEC — previously this was a bare INCR followed by a separate
    EXPIRE which could orphan the counter (no TTL) if the process died
    between the two commands.
    """
    r = redis.get_redis()
    k = f"uec:{user_id}"  # User Execution Count global key
    return incr_with_ttl_sync(r, k, settings.config.execution_counter_expiration_time)
