"""api/task_integration.py — Integrates background task system into zen-console.

Call init_task_system() from server.py main() to:
1. Start the background worker daemon
2. Register task routes
"""


def init_task_system():
    """Initialize the background task system. Call from server.py main()."""
    from api.task_worker import start_background_worker
    from api.task_store import get_task_store

    # Ensure DB exists
    store = get_task_store()

    # Clean up any stale running tasks from previous crashes
    fixed = store.cleanup_stale_running(timeout_minutes=30)
    if fixed:
        print(f'  [tasks] Fixed {fixed} stale running tasks', flush=True)

    # Start background worker
    worker = start_background_worker()

    stats = store.count_by_status()
    print(f'  [tasks] Background worker started. Queue: {stats}', flush=True)

    return worker
