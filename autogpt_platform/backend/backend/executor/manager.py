"""
RabbitMQ-backed executor (`ExecutionManager`).

This is the classic broker path: an `AppProcess` that consumes graph-execution
run/cancel messages from RabbitMQ and drives the broker-agnostic engine in
`backend.executor.engine`. The Render Workflows path (`backend/workflows/`) is
an alternative front-end to the SAME engine; both are selected at the producer
side via the `EXECUTION_BACKEND` setting (see `backend/executor/utils.py`).

Engine symbols are imported directly from `backend.executor.engine`; this module
imports only what `ExecutionManager` itself uses. Consumers that want engine
helpers (e.g. `ExecutionProcessor`, `get_db_async_client`) import them from
`backend.executor.engine` directly.
"""

import asyncio
import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties
from prometheus_client import start_http_server

from backend.data import redis_client as redis
from backend.data.execution import ExecutionStatus, GraphExecutionEntry
from backend.data.rabbitmq import SyncRabbitMQ
from backend.executor.cost_tracking import drain_pending_cost_logs
from backend.executor.engine import (
    active_runs_gauge,
    execute_graph,
    get_db_client,
    init_worker,
    pool_size_gauge,
    utilization_gauge,
)
from backend.util.decorator import error_logged
from backend.util.logging import TruncatedLogger
from backend.util.process import AppProcess
from backend.util.retry import continuous_retry, func_retry
from backend.util.settings import Settings

from .cluster_lock import ClusterLock
from .utils import (
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    GRAPH_EXECUTION_CANCEL_QUEUE_NAME,
    GRAPH_EXECUTION_EXCHANGE,
    GRAPH_EXECUTION_QUEUE_NAME,
    GRAPH_EXECUTION_ROUTING_KEY,
    CancelExecutionEvent,
    create_execution_queue_config,
)

_logger = logging.getLogger(__name__)
logger = TruncatedLogger(_logger, prefix="[ExecutionManager]")
settings = Settings()


