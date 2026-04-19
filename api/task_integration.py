"""api/task_integration.py — Integrates background task system into zen-console.

Call init_task_system() from server.py main() to:
1. Re-queue dispatch-timeout tasks from previous runs
2. Start the background worker daemon
3. Register task routes
"""


def init_task_system():
    """Initialize the background task system. Call from server.py main()."""
    from api.task_worker import start_background_worker
    from api.task_store import get_task_store

    # Ensure DB exists
    store = get_task_store()

    # Re-queue tasks that failed due to dispatch timeout (worker wasn't running)
    requeued = _requeue_dispatch_timeouts(store)
    if requeued:
        print(f'  [tasks] Re-queued {requeued} dispatch-timeout tasks', flush=True)

    # Clean up any stale running tasks from previous crashes
    fixed = store.cleanup_stale_running(timeout_minutes=30)
    if fixed:
        print(f'  [tasks] Fixed {fixed} stale running tasks', flush=True)

    # Start background worker
    worker = start_background_worker()

    stats = store.count_by_status()
    print(f'  [tasks] Background worker started. Queue: {stats}', flush=True)

    return worker


def _requeue_dispatch_timeouts(store) -> int:
    """Re-queue failed tasks whose error was 'dispatch timeout'.

    These tasks failed because the worker wasn't running when they were queued
    (e.g., server was restarting). Give them another chance.
    """
    import logging
    logger = logging.getLogger(__name__)

    failed_tasks = store.list_tasks(status='failed', limit=200)
    requeued = 0
    for task in failed_tasks:
        error = task.get('error', '')
        if 'dispatch timeout' in error:
            try:
                # Reset to queued so the worker picks them up
                store.update_status(
                    task['task_id'],
                    'queued',
                    error='',
                    started_at='',
                    completed_at='',
                )
                requeued += 1
            except Exception as exc:
                logger.warning('Failed to re-queue task %s: %s', task['task_id'][:8], exc)

    return requeued
