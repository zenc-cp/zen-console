/**
 * tasks.js — Background task UI for Hermes.
 *
 * Adds:
 * 1. Background mode toggle (⚡ icon) in the input area
 * 2. Task list panel in the sidebar (under "Tasks" tab)
 * 3. Polling for task status updates
 * 4. Notification badge when tasks complete
 */

/* ─── State ─────────────────────────────────────────────────────────────── */

let bgMode = false;
let taskPanelVisible = false;
const _pollingTasks = new Set();
let _pollTimer = null;

/* ─── Toast (self-contained, typed variant) ─────────────────────────────── */

function showTaskToast(msg, type) {
    const colors = { info: '#64748b', success: '#00ff88', error: '#ff4488', warning: '#ffcc00' };
    const color = colors[type] || colors.info;
    const toast = document.createElement('div');
    toast.style.cssText = [
        'position:fixed;top:16px;left:50%;transform:translateX(-50%);',
        'background:rgba(12,14,26,0.95);border:1px solid ' + color + ';',
        'border-radius:4px;padding:8px 16px;color:' + color + ';',
        'font-family:monospace;font-size:11px;z-index:2000;',
        'transition:opacity 0.3s;pointer-events:none;white-space:nowrap;',
    ].join('');
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(function () {
        toast.style.opacity = '0';
        setTimeout(function () { toast.remove(); }, 300);
    }, 3000);
}

/* ─── HTML escaping ─────────────────────────────────────────────────────── */

function escapeHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ─── Background mode toggle ────────────────────────────────────────────── */

function initBackgroundMode() {
    // Create toggle button
    const toggle = document.createElement('button');
    toggle.id = 'bgModeToggle';
    toggle.innerHTML = '⚡';
    toggle.title = 'Background mode: OFF';
    toggle.style.cssText = [
        'background:none;border:1px solid #334155;border-radius:4px;',
        'color:#64748b;cursor:pointer;padding:4px 8px;font-size:14px;margin-right:4px;',
        'vertical-align:middle;line-height:1;',
    ].join('');

    toggle.onclick = function () {
        bgMode = !bgMode;
        toggle.style.borderColor = bgMode ? '#00ff88' : '#334155';
        toggle.style.color = bgMode ? '#00ff88' : '#64748b';
        toggle.title = 'Background mode: ' + (bgMode ? 'ON' : 'OFF');
        showTaskToast(
            bgMode
                ? 'Background mode ON — tasks run without browser'
                : 'Background mode OFF — interactive streaming',
            'info'
        );
    };

    // Insert before send button inside .composer-right
    var composerRight = document.querySelector('.composer-right');
    if (composerRight) {
        var sendBtn = composerRight.querySelector('#btnSend');
        if (sendBtn) {
            composerRight.insertBefore(toggle, sendBtn);
        } else {
            composerRight.appendChild(toggle);
        }
    }
}

/* ─── Submit background task ────────────────────────────────────────────── */

async function submitBackgroundTask(sessionId, message, model, workspace) {
    try {
        var res = await api('/api/task/submit', {
            method: 'POST',
            body: JSON.stringify({
                session_id: sessionId,
                message: message,
                model: model || '',
                workspace: workspace || '',
            }),
        });
        if (res.ok && res.task) {
            showTaskToast('Task queued — you can close this tab', 'success');
            addTaskCard(res.task);
            startPolling(res.task.task_id);
            return res.task;
        } else {
            showTaskToast('Task error: ' + (res.error || 'unknown'), 'error');
        }
    } catch (e) {
        showTaskToast('Task submit failed: ' + e.message, 'error');
    }
    return null;
}

/* ─── Task list panel ────────────────────────────────────────────────────── */

