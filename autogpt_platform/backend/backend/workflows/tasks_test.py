"""Unit tests for :mod:`backend.workflows.tasks`.

The task body is invoked via ``__wrapped__`` (the raw undecorated function) so
no Render Workflows runtime is needed. All Redis-backed dependencies are mocked.
"""

import pytest
from pytest_mock import MockerFixture

from backend.workflows import tasks

# The undecorated task body — bypasses the render_sdk TaskCallable wrapper.
_run_graph_execution = tasks.run_graph_execution.__wrapped__


@pytest.fixture(autouse=True)
def _reset_processor():
    """The per-thread ExecutionProcessor persists across runs by design; reset it
    between tests so each test observes a fresh init deterministically."""
    if hasattr(tasks._tls, "processor"):
        del tasks._tls.processor
    yield
    if hasattr(tasks._tls, "processor"):
        del tasks._tls.processor


def _wire_happy_path(mocker: MockerFixture, *, acquire="self", on_execution=None):
    """Mock every dependency so the task body reaches its try/finally.

    ``acquire`` selects what ``ClusterLock.try_acquire`` returns:
      "self"  → the generated executor_id (lock acquired)
      "other" → a different owner (genuine foreign owner → skipped_locked)
      None    → indeterminate (Redis error) → raise-for-requeue path
    ``on_execution`` is an optional side effect for ``on_graph_execution`` (e.g.
    to set the cancel event). Returns the wired spies.
    """
    mocker.patch(
        "backend.workflows.entry_store.load_execution_entry_sync",
        return_value=mocker.MagicMock(name="entry"),
    )
    mocker.patch("backend.data.redis_client.get_redis", return_value=mocker.MagicMock())

    def _make_lock(**kwargs):
        lock = mocker.MagicMock()
        lock.owner_id = kwargs["owner_id"]
        lock.try_acquire.return_value = (
            kwargs["owner_id"] if acquire == "self" else acquire
        )
        return lock

    lock_cls = mocker.patch(
        "backend.workflows.tasks.ClusterLock", side_effect=_make_lock
    )

    processor_cls = mocker.patch("backend.workflows.tasks.ExecutionProcessor")
    if on_execution is not None:
        processor_cls.return_value.on_graph_execution.side_effect = on_execution

    mocker.patch("backend.workflows.cancel.start_cancel_poller")
    release = mocker.patch("backend.workflows.rate_limit.release_run_slot_sync")
    clear_cancel = mocker.patch("backend.workflows.cancel.clear_cancel")
    delete_entry = mocker.patch(
        "backend.workflows.entry_store.delete_execution_entry_sync"
    )
    return dict(
        lock_cls=lock_cls,
        processor_cls=processor_cls,
        release=release,
        clear_cancel=clear_cancel,
        delete_entry=delete_entry,
    )


def test_processor_initialized_once_across_runs(mocker: MockerFixture):
    """`on_graph_executor_start` spins up daemon event-loop threads; it must run
    at most once per worker, not per run (else 2 threads/loops leak per run on a
    reused worker). Two runs in one thread → one processor, one start."""
    m = _wire_happy_path(mocker)

    _run_graph_execution("exec-1", "user-1")
    _run_graph_execution("exec-2", "user-1")

    assert m["processor_cls"].call_count == 1
    m["processor_cls"].return_value.on_graph_executor_start.assert_called_once()
    assert m["processor_cls"].return_value.on_graph_execution.call_count == 2


def test_skipped_locked_keeps_slot(mocker: MockerFixture):
    """A genuine foreign owner means the OWNING run still holds the concurrency
    slot — the duplicate dispatch must return skipped_locked WITHOUT releasing
    it."""
    m = _wire_happy_path(mocker, acquire="other-owner")

    result = _run_graph_execution("exec-1", "user-1")

    assert result == {"graph_exec_id": "exec-1", "status": "skipped_locked"}
    m["release"].assert_not_called()
    # The owning run's processor must not be touched either.
    m["processor_cls"].return_value.on_graph_execution.assert_not_called()


def test_indeterminate_lock_raises_and_releases_slot(mocker: MockerFixture):
    """try_acquire → None means Redis errored, NOT a foreign owner. Returning a
    success-shaped skip would strand the run (retries disabled, no requeue). The
    task must release the unusable slot and raise so it surfaces for requeue."""
    m = _wire_happy_path(mocker, acquire=None)

    with pytest.raises(RuntimeError, match="indeterminate"):
        _run_graph_execution("exec-1", "user-1")

    m["release"].assert_called_once_with("user-1", "exec-1")
    m["clear_cancel"].assert_called_once_with("exec-1")


def test_happy_path_finally_releases_all(mocker: MockerFixture):
    """The completed run's finally must release the slot, clear the cancel flag,
    and delete the stored entry."""
    m = _wire_happy_path(mocker)

    result = _run_graph_execution("exec-1", "user-1")

    assert result == {"graph_exec_id": "exec-1", "status": "completed"}
    m["release"].assert_called_once_with("user-1", "exec-1")
    m["clear_cancel"].assert_called_once_with("exec-1")
    m["delete_entry"].assert_called_once_with("exec-1")


def test_cancelled_status_when_cancel_event_set(mocker: MockerFixture):
    """When the engine observed a cooperative cancel (cancel_event set), the task
    reports `cancelled`, not `completed`."""

    def _set_cancel(entry, cancel_event, cluster_lock):
        cancel_event.set()

    m = _wire_happy_path(mocker, on_execution=_set_cancel)

    result = _run_graph_execution("exec-1", "user-1")

    assert result == {"graph_exec_id": "exec-1", "status": "cancelled"}
    m["release"].assert_called_once_with("user-1", "exec-1")


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
