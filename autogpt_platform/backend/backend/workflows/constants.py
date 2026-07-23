"""Shared constants for the Render Workflows execution path."""

# Name of the graph-execution task, shared by the producer (dispatch slug) and
# the task definition so the two can never drift out of sync.
TASK_NAME = "run_graph_execution"

# Render Workflows caps a single task run at 24h (86,400s). Used as the task
# timeout and as the basis for the per-run Redis artifact TTL below.
MAX_RUN_SECONDS = 24 * 60 * 60

# TTL / stale-reclaim window for the per-run Redis artifacts backing one graph
# execution: the cancel flag (`backend.workflows.cancel`), the stored
# `GraphExecutionEntry` (`backend.workflows.entry_store`), and the per-user
# concurrency slot (`backend.workflows.rate_limit`). Sized to the max run
# duration plus a 1h margin so none of them can expire mid-run; a crashed
# holder's concurrency slot is reclaimed once this window lapses.
RUN_ARTIFACT_TTL_SECONDS = MAX_RUN_SECONDS + 60 * 60