function createTaskListPanel() {
    if (document.getElementById('taskListPanel')) return;

    var panel = document.createElement('div');
    panel.id = 'taskListPanel';
    panel.style.cssText = [
        'display:none;position:fixed;bottom:60px;right:16px;width:320px;',
        'max-height:400px;overflow-y:auto;background:rgba(12,14,26,0.95);',
        'border:1px solid #1e293b;border-radius:8px;padding:12px;',
        "font-family:'Press Start 2P',monospace;z-index:600;",
    ].join('');
    panel.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
        '  <span style="color:#00ff88;font-size:8px;">⏳ BACKGROUND TASKS</span>' +
        '  <button onclick="toggleTaskPanel()" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:10px;">✕</button>' +
        '</div>' +
        '<div id="taskListContent" style="font-size:7px;color:#94a3b8;"></div>';
    document.body.appendChild(panel);
}

function toggleTaskPanel() {
    taskPanelVisible = !taskPanelVisible;
    var panel = document.getElementById('taskListPanel');
    if (panel) panel.style.display = taskPanelVisible ? 'block' : 'none';
    if (taskPanelVisible) refreshTaskList();
}

/* ─── Task card rendering ────────────────────────────────────────────────── */

function renderTaskCard(task) {
    var statusColors = {
        queued: '#64748b',
        running: '#ffcc00',
        completed: '#00ff88',
        failed: '#ff4488',
        cancelled: '#64748b',
    };
    var statusIcons = {
        queued: '⏳',
        running: '⚙️',
        completed: '✅',
        failed: '❌',
        cancelled: '🚫',
    };
    var color = statusColors[task.status] || '#64748b';
    var icon = statusIcons[task.status] || '?';
    var preview = (task.progress && task.progress.preview) ||
        (task.result && task.result.substring(0, 100)) ||
        (task.prompt && task.prompt.substring(0, 80)) || '';
    var tokens = (task.progress && task.progress.tokens) || 0;

    var cancelBtn = (task.status === 'queued' || task.status === 'running')
        ? '<button onclick="event.stopPropagation();cancelTask(\'' + task.task_id + '\')" ' +
          'style="background:none;border:1px solid #ff4488;color:#ff4488;border-radius:2px;' +
          'padding:2px 6px;margin-top:4px;cursor:pointer;font-size:5px;">Cancel</button>'
        : '';
    var retryBtn = task.status === 'failed'
        ? '<button onclick="event.stopPropagation();retryTask(\'' + task.task_id + '\')" ' +
          'style="background:none;border:1px solid #ffcc00;color:#ffcc00;border-radius:2px;' +
          'padding:2px 6px;margin-top:4px;cursor:pointer;font-size:5px;">Retry</button>'
        : '';
    var tokenLine = task.status === 'running'
        ? '<div style="color:#ffcc00;margin-top:2px;">' + tokens + ' tokens</div>'
        : '';
    var previewLine = task.status === 'completed'
        ? '<div style="color:#64748b;margin-top:2px;overflow:hidden;text-overflow:ellipsis;">' +
          escapeHtml(preview.substring(0, 80)) + '</div>'
        : '';

    return '<div class="task-card" data-task-id="' + task.task_id + '" style="' +
        'background:rgba(30,41,59,0.5);border:1px solid ' + color + '33;border-radius:4px;' +
        'padding:8px;margin-bottom:6px;cursor:pointer;" ' +
        'onclick="viewTaskResult(\'' + task.task_id + '\')">' +
        '  <div style="display:flex;justify-content:space-between;align-items:center;">' +
        '    <span style="color:' + color + ';">' + icon + ' ' + task.status.toUpperCase() + '</span>' +
        '    <span style="color:#475569;font-size:5px;">' + escapeHtml(task.task_id) + '</span>' +
        '  </div>' +
        '  <div style="color:#94a3b8;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' +
        escapeHtml((task.prompt || '').substring(0, 60)) +
        '  </div>' +
        tokenLine +
        previewLine +
        cancelBtn +
        retryBtn +
        '</div>';
}

/* ─── Add / update task cards ────────────────────────────────────────────── */

function addTaskCard(task) {
    // Open panel and prepend card
    if (!taskPanelVisible) toggleTaskPanel();
    var el = document.getElementById('taskListContent');
    if (!el) return;
    var wrapper = document.createElement('div');
    wrapper.innerHTML = renderTaskCard(task);
    el.insertBefore(wrapper.firstChild, el.firstChild);
}

