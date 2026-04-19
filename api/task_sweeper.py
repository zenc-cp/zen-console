"""api/task_sweeper.py — Clean up stuck background tasks.

Runs as a daemon thread, checking every 60 seconds for tasks that have been
stuck in 'running' or 'queued' states beyond their allowed time windows:

  - Running tasks older than 120 seconds  → marked 'failed' with error 'timeout'
  - Queued tasks older than 300 seconds   → marked 'failed' with error 'dispatch timeout'

Also cleans up orphaned stream slots from the STREAMS dict that belong to
these timed-out tasks.
"""

import time
import threading
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# How often the sweeper polls (seconds)
_SWEEP_INTERVAL = 60

# Thresholds
_RUNNING_TIMEOUT_SECS = 1800  # 30 minutes — complex agent tasks (research, coding) routinely take 5-60 min
_QUEUED_TIMEOUT_SECS  = 600   # 10 minutes — allow time for server restart + worker spin-up


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(iso_str: str) -> datetime | None:
    """Parse an ISO-8601 datetime string (with or without timezone)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _sweep_once(task_store, streams_dict: dict, streams_lock: threading.Lock) -> dict:
    """Perform one sweep pass. Returns a summary dict for logging."""
    now = _utcnow()
    running_cutoff = now - timedelta(seconds=_RUNNING_TIMEOUT_SECS)
    queued_cutoff  = now - timedelta(seconds=_QUEUED_TIMEOUT_SECS)

    timed_out_running = []
    timed_out_queued  = []

    # --- Check running tasks ---
    try:
        running_tasks = task_store.list_tasks(status='running', limit=200)
    except Exception as exc:
        logger.error("task_sweeper: error fetching running tasks: %s", exc)
        running_tasks = []

    for task in running_tasks:
        task_id     = task.get('task_id', '')
        started_at  = task.get('started_at', '') or task.get('created_at', '')
        started_dt  = _parse_dt(started_at)

        if started_dt is None:
            # Can't determine age — skip
            continue

        # Heartbeat liveness: if updated_at is recent, the worker is still active
        # even if the task has been running a long time. Only kill truly stuck tasks.
        updated_at = task.get('updated_at', '')
        updated_dt = _parse_dt(updated_at)
        if updated_dt is not None:
            idle_secs = int((now - updated_dt).total_seconds())
            if idle_secs < _RUNNING_TIMEOUT_SECS:
                # Still heartbeating — skip
                continue

        if started_dt < running_cutoff:
            age_secs = int((now - started_dt).total_seconds())
            logger.warning(
                "task_sweeper: running task %s stuck for %ds (>%ds) → marking failed",
                task_id, age_secs, _RUNNING_TIMEOUT_SECS,
            )
            try:
                task_store.update_status(
                    task_id,
                    'failed',
                    error=f'timeout: task ran for {age_secs}s without completing',
                    completed_at=now.isoformat(),
                )
                timed_out_running.append(task_id)
            except Exception as exc:
                logger.error("task_sweeper: failed to mark task %s as failed: %s", task_id, exc)

    # --- Check queued tasks ---
    try:
        queued_tasks = task_store.list_tasks(status='queued', limit=200)
    except Exception as exc:
        logger.error("task_sweeper: error fetching queued tasks: %s", exc)
        queued_tasks = []

    for task in queued_tasks:
        task_id    = task.get('task_id', '')
        created_at = task.get('created_at', '')
        created_dt = _parse_dt(created_at)

        if created_dt is None:
            continue

        if created_dt < queued_cutoff:
            age_secs = int((now - created_dt).total_seconds())
            logger.warning(
                "task_sweeper: queued task %s waited %ds (>%ds) without dispatch → marking failed",
                task_id, age_secs, _QUEUED_TIMEOUT_SECS,
            )
            try:
                task_store.update_status(
                    task_id,
                    'failed',
                    error=f'dispatch timeout: task queued for {age_secs}s without being picked up',
                    completed_at=now.isoformat(),
                )
                timed_out_queued.append(task_id)
            except Exception as exc:
                logger.error("task_sweeper: failed to mark queued task %s as failed: %s", task_id, exc)

    # --- Clean up STREAMS dict for all timed-out tasks ---
    all_timed_out = timed_out_running + timed_out_queued
    cleaned_streams = []
    if all_timed_out:
        with streams_lock:
            for task_id in all_timed_out:
                stream_id = f"bg_{task_id}"
                if stream_id in streams_dict:
                    # Put a terminal event in the queue so any live SSE consumer
                    # receives a clean close before the slot is removed
                    try:
                        q = streams_dict[stream_id]
                        q.put_nowait(('error', {
                            'message': 'Task timed out and was forcibly terminated by the sweeper.',
                        }))
                    except Exception:
                        pass
                    del streams_dict[stream_id]
                    cleaned_streams.append(stream_id)
                # Also clean up CANCEL_FLAGS if accessible
                try:
                    from api.config import CANCEL_FLAGS
                    CANCEL_FLAGS.pop(stream_id, None)
                except Exception:
                    pass

    summary = {
        'running_timed_out': len(timed_out_running),
        'queued_timed_out':  len(timed_out_queued),
        'streams_cleaned':   len(cleaned_streams),
        'task_ids':          all_timed_out,
    }

    if any(v > 0 for k, v in summary.items() if isinstance(v, int)):
        logger.info(
            "task_sweeper: pass complete — running_timeout=%d queued_timeout=%d streams_cleaned=%d",
            summary['running_timed_out'],
            summary['queued_timed_out'],
            summary['streams_cleaned'],
        )

    return summary


def _sweeper_loop(task_store, streams_dict: dict, streams_lock: threading.Lock) -> None:
    """Main sweeper loop. Runs forever (daemon thread)."""
    logger.info(
        "task_sweeper: started (interval=%ds, running_timeout=%ds, queued_timeout=%ds)",
        _SWEEP_INTERVAL, _RUNNING_TIMEOUT_SECS, _QUEUED_TIMEOUT_SECS,
    )
    while True:
        try:
            time.sleep(_SWEEP_INTERVAL)
            _sweep_once(task_store, streams_dict, streams_lock)
        except Exception as exc:
            # Never let the sweeper die
            logger.error("task_sweeper: unhandled error in sweep loop: %s", exc, exc_info=True)


def start_task_sweeper(task_store, streams_dict: dict, streams_lock: threading.Lock) -> threading.Thread:
    """Start the task sweeper background thread.

    Runs every 60 seconds. Checks for:
    - Running tasks older than 120s  → mark failed with 'timeout'
    - Queued tasks older than 300s   → mark failed with 'dispatch timeout'

    Also cleans up associated stream entries from *streams_dict*.

    Args:
        task_store:    A TaskStore instance (from api.task_store).
        streams_dict:  The STREAMS dict from api.config (mutated in-place).
        streams_lock:  The STREAMS_LOCK threading.Lock from api.config.

    Returns:
        The started daemon Thread.

    Example (server.py main())::

        from api.task_sweeper import start_task_sweeper
        from api.task_store import get_task_store
        from api.config import STREAMS, STREAMS_LOCK

        start_task_sweeper(get_task_store(), STREAMS, STREAMS_LOCK)
    """
    t = threading.Thread(
        target=_sweeper_loop,
        args=(task_store, streams_dict, streams_lock),
        daemon=True,
        name='task-sweeper',
    )
    t.start()
    return t
