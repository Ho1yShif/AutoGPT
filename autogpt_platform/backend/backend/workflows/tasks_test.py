"""Unit tests for :mod:`backend.workflows.tasks`.

The task body is invoked via ``__wrapped__`` (the raw undecorated function) so
no Render Workflows runtime is needed. All Redis-backed dependencies are mocked.
"""

from pytest_mock import MockerFixture

from backend.workflows import tasks

# The undecorated task body — bypasses the render_sdk TaskCallable wrapper.
_run_graph_execution = tasks.run_graph_execution.__wrapped__


def test_skipped_no_entry_releases_slot_and_clears_cancel(mocker: MockerFixture):
    """When the stored entry is missing (blob expired / never written) the task
    returns BEFORE its try/finally, so it must release the concurrency slot and
    clear the cancel flag inline — otherwise the slot leaks until the 25h
    stale-sweep, silently costing the user a slot for a day."""
    mocker.patch(
        "backend.workflows.entry_store.load_execution_entry_sync",
        return_value=None,
    )
    release = mocker.patch("backend.workflows.rate_limit.release_run_slot_sync")
    clear_cancel = mocker.patch("backend.workflows.cancel.clear_cancel")

    result = _run_graph_execution("exec-1", "user-1")

    assert result == {"graph_exec_id": "exec-1", "status": "skipped_no_entry"}
    release.assert_called_once_with("user-1", "exec-1")
    clear_cancel.assert_called_once_with("exec-1")