class ExecutionManager(AppProcess):
    def __init__(self):
        super().__init__()
        self.pool_size = settings.config.num_graph_workers
        self.active_graph_runs: dict[str, tuple[Future, threading.Event]] = {}
        self.executor_id = str(uuid.uuid4())

        self._executor = None
        self._stop_consuming = None

        self._cancel_thread = None
        self._cancel_client = None
        self._run_thread = None
        self._run_client = None

        self._execution_locks = {}

    @property
    def cancel_thread(self) -> threading.Thread:
        if self._cancel_thread is None:
            self._cancel_thread = threading.Thread(
                target=lambda: self._consume_execution_cancel(),
                daemon=True,
            )
        return self._cancel_thread

    @property
    def run_thread(self) -> threading.Thread:
        if self._run_thread is None:
            self._run_thread = threading.Thread(
                target=lambda: self._consume_execution_run(),
                daemon=True,
            )
        return self._run_thread

    @property
    def stop_consuming(self) -> threading.Event:
        if self._stop_consuming is None:
            self._stop_consuming = threading.Event()
        return self._stop_consuming

    @property
    def executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.pool_size,
                initializer=init_worker,
            )
        return self._executor

    @property
    def cancel_client(self) -> SyncRabbitMQ:
        if self._cancel_client is None:
            self._cancel_client = SyncRabbitMQ(create_execution_queue_config())
        return self._cancel_client

    @property
    def run_client(self) -> SyncRabbitMQ:
        if self._run_client is None:
            self._run_client = SyncRabbitMQ(create_execution_queue_config())
        return self._run_client

    def run(self):
        logger.info(
            f"[{self.service_name}] 🆔 Pod assigned executor_id: {self.executor_id}"
        )
        logger.info(f"[{self.service_name}] ⏳ Spawn max-{self.pool_size} workers...")

        pool_size_gauge.set(self.pool_size)
        self._update_prompt_metrics()
        start_http_server(settings.config.execution_manager_port)

        self.cancel_thread.start()
        self.run_thread.start()

        while True:
            time.sleep(1e5)

    @continuous_retry()
    def _consume_execution_cancel(self):
        if self.stop_consuming.is_set() and not self.active_graph_runs:
            logger.info(
                f"[{self.service_name}] Stop reconnecting cancel consumer since the service is cleaned up."
            )
            return

        # Check if channel is closed and force reconnection if needed
        if not self.cancel_client.is_ready:
            self.cancel_client.disconnect()
        self.cancel_client.connect()
        cancel_channel = self.cancel_client.get_channel()
        cancel_channel.basic_consume(
            queue=GRAPH_EXECUTION_CANCEL_QUEUE_NAME,
            on_message_callback=self._handle_cancel_message,
            auto_ack=True,
        )
        logger.info(f"[{self.service_name}] ⏳ Starting cancel message consumer...")
        cancel_channel.start_consuming()
        if not self.stop_consuming.is_set() or self.active_graph_runs:
            raise RuntimeError(
                f"[{self.service_name}] ❌ cancel message consumer is stopped: {cancel_channel}"
            )
        logger.info(
            f"[{self.service_name}] ✅ Cancel message consumer stopped gracefully"
        )

    @continuous_retry()
    def _consume_execution_run(self):
        # Long-running executions are handled by:
        # 1. Long consumer timeout (x-consumer-timeout) allows long running agent
        # 2. Enhanced connection settings (5 retries, 1s delay) for quick reconnection
        # 3. Process monitoring ensures failed executors release messages back to queue
        if self.stop_consuming.is_set():
            logger.info(
                f"[{self.service_name}] Stop reconnecting execution consumer since the service is cleaned up."
            )
            return

        # Check if channel is closed and force reconnection if needed
        if not self.run_client.is_ready:
            self.run_client.disconnect()
        self.run_client.connect()
        run_channel = self.run_client.get_channel()
        run_channel.basic_qos(prefetch_count=self.pool_size)

        # Configure consumer for long-running graph executions
        # auto_ack=False: Don't acknowledge messages until execution completes (prevents data loss)
        run_channel.basic_consume(
            queue=GRAPH_EXECUTION_QUEUE_NAME,
            on_message_callback=self._handle_run_message,
            auto_ack=False,
            consumer_tag="graph_execution_consumer",
        )
        run_channel.confirm_delivery()
        logger.info(f"[{self.service_name}] ⏳ Starting to consume run messages...")
        run_channel.start_consuming()
        if not self.stop_consuming.is_set():
            raise RuntimeError(
                f"[{self.service_name}] ❌ run message consumer is stopped: {run_channel}"
            )
        logger.info(f"[{self.service_name}] ✅ Run message consumer stopped gracefully")

    @error_logged(swallow=True)
    def _handle_cancel_message(
        self,
        _channel: BlockingChannel,
        _method: Basic.Deliver,
        _properties: BasicProperties,
        body: bytes,
    ):
        """
        Called whenever we receive a CANCEL message from the queue.
        (With auto_ack=True, message is considered 'acked' automatically.)
        """
        request = CancelExecutionEvent.model_validate_json(body)
        graph_exec_id = request.graph_exec_id
        if not graph_exec_id:
            logger.warning(
                f"[{self.service_name}] Cancel message missing 'graph_exec_id'"
            )
            return
        if graph_exec_id not in self.active_graph_runs:
            logger.debug(
                f"[{self.service_name}] Cancel received for {graph_exec_id} but not active."
            )
            return

        _, cancel_event = self.active_graph_runs[graph_exec_id]
        logger.info(f"[{self.service_name}] Received cancel for {graph_exec_id}")
        if not cancel_event.is_set():
            cancel_event.set()
        else:
            logger.debug(
                f"[{self.service_name}] Cancel already set for {graph_exec_id}"
            )

    def _handle_run_message(
        self,
        _channel: BlockingChannel,
        method: Basic.Deliver,
        _properties: BasicProperties,
        body: bytes,
    ):
        delivery_tag = method.delivery_tag

        @func_retry
        def _ack_message(reject: bool, requeue: bool):
            """
            Acknowledge or reject the message based on execution status.

            Args:
                reject: Whether to reject the message
                requeue: Whether to requeue the message
            """

            # Connection can be lost, so always get a fresh channel
            channel = self.run_client.get_channel()
            if reject:
                if requeue and settings.config.requeue_by_republishing:
                    # Send rejected message to back of queue using republishing
                    def _republish_to_back():
                        try:
                            # First republish to back of queue
                            self.run_client.publish_message(
                                routing_key=GRAPH_EXECUTION_ROUTING_KEY,
                                message=body.decode(),  # publish_message expects string, not bytes
                                exchange=GRAPH_EXECUTION_EXCHANGE,
                            )
                            # Then reject without requeue (message already republished)
                            channel.basic_nack(delivery_tag, requeue=False)
                            logger.info("Message requeued to back of queue")
                        except Exception as e:
                            logger.error(
                                f"[{self.service_name}] Failed to requeue message to back: {e}"
                            )
                            # Fall back to traditional requeue on failure
                            channel.basic_nack(delivery_tag, requeue=True)

                    channel.connection.add_callback_threadsafe(_republish_to_back)
                else:
                    # Traditional requeue (goes to front) or no requeue
                    channel.connection.add_callback_threadsafe(
                        lambda: channel.basic_nack(delivery_tag, requeue=requeue)
                    )
            else:
                channel.connection.add_callback_threadsafe(
                    lambda: channel.basic_ack(delivery_tag)
                )

        # Check if we're shutting down - reject new messages but keep connection alive
        if self.stop_consuming.is_set():
            logger.info(
                f"[{self.service_name}] Rejecting new execution during shutdown"
            )
            _ack_message(reject=True, requeue=True)
            return

        # Check if we can accept more runs
        self._cleanup_completed_runs()
        if len(self.active_graph_runs) >= self.pool_size:
            _ack_message(reject=True, requeue=True)
            return

        try:
            graph_exec_entry = GraphExecutionEntry.model_validate_json(body)
        except Exception as e:
            logger.error(
                f"[{self.service_name}] Could not parse run message: {e}, body={body}"
            )
            _ack_message(reject=True, requeue=False)
            return

        graph_exec_id = graph_exec_entry.graph_exec_id
        user_id = graph_exec_entry.user_id
        graph_id = graph_exec_entry.graph_id
        root_exec_id = graph_exec_entry.execution_context.root_execution_id
        parent_exec_id = graph_exec_entry.execution_context.parent_execution_id

        logger.info(
            f"[{self.service_name}] Received RUN for graph_exec_id={graph_exec_id}, user_id={user_id}, executor_id={self.executor_id}"
            + (f", root={root_exec_id}" if root_exec_id else "")
            + (f", parent={parent_exec_id}" if parent_exec_id else "")
        )

        # Check if root execution is already terminated (prevents orphaned child executions)
        if root_exec_id and root_exec_id != graph_exec_id:
            parent_exec = get_db_client().get_graph_execution_meta(
                execution_id=root_exec_id,
                user_id=user_id,
            )
            if parent_exec and parent_exec.status == ExecutionStatus.TERMINATED:
                logger.info(
                    f"[{self.service_name}] Skipping execution {graph_exec_id} - parent {root_exec_id} is TERMINATED"
                )
                # Mark this child as terminated since parent was stopped
                get_db_client().update_graph_execution_stats(
                    graph_exec_id=graph_exec_id,
                    status=ExecutionStatus.TERMINATED,
                )
                _ack_message(reject=False, requeue=False)
                return

        # Check user rate limit before processing
        try:
            # Only check executions from the last 24 hours for performance
            current_running_count = get_db_client().get_graph_executions_count(
                user_id=user_id,
                graph_id=graph_id,
                statuses=[ExecutionStatus.RUNNING],
                created_time_gte=datetime.now(timezone.utc) - timedelta(hours=24),
            )

            if (
                current_running_count
                >= settings.config.max_concurrent_graph_executions_per_user
            ):
                logger.warning(
                    f"[{self.service_name}] Rate limit exceeded for user {user_id} on graph {graph_id}: "
                    f"{current_running_count}/{settings.config.max_concurrent_graph_executions_per_user} running executions"
                )
                _ack_message(reject=True, requeue=True)
                return

        except Exception as e:
            logger.error(
                f"[{self.service_name}] Failed to check rate limit for user {user_id}: {e}, proceeding with execution"
            )
            # If rate limit check fails, proceed to avoid blocking executions

        # Check for local duplicate execution first
        if graph_exec_id in self.active_graph_runs:
            logger.warning(
                f"[{self.service_name}] Graph {graph_exec_id} already running locally; rejecting duplicate."
            )
            _ack_message(reject=True, requeue=True)
            return

        # Try to acquire cluster-wide execution lock
        cluster_lock = ClusterLock(
            redis=redis.get_redis(),
            key=f"exec_lock:{graph_exec_id}",
            owner_id=self.executor_id,
            timeout=settings.config.cluster_lock_timeout,
        )
        current_owner = cluster_lock.try_acquire()
        if current_owner != self.executor_id:
            # Either someone else has it or Redis is unavailable
            if current_owner is not None:
                logger.warning(
                    f"[{self.service_name}] Graph {graph_exec_id} already running on pod {current_owner}, current executor_id={self.executor_id}"
                )
                _ack_message(reject=True, requeue=False)
            else:
                logger.warning(
                    f"[{self.service_name}] Could not acquire lock for {graph_exec_id} - Redis unavailable"
                )
                _ack_message(reject=True, requeue=True)
            return

        # Wrap entire block after successful lock acquisition
        try:
            self._execution_locks[graph_exec_id] = cluster_lock

            logger.info(
                f"[{self.service_name}] Successfully acquired cluster lock for {graph_exec_id}, executor_id={self.executor_id}"
            )

            cancel_event = threading.Event()
            future = self.executor.submit(
                execute_graph, graph_exec_entry, cancel_event, cluster_lock
            )
            self.active_graph_runs[graph_exec_id] = (future, cancel_event)
        except Exception as e:
            logger.warning(
                f"[{self.service_name}] Failed to setup execution for {graph_exec_id}: {type(e).__name__}: {e}"
            )
            # Release cluster lock before requeue
            cluster_lock.release()
            if graph_exec_id in self._execution_locks:
                del self._execution_locks[graph_exec_id]
            _ack_message(reject=True, requeue=True)
            return
        self._update_prompt_metrics()

        def _on_run_done(f: Future):
            logger.info(f"[{self.service_name}] Run completed for {graph_exec_id}")
            try:
                if exec_error := f.exception():
                    logger.error(
                        f"[{self.service_name}] Execution for {graph_exec_id} failed: {type(exec_error)} {exec_error}"
                    )
                    _ack_message(reject=True, requeue=True)
                else:
                    _ack_message(reject=False, requeue=False)
            except BaseException as e:
                logger.exception(
                    f"[{self.service_name}] Error in run completion callback: {e}"
                )
            finally:
                # Release the cluster-wide execution lock
                if graph_exec_id in self._execution_locks:
                    logger.info(
                        f"[{self.service_name}] Releasing cluster lock for {graph_exec_id}, executor_id={self.executor_id}"
                    )
                    self._execution_locks[graph_exec_id].release()
                    del self._execution_locks[graph_exec_id]
                self._cleanup_completed_runs()

        future.add_done_callback(_on_run_done)

    def _cleanup_completed_runs(self) -> list[str]:
        """Remove completed futures from active_graph_runs and update metrics"""
        completed_runs = []
        for graph_exec_id, (future, _) in self.active_graph_runs.items():
            if future.done():
                completed_runs.append(graph_exec_id)

        for geid in completed_runs:
            logger.info(f"[{self.service_name}] ✅ Cleaned up completed run {geid}")
            self.active_graph_runs.pop(geid, None)

        self._update_prompt_metrics()
        return completed_runs

    def _update_prompt_metrics(self):
        active_count = len(self.active_graph_runs)
        active_runs_gauge.set(active_count)
        if self.stop_consuming.is_set():
            utilization_gauge.set(1.0)
        else:
            utilization_gauge.set(active_count / self.pool_size)

    def _stop_message_consumers(
        self, thread: threading.Thread, client: SyncRabbitMQ, prefix: str
    ):
        try:
            channel = client.get_channel()
            channel.connection.add_callback_threadsafe(lambda: channel.stop_consuming())

            thread.join(timeout=300)
            if thread.is_alive():
                logger.warning(
                    f"{prefix} ⚠️ Run thread did not finish in time, forcing disconnect"
                )

            client.disconnect()
            logger.info(f"{prefix} ✅ Run client disconnected")
        except Exception as e:
            logger.warning(f"{prefix} ⚠️ Error disconnecting run client: {type(e)} {e}")

    def cleanup(self):
        """Override cleanup to implement graceful shutdown with active execution waiting."""
        prefix = f"[{self.service_name}][on_graph_executor_stop {os.getpid()}]"
        logger.info(f"{prefix} 🧹 Starting graceful shutdown...")

        # Signal the consumer thread to stop (thread-safe)
        try:
            self.stop_consuming.set()
            run_channel = self.run_client.get_channel()
            run_channel.connection.add_callback_threadsafe(
                lambda: run_channel.stop_consuming()
            )
            logger.info(f"{prefix} ✅ Exec consumer has been signaled to stop")
        except Exception as e:
            logger.warning(
                f"{prefix} ⚠️ Error signaling consumer to stop: {type(e)} {e}"
            )

        # Wait for active executions to complete
        if self.active_graph_runs:
            logger.info(
                f"{prefix} ⏳ Waiting for {len(self.active_graph_runs)} active executions to complete..."
            )

            max_wait = GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
            wait_interval = 5
            waited = 0

            while waited < max_wait:
                self._cleanup_completed_runs()
                if not self.active_graph_runs:
                    logger.info(f"{prefix} ✅ All active executions completed")
                    break
                else:
                    ids = [k.split("-")[0] for k in self.active_graph_runs.keys()]
                    logger.info(
                        f"{prefix} ⏳ Still waiting for {len(self.active_graph_runs)} executions: {ids}"
                    )

                    for graph_exec_id in self.active_graph_runs:
                        if lock := self._execution_locks.get(graph_exec_id):
                            lock.refresh()

                time.sleep(wait_interval)
                waited += wait_interval

            if self.active_graph_runs:
                logger.warning(
                    f"{prefix} ⚠️ {len(self.active_graph_runs)} executions still running after {max_wait}s"
                )
            else:
                logger.info(f"{prefix} ✅ All executions completed gracefully")

        # Shutdown the executor
        try:
            self.executor.shutdown(cancel_futures=True, wait=False)
            logger.info(f"{prefix} ✅ Executor shutdown completed")
        except Exception as e:
            logger.warning(f"{prefix} ⚠️ Error during executor shutdown: {type(e)} {e}")

        # Release remaining execution locks
        try:
            for lock in self._execution_locks.values():
                lock.release()
            self._execution_locks.clear()
            logger.info(f"{prefix} ✅ Released execution locks")
        except Exception as e:
            logger.warning(f"{prefix} ⚠️ Failed to release all locks: {e}")

        # Disconnect the run execution consumer
        self._stop_message_consumers(
            self.run_thread,
            self.run_client,
            prefix + " [run-consumer]",
        )
        self._stop_message_consumers(
            self.cancel_thread,
            self.cancel_client,
            prefix + " [cancel-consumer]",
        )

        # Drain any in-flight cost log tasks before exit so we don't silently
        # drop INSERT operations during deployments.
        loop = getattr(self, "node_execution_loop", None)
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    drain_pending_cost_logs(), loop
                ).result(timeout=10)
                logger.info(f"{prefix} ✅ Cost log tasks drained")
            except Exception as e:
                logger.warning(f"{prefix} ⚠️ Failed to drain cost log tasks: {e}")

        logger.info(f"{prefix} ✅ Finished GraphExec cleanup")

        super().cleanup()
