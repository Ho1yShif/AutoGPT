"""Render Workflows executor backend.

Alternative dispatch path to the RabbitMQ `ExecutionManager`, selected by
`Config.execution_backend == "workflows"`. Producers
(`backend.executor.utils.add_graph_execution` / `stop_graph_execution`) call
into `backend.workflows.client` to start / cancel a Render Workflows task run;
the task itself (`backend.workflows.tasks.run_graph_execution`) drives the same
broker-agnostic engine in `backend.executor.engine`.

`render_sdk` is imported lazily inside `client` so the default RabbitMQ path
never requires the dependency; `tasks`/`main` import it at module level since
they only load in the Render Workflows deployment.
"""