function updateTaskCard(task) {
    var card = document.querySelector('.task-card[data-task-id="' + task.task_id + '"]');
    if (card) {
        var wrapper = document.createElement('div');
        wrapper.innerHTML = renderTaskCard(task);
        card.parentNode.replaceChild(wrapper.firstChild, card);
    }
    if (taskPanelVisible) refreshTaskList();
}

/* ─── Polling ────────────────────────────────────────────────────────────── */

function startPolling(taskId) {
    _pollingTasks.add(taskId);
    if (!_pollTimer) {
        _pollTimer = setInterval(pollTasks, 3000);
    }
}

function stopPolling(taskId) {
    _pollingTasks.delete(taskId);
    if (_pollingTasks.size === 0 && _pollTimer) {
        clearInterval(_pollTimer);
        _pollTimer = null;
    }
}

async function pollTasks() {
    var ids = Array.from(_pollingTasks);
    for (var i = 0; i < ids.length; i++) {
        var taskId = ids[i];
        try {
            var res = await api('/api/task?task_id=' + encodeURIComponent(taskId));
            if (res.task) {
                updateTaskCard(res.task);
                if (['completed', 'failed', 'cancelled'].indexOf(res.task.status) !== -1) {
                    stopPolling(taskId);
                    if (res.task.status === 'completed') {
                        showTaskToast(
                            'Task done: ' + (res.task.prompt || '').substring(0, 40) + '…',
                            'success'
                        );
                        updateNotificationBadge();
                    }
                }
            }
        } catch (e) { /* silent */ }
    }
}

async function refreshTaskList() {
    try {
        var res = await api('/api/tasks?limit=20');
        var el = document.getElementById('taskListContent');
        if (el && res.tasks) {
            if (res.tasks.length === 0) {
                el.innerHTML = '<div style="text-align:center;color:#475569;padding:1rem;">No background tasks</div>';
            } else {
                el.innerHTML = res.tasks.map(renderTaskCard).join('');
            }
            // Resume polling for active tasks
            res.tasks.forEach(function (t) {
                if (t.status === 'queued' || t.status === 'running') {
                    startPolling(t.task_id);
                }
            });
        }
    } catch (e) { /* silent */ }
}

/* ─── Actions ────────────────────────────────────────────────────────────── */

async function cancelTask(taskId) {
    try {
        await api('/api/task/cancel', {
            method: 'POST',
            body: JSON.stringify({ task_id: taskId }),
        });
        showTaskToast('Task cancelled', 'info');
        stopPolling(taskId);
        refreshTaskList();
    } catch (e) {
        showTaskToast('Cancel failed', 'error');
    }
}

async function retryTask(taskId) {
    try {
        var res = await api('/api/task/retry', {
            method: 'POST',
            body: JSON.stringify({ task_id: taskId }),
        });
        if (res.ok && res.task) {
            showTaskToast('Retrying as ' + res.task.task_id, 'info');
            startPolling(res.task.task_id);
            refreshTaskList();
        }
    } catch (e) {
        showTaskToast('Retry failed', 'error');
    }
}

