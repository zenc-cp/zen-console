"""api/task_notify.py — Task completion notifications.

Sends notifications when background tasks complete.
Supports Slack webhook and Telegram Bot API.
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_notifications_dir() -> Path:
    from api.config import STATE_DIR
    d = STATE_DIR / 'notifications'
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Notification senders ──────────────────────────────────────────────────────

def _notify_slack(webhook_url: str, task: dict, result: str) -> bool:
    """POST a completion message to a Slack incoming webhook.

    Returns True on HTTP 200, False on any error (never raises).
    """
    text = (
        f"\u2705 Task complete: {task.get('prompt', '')[:100]}\n\n"
        f"Result preview:\n{result[:500]}"
    )
    payload = json.dumps({'text': text}).encode('utf-8')
    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        log.error('Slack notification failed: %s', exc)
        return False


def _notify_telegram(chat_id: str, task: dict, result: str) -> bool:
    """POST a completion message to a Telegram chat via Bot API.

    Requires TELEGRAM_BOT_TOKEN env var.
    Returns True on HTTP 200, False on any error (never raises).
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        log.warning('TELEGRAM_BOT_TOKEN not set; skipping Telegram notification')
        return False

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    text = (
        f"\u2705 Task complete: {task.get('prompt', '')[:100]}\n\n"
        f"{result[:500]}"
    )
    payload = json.dumps({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
    }).encode('utf-8')
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        log.error('Telegram notification failed: %s', exc)
        return False


def _notify_browser_flag(task_id: str) -> None:
    """Write a flag file so the frontend can poll /api/notifications/pending."""
    notifications_dir = _get_notifications_dir()
    flag_path = notifications_dir / f'{task_id}.json'
    content = {
        'task_id': task_id,
        'type': 'task_complete',
        'created_at': _utcnow_iso(),
    }
    flag_path.write_text(json.dumps(content), encoding='utf-8')


# ── Public API ────────────────────────────────────────────────────────────────

def notify_task_complete(task: dict, result: str) -> dict:
    """Send all configured notifications for a completed task.

    Reads ``notify_config`` from the task dict (already deserialized by the
    task store, or a raw JSON string — handled transparently).

    Returns a summary dict: {"slack": bool, "telegram": bool, "errors": list}
    """
    notify_config = task.get('notify_config', {})
    if isinstance(notify_config, str):
        try:
            notify_config = json.loads(notify_config)
        except (json.JSONDecodeError, TypeError):
            notify_config = {}

    results = {'slack': False, 'telegram': False, 'errors': []}

    slack_url = notify_config.get('slack_url', '').strip()
    if slack_url:
        try:
            results['slack'] = _notify_slack(slack_url, task, result)
        except Exception as exc:
            results['errors'].append(f'slack: {exc}')

    telegram_chat_id = notify_config.get('telegram_chat_id', '').strip()
    if telegram_chat_id:
        try:
            results['telegram'] = _notify_telegram(telegram_chat_id, task, result)
        except Exception as exc:
            results['errors'].append(f'telegram: {exc}')

    # Always write a browser flag so the UI can show an in-app notification
    try:
        _notify_browser_flag(task.get('task_id', 'unknown'))
    except Exception as exc:
        results['errors'].append(f'browser_flag: {exc}')

    return results


def get_pending_notifications() -> list[dict]:
    """Read all pending notification files and consume them (delete after read).

    Returns a list of notification dicts sorted by created_at descending.
    """
    notifications_dir = _get_notifications_dir()
    items = []
    for path in sorted(notifications_dir.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            items.append(data)
            path.unlink(missing_ok=True)
        except Exception as exc:
            log.error('Failed to read notification file %s: %s', path, exc)

    # Sort by created_at descending (newest first)
    items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return items


def clear_notifications() -> int:
    """Delete all pending notification files. Returns the number removed."""
    notifications_dir = _get_notifications_dir()
    count = 0
    for path in notifications_dir.glob('*.json'):
        try:
            path.unlink(missing_ok=True)
            count += 1
        except Exception as exc:
            log.error('Failed to delete notification file %s: %s', path, exc)
    return count