async function viewTaskResult(taskId) {
    try {
        var res = await api('/api/task/result?task_id=' + encodeURIComponent(taskId));
        if (res.result) {
            var modal = document.createElement('div');
            modal.style.cssText = [
                'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:1000;',
                'display:flex;align-items:center;justify-content:center;',
            ].join('');
            modal.innerHTML =
                '<div style="background:#0c0e1a;border:1px solid #1e293b;border-radius:8px;' +
                'padding:16px;max-width:600px;width:90%;max-height:80vh;overflow-y:auto;' +
                'font-family:monospace;color:#e2e8f0;font-size:12px;white-space:pre-wrap;position:relative;">' +
                '  <button onclick="this.closest(\'[data-modal]\').remove()" ' +
                '    style="position:absolute;top:8px;right:8px;background:none;border:none;' +
                '    color:#64748b;cursor:pointer;font-size:16px;">✕</button>' +
                '  <div style="color:#00ff88;margin-bottom:8px;font-size:10px;">Task Result: ' +
                escapeHtml(taskId) + '</div>' +
                escapeHtml(res.result) +
                '</div>';
            // Tag for easy querySelector removal
            modal.setAttribute('data-modal', 'task-result');
            // Fix close button selector now that we use data-modal
            var closeBtn = modal.querySelector('button');
            if (closeBtn) {
                closeBtn.onclick = function () { modal.remove(); };
            }
            modal.addEventListener('click', function (e) {
                if (e.target === modal) modal.remove();
            });
            document.body.appendChild(modal);
        } else if (res.error) {
            showTaskToast('Error: ' + res.error, 'error');
        } else {
            showTaskToast('No result yet — task still running', 'info');
        }
    } catch (e) {
        showTaskToast('Failed to load result', 'error');
    }
}

/* ─── Notification badge ─────────────────────────────────────────────────── */

async function checkNotifications() {
    try {
        var res = await api('/api/notifications/pending');
        if (Array.isArray(res) && res.length > 0) {
            updateNotificationBadge(res.length);
            res.forEach(function (n) {
                if (n.type === 'task_complete') {
                    showTaskToast('Task completed: ' + n.task_id, 'success');
                }
            });
        }
    } catch (e) { /* silent */ }
}

function updateNotificationBadge(count) {
    var badge = document.getElementById('taskNotifBadge');
    if (!badge) {
        badge = document.createElement('span');
        badge.id = 'taskNotifBadge';
        badge.style.cssText = [
            'position:fixed;top:8px;right:80px;background:#ff4488;color:#fff;',
            'border-radius:50%;width:16px;height:16px;display:flex;',
            'align-items:center;justify-content:center;font-size:8px;',
            'z-index:2000;cursor:pointer;',
        ].join('');
        badge.onclick = toggleTaskPanel;
        document.body.appendChild(badge);
    }
    if (count && count > 0) {
        badge.textContent = count;
        badge.style.display = 'flex';
    } else {
        badge.style.display = 'none';
    }
}

// Poll notifications every 30 s
setInterval(checkNotifications, 30000);

/* ─── Send intercept ─────────────────────────────────────────────────────── */

// Patch the global send() so that when bgMode is active we route through
// the task submit API instead of the streaming chat API.
(function () {
    // Wait until send is defined (messages.js loads before tasks.js)
    function patchSend() {
        if (typeof send !== 'function') return;
        var _originalSend = send;
        send = async function () {
            if (!bgMode) return _originalSend.apply(this, arguments);

            var text = (typeof $ === 'function' && $('msg')) ? $('msg').value.trim() : '';
            if (!text) return;

            // Ensure a session exists
            if (typeof S !== 'undefined' && !S.session) {
                if (typeof newSession === 'function') await newSession();
            }

            var sessionId = (typeof S !== 'undefined' && S.session) ? S.session.session_id : '';
            var model = (typeof S !== 'undefined' && S.session) ? (S.session.model || '') : '';
            var workspace = (typeof S !== 'undefined' && S.session) ? (S.session.workspace || '') : '';

            $('msg').value = '';
            if (typeof autoResize === 'function') autoResize();

            await submitBackgroundTask(sessionId, text, model, workspace);
        };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', patchSend);
    } else {
        patchSend();
    }
})();

/* ─── Init ───────────────────────────────────────────────────────────────── */

function initTaskSystem() {
    createTaskListPanel();
    initBackgroundMode();
    // Restore any active tasks from previous session
    refreshTaskList();
    checkNotifications();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTaskSystem);
} else {
    initTaskSystem();
}

/* ─── Exports for external integration ──────────────────────────────────── */

window._bgTaskSubmit = submitBackgroundTask;
window._bgMode = function () { return bgMode; };
window.toggleTaskPanel = toggleTaskPanel;
window.cancelTask = cancelTask;
window.retryTask = retryTask;
window.viewTaskResult = viewTaskResult;
