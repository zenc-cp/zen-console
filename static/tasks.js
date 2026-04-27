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
    var toggle = document.createElement('button');
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
        // Show/hide the composer bg banner
        var banner = document.getElementById('bgModeBanner');
        if (banner) banner.style.display = bgMode ? 'flex' : 'none';
        // Change composer border to indicate bg mode
        var wrap = document.getElementById('composerWrap');
        if (wrap) wrap.style.borderColor = bgMode ? 'rgba(255,204,0,0.5)' : '';
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

    // Create background mode banner above the composer
    var banner = document.createElement('div');
    banner.id = 'bgModeBanner';
    banner.style.cssText = [
        'display:none;align-items:center;gap:6px;',
        'background:rgba(255,204,0,0.08);border:1px solid rgba(255,204,0,0.25);',
        'border-radius:4px;padding:4px 10px;margin-bottom:4px;',
        'font-family:monospace;font-size:10px;color:#ffcc00;',
    ].join('');
    banner.innerHTML = '<span>⚡ BG MODE</span><span style="color:#64748b;font-size:9px;">Tasks run in background — close tab safely</span>';
    var composerBox = document.getElementById('composerBox');
    if (composerBox) {
        composerBox.parentElement.insertBefore(banner, composerBox);
    }
}

/* ─── Submit background task ────────────────────────────────────────────── */

async function submitBackgroundTask(sessionId, message, model, workspace, profile) {
    try {
        var res = await api('/api/task/submit', {
            method: 'POST',
            body: JSON.stringify({
                session_id: sessionId,
                message: message,
                model: model || '',
                workspace: workspace || '',
                profile: profile || '',
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
        'display:none;position:fixed;bottom:60px;right:16px;width:360px;',
        'max-height:450px;overflow-y:auto;background:rgba(12,14,26,0.97);',
        'border:1px solid #1e293b;border-radius:8px;padding:12px;',
        "font-family:'Press Start 2P',monospace;z-index:600;",
    ].join('');
    panel.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
        '  <span style="color:#00ff88;font-size:8px;">⏳ BACKGROUND TASKS</span>' +
        '  <div style="display:flex;gap:6px;align-items:center;">' +
        '    <span id="taskCountBadge" style="color:#64748b;font-size:7px;"></span>' +
        '    <button onclick="toggleTaskPanel()" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:10px;">✕</button>' +
        '  </div>' +
        '</div>' +
        '<div id="taskListContent" style="font-size:7px;color:#94a3b8;"></div>';
    document.body.appendChild(panel);
}

function toggleTaskPanel() {
    taskPanelVisible = !taskPanelVisible;
    var panel = document.getElementById('taskListPanel');
    if (panel) panel.style.display = taskPanelVisible ? 'block' : 'none';
    var btn = document.getElementById('btnBgTasks');
    if (btn) {
        btn.style.background = taskPanelVisible ? 'rgba(0,255,136,0.15)' : '';
        btn.style.borderColor = taskPanelVisible ? '#00ff88' : '';
    }
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

    // Duration display
    var duration = '';
    if (task.started_at) {
        var startTime = new Date(task.started_at).getTime();
        var endTime = task.completed_at ? new Date(task.completed_at).getTime() : Date.now();
        var durSec = Math.floor((endTime - startTime) / 1000);
        if (durSec < 60) duration = durSec + 's';
        else if (durSec < 3600) duration = Math.floor(durSec/60) + 'm' + (durSec%60 < 10 ? '0' : '') + (durSec%60) + 's';
        else duration = Math.floor(durSec/3600) + 'h' + Math.floor((durSec%3600)/60) + 'm';
    }

    var watchBtn = task.status === 'running'
        ? '<button onclick="event.stopPropagation();watchTask(\'' + task.task_id + '\')" ' +
          'style="background:none;border:1px solid #00ff88;color:#00ff88;border-radius:2px;' +
          'padding:2px 6px;margin-top:4px;cursor:pointer;font-size:6px;margin-right:4px;">Watch Live</button>'
        : '';
    var cancelBtn = (task.status === 'queued' || task.status === 'running')
        ? '<button onclick="event.stopPropagation();cancelTask(\'' + task.task_id + '\')" ' +
          'style="background:none;border:1px solid #ff4488;color:#ff4488;border-radius:2px;' +
          'padding:2px 6px;margin-top:4px;cursor:pointer;font-size:6px;">Cancel</button>'
        : '';
    var retryBtn = task.status === 'failed'
        ? '<button onclick="event.stopPropagation();retryTask(\'' + task.task_id + '\')" ' +
          'style="background:none;border:1px solid #ffcc00;color:#ffcc00;border-radius:2px;' +
          'padding:2px 6px;margin-top:4px;cursor:pointer;font-size:6px;">Retry</button>'
        : '';
    var tokenLine = task.status === 'running'
        ? '<div style="color:#ffcc00;margin-top:2px;">' + tokens + ' tokens' + (duration ? ' · ' + duration : '') + '</div>'
        : '';
    // Duration line for completed/failed
    var durationLine = (task.status !== 'running' && duration)
        ? '<div style="color:#475569;margin-top:2px;font-size:6px;">⏱ ' + duration + '</div>'
        : '';
    // For completed tasks: expandable inline log — auto-show on first completion
    // Use innerHTML with formatResultText so code blocks / headings render
    var logId = 'tlog_' + escapeHtml(task.task_id);
    var wrapId = 'tlw_' + escapeHtml(task.task_id);
    var logSnippet = task.status === 'completed'
        ? '<div style="margin-top:4px;" class="task-log-wrap" id="' + wrapId + '">' +
          '<button onclick="event.stopPropagation();toggleTaskLog(\'' + escapeHtml(task.task_id) + '\')" ' +
          'style="background:none;border:1px solid #334155;color:#64748b;border-radius:2px;' +
          'padding:2px 6px;cursor:pointer;font-size:6px;">📋 Show log</button>' +
          '<div id="' + logId + '" style="display:none;margin-top:6px;' +
          'background:rgba(0,0,0,0.3);border:1px solid #1e293b;border-radius:4px;' +
          'padding:6px 8px;max-height:200px;overflow-y:auto;' +
          'font-family:monospace;font-size:10px;color:#94a3b8;white-space:pre-wrap;text-align:left;word-break:break-all;">' +
          formatResultText((task.result || preview || '').substring(0, 2000)) +
          '</div></div>'
        : '';
    // Context badges: workspace + profile + model
    var badges = [];
    if (task.workspace) badges.push('<span style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.25);border-radius:2px;padding:1px 3px;font-size:5px;color:#00ff88;">📂 ' + escapeHtml(task.workspace.split('/').pop()) + '</span>');
    if (task.profile) badges.push('<span style="background:rgba(100,116,139,0.15);border:1px solid rgba(100,116,139,0.25);border-radius:2px;padding:1px 3px;font-size:5px;color:#94a3b8;">👤 ' + escapeHtml(task.profile) + '</span>');
    if (task.model) badges.push('<span style="background:rgba(255,204,0,0.1);border:1px solid rgba(255,204,0,0.25);border-radius:2px;padding:1px 3px;font-size:5px;color:#ffcc00;">🤖 ' + escapeHtml(task.model.split('/').pop()) + '</span>');
    var badgeLine = badges.length
        ? '<div style="display:flex;gap:4px;margin-top:3px;flex-wrap:wrap;">' + badges.join('') + '</div>'
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
        escapeHtml((task.prompt || '').substring(0, 80)) +
        '  </div>' +
        tokenLine +
        durationLine +
        logSnippet +
        badgeLine +
        '  <div style="margin-top:4px;">' + watchBtn + cancelBtn + retryBtn + '</div>' +
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
        _pollTimer = setInterval(pollTasks, 6000);
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
                        // Inject result into main chat stream if this is the active session
                        injectTaskResultIntoChat(res.task);
                    } else if (res.task.status === 'failed') {
                        showTaskToast(
                            'Task failed: ' + (res.task.prompt || '').substring(0, 40) + '…',
                            'error'
                        );
                    }
                    // Auto-show log for completed or failed tasks
                    setTimeout(function() { autoShowTaskLog(taskId); }, 100);
                    // Show floating result banner at bottom of screen
                    showResultBanner(taskId);
                }
            }
        } catch (e) { /* silent */ }
    }
}

/* ─── Result formatter ─────────────────────────────────────────────────── */

/** Convert plain text / light markdown into HTML for the log display.
 *  Handles: code blocks (```...```), inline `code`, **bold**, headings (##),
 *  and preserves line breaks. Does NOT require external libs. */
function formatResultText(text) {
    if (!text) return '';
    // Escape HTML entities first
    var escaped = escapeHtml(text);
    // Code blocks: triple-backtick delimited blocks
    escaped = escaped.replace(/```(\w*)\n?([\s\S]*?)```/g, function(_, lang, code) {
        var langLabel = lang ? '<span style="color:#00ff88;">' + escapeHtml(lang) + '</span> ' : '';
        return '<div style="background:rgba(0,0,0,0.4);border:1px solid #1e293b;border-radius:4px;padding:8px;margin:4px 0;overflow-x:auto;">' +
               '<div style="color:#64748b;font-size:8px;margin-bottom:4px;font-family:monospace;">' + langLabel + 'code</div>' +
               '<pre style="margin:0;color:#c4cfe0;font-size:11px;white-space:pre-wrap;word-break:break-all;font-family:monospace;">' + code + '</pre></div>';
    });
    // Inline code: `code`
    escaped = escaped.replace(/`([^`]+)`/g, '<code style="background:rgba(100,116,139,0.2);border:1px solid rgba(100,116,139,0.3);border-radius:3px;padding:1px 4px;font-size:11px;color:#94a3b8;font-family:monospace;">$1</code>');
    // Headings: ## Text  →  <strong>Text</strong> with accent
    escaped = escaped.replace(/^## (.+)$/gm, '<div style="color:#00ff88;font-size:11px;font-family:monospace;margin:8px 0 2px;border-left:2px solid #00ff88;padding-left:6px;">$1</div>');
    escaped = escaped.replace(/^### (.+)$/gm, '<div style="color:#00ff88;font-size:10px;font-family:monospace;margin:6px 0 2px;border-left:2px solid #334155;padding-left:6px;">$1</div>');
    // Bold: **text**
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong style="color:#e2e8f0;">$1</strong>');
    // Line breaks → <br>
    escaped = escaped.replace(/\n/g, '<br>');
    return escaped;
}

/* ─── Auto-show log on task completion ─────────────────────────────────── */

async function autoShowTaskLog(taskId) {
    var logDiv = document.getElementById('tlog_' + taskId);
    var wrapDiv = document.getElementById('tlw_' + taskId);
    if (!logDiv) return;

    // Lazy-load full result from server
    var duration = '';
    var charCount = 0;
    try {
        var res = await api('/api/task/result?task_id=' + encodeURIComponent(taskId));
        if (res) {
            duration = res.duration || '';
            charCount = (res.result || '').length;
            if (res.result) {
                logDiv.innerHTML = formatResultText(res.result);
            } else if (res.error) {
                logDiv.innerHTML = '<div style="color:#ff4488;font-family:monospace;font-size:11px;">[Error] ' + escapeHtml(res.error) + '</div>';
            }
        }
    } catch (e) { /* keep cached text */ }

    // Show the log
    logDiv.style.display = 'block';

    // Add metadata header above the log if not already present
    if (wrapDiv) {
        var existingMeta = wrapDiv.querySelector('.task-log-meta');
        if (!existingMeta && (duration || charCount > 0)) {
            var metaDiv = document.createElement('div');
            metaDiv.className = 'task-log-meta';
            metaDiv.style.cssText = 'display:flex;gap:8px;margin-bottom:4px;flex-wrap:wrap;';
            var parts = [];
            if (duration) parts.push('<span style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.25);border-radius:2px;padding:1px 5px;font-size:6px;color:#00ff88;font-family:monospace;">⏱ ' + escapeHtml(duration) + '</span>');
            if (charCount > 0) parts.push('<span style="background:rgba(100,116,139,0.15);border:1px solid rgba(100,116,139,0.25);border-radius:2px;padding:1px 5px;font-size:6px;color:#94a3b8;font-family:monospace;">' + charCount.toLocaleString() + ' chars</span>');
            metaDiv.innerHTML = parts.join('');
            wrapDiv.insertBefore(metaDiv, wrapDiv.firstChild);
        }
        var btn = wrapDiv.querySelector('button');
        if (btn) btn.textContent = '📋 Hide log';
    }
    logDiv.scrollTop = 0;
}

/* ─── Floating result banner ─────────────────────────────────────────────── */

/** Show a dismissible floating banner with the completed task result.
 *  Appears at the bottom of the screen when a bg task finishes. */
async function showResultBanner(taskId) {
    try {
        var res = await api('/api/task/result?task_id=' + encodeURIComponent(taskId));
        if (!res || (!res.result && !res.error)) return;

        var existing = document.getElementById('resultBanner_' + taskId);
        if (existing) existing.remove();

        var isError = !!res.error;
        var bannerColor = isError ? '#ff4488' : '#00ff88';
        var label = isError ? 'Task failed' : 'Task complete';
        var resultText = res.result || ('[Error] ' + res.error);
        var duration = res.duration || '';
        var charCount = (res.result || '').length;

        // Preview: first 300 chars, stripped of markdown for banner
        var preview = resultText.substring(0, 300).replace(/```[\s\S]*?```/g, '[code]').replace(/`[^`]+`/g, '[code]').replace(/\*\*([^*]+)\*\*/g, '$1').replace(/\n+/g, ' ').trim();
        if (resultText.length > 300) preview += '…';

        var banner = document.createElement('div');
        banner.id = 'resultBanner_' + taskId;
        banner.style.cssText = [
            'position:fixed;bottom:16px;left:50%;transform:translateX(-50%);',
            'width:min(680px, 94vw);',
            'background:rgba(12,14,26,0.97);',
            'border:1px solid ' + bannerColor + ';',
            'border-radius:8px;padding:12px 16px;',
            'z-index:1200;',
            'display:flex;flex-direction:column;gap:6px;',
            'box-shadow: 0 4px 32px rgba(0,0,0,0.6);',
        ].join('');

        banner.innerHTML =
            '<div style="display:flex;align-items:center;gap:8px;">' +
            '  <span style="color:' + bannerColor + ';font-family:monospace;font-size:10px;">' + label + '</span>' +
            (duration ? '<span style="color:#475569;font-family:monospace;font-size:9px;">· ' + escapeHtml(duration) + '</span>' : '') +
            (charCount > 0 ? '<span style="color:#475569;font-family:monospace;font-size:9px;">· ' + charCount.toLocaleString() + ' chars</span>' : '') +
            '  <div style="margin-left:auto;display:flex;gap:6px;">' +
            '    <button onclick="viewTaskResult(\'' + taskId + '\')" style="background:none;border:1px solid #334155;color:#94a3b8;border-radius:3px;padding:3px 8px;cursor:pointer;font-size:9px;font-family:monospace;">View full</button>' +
            '    <button onclick="copyTaskResult(\'' + taskId + '\')" title="Copy to clipboard" style="background:none;border:1px solid #334155;color:#64748b;border-radius:3px;padding:3px 8px;cursor:pointer;font-size:9px;font-family:monospace;">Copy</button>' +
            '    <button onclick="this.closest(\'[id^=resultBanner_]\').remove()" style="background:none;border:none;color:#475569;cursor:pointer;font-size:14px;padding:0 2px;">✕</button>' +
            '  </div>' +
            '</div>' +
            '<div style="font-family:monospace;font-size:11px;color:#94a3b8;white-space:pre-wrap;max-height:120px;overflow:hidden;text-overflow:ellipsis;line-height:1.5;">' +
            escapeHtml(preview) +
            '</div>';

        document.body.appendChild(banner);

        // Auto-dismiss after 30s
        setTimeout(function() {
            var b = document.getElementById('resultBanner_' + taskId);
            if (b) {
                b.style.opacity = '0';
                b.style.transition = 'opacity 0.5s';
                setTimeout(function() { b.remove(); }, 500);
            }
        }, 30000);
    } catch (e) { /* silent */ }
}

var _refreshDebounce = null;
async function refreshTaskList() {
    if (_refreshDebounce) return;
    _refreshDebounce = setTimeout(function(){ _refreshDebounce = null; }, 2000);
    try {
        var res = await api('/api/tasks?limit=20');
        var el = document.getElementById('taskListContent');
        if (el && res.tasks) {
            // Filter: show active tasks + recently finished (last 5 min)
            var now = Date.now() / 1000;
            var visible = res.tasks.filter(function(t) {
                if (t.status === 'queued' || t.status === 'running') return true;
                // Show completed/failed/cancelled only if < 5 min old
                var age = now - (t.updated_at || t.created_at || 0);
                return age < 300;
            });
            if (visible.length === 0) {
                el.innerHTML = '<div style="text-align:center;color:#475569;padding:1rem;">No background tasks</div>';
            } else {
                el.innerHTML = visible.map(renderTaskCard).join('');
            }
            // Update count badge
            var badge = document.getElementById('taskCountBadge');
            if (badge) {
                var counts = {running: 0, queued: 0, completed: 0};
                res.tasks.forEach(function(t) { if (counts[t.status] !== undefined) counts[t.status]++; });
                var parts = [];
                if (counts.running) parts.push('⚙️' + counts.running);
                if (counts.queued) parts.push('⏳' + counts.queued);
                badge.textContent = parts.join(' ') || '';
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

/* ─── Expandable task log ─────────────────────────────────────────────────── */

async function toggleTaskLog(taskId) {
    var logDiv = document.getElementById('tlog_' + taskId);
    var wrapDiv = document.getElementById('tlw_' + taskId);
    if (!logDiv) return;

    var btn = wrapDiv ? wrapDiv.querySelector('button') : null;
    if (logDiv.style.display === 'none') {
        // Lazy-load full result if we only have a cached preview
        var logText = logDiv.textContent || '';
        if (logText.length < 200) {
            try {
                var res = await api('/api/task/result?task_id=' + encodeURIComponent(taskId));
                if (res && res.result) {
                    logText = res.result;
                    logDiv.innerHTML = formatResultText(logText);
                }
            } catch (e) { /* keep cached text */ }
        }
        logDiv.style.display = 'block';
        if (btn) btn.textContent = '📋 Hide log';
        logDiv.scrollTop = 0;
    } else {
        logDiv.style.display = 'none';
        if (btn) btn.textContent = '📋 Show log';
    }
}

/* ─── View full result modal ─────────────────────────────────────────────── */

async function viewTaskResult(taskId) {
    try {
        var res = await api('/api/task/result?task_id=' + encodeURIComponent(taskId));
        if (res.result) {
            var modal = document.createElement('div');
            modal.style.cssText = [
                'position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:1000;',
                'display:flex;align-items:center;justify-content:center;',
            ].join('');
            modal.innerHTML =
                '<div style="background:#0c0e1a;border:1px solid #1e293b;border-radius:8px;' +
                'padding:20px;width:94%;max-width:860px;max-height:88vh;overflow-y:auto;' +
                'font-family:monospace;color:#e2e8f0;font-size:12px;white-space:pre-wrap;' +
                'position:relative;display:flex;flex-direction:column;gap:10px;">' +
                '  <div style="display:flex;justify-content:space-between;align-items:center;' +
                '    border-bottom:1px solid #1e293b;padding-bottom:8px;margin-bottom:4px;">' +
                '    <span style="color:#00ff88;font-size:10px;">Task Result: ' +
                escapeHtml(taskId) + '</span>' +
                '    <div style="display:flex;gap:8px;align-items:center;">' +
                '      <span style="color:#475569;font-size:9px;">' +
                escapeHtml((res.result || '').length + ' chars') + '</span>' +
                '      <button onclick="copyTaskResult(\'' + escapeHtml(taskId) + '\')" ' +
                '        title="Copy to clipboard" ' +
                '        style="background:none;border:1px solid #334155;color:#64748b;' +
                '        border-radius:3px;padding:3px 8px;cursor:pointer;font-size:9px;">📋 Copy</button>' +
                '      <button onclick="this.closest(\'[data-modal]\').remove()" ' +
                '        style="background:none;border:none;color:#64748b;' +
                '        cursor:pointer;font-size:16px;padding:0 4px;">✕</button>' +
                '    </div>' +
                '  </div>' +
                '  <div id="taskResultContent" style="flex:1;overflow-y:auto;color:#c4cfe0;' +
                '  line-height:1.6;">' + formatResultText(res.result) + '</div>' +
                '</div>';
            modal.setAttribute('data-modal', 'task-result');
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

/* Copy result text from modal for reuse */
function copyTaskResult(taskId) {
    var content = document.getElementById('taskResultContent');
    if (content && navigator.clipboard) {
        navigator.clipboard.writeText(content.textContent || '').then(function() {
            showTaskToast('Copied!', 'success');
        }).catch(function() {
            showTaskToast('Copy failed', 'error');
        });
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
            var profile = (typeof getActiveProfileName === 'function') ? getActiveProfileName() : '';

            $('msg').value = '';
            if (typeof autoResize === 'function') autoResize();

            await submitBackgroundTask(sessionId, text, model, workspace, profile);
        };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', patchSend);
    } else {
        patchSend();
    }
})();

/* ── Floating running-task status bar ─────────────────────────────────────── */

function _formatElapsed(startedAt) {
    if (!startedAt) return '';
    try {
        var start = new Date(startedAt).getTime();
        var now = Date.now();
        var sec = Math.floor((now - start) / 1000);
        if (sec < 60) return sec + 's';
        var m = Math.floor(sec / 60);
        var s = sec % 60;
        return m + 'm' + (s < 10 ? '0' : '') + s + 's';
    } catch(e) { return ''; }
}

function _startElapsedTicker() {
    if (window._elapsedTicker) return;
    window._elapsedTicker = setInterval(function() {
        var bar = document.getElementById('runningTaskBar');
        if (!bar || bar.style.display === 'none') return;
        // Update all elapsed displays
        bar.querySelectorAll('[data-elapsed-start]').forEach(function(el) {
            el.textContent = _formatElapsed(el.getAttribute('data-elapsed-start'));
        });
    }, 1000);
}

function updateRunningTaskBar() {
    var bar = document.getElementById('runningTaskBar');
    // Fetch all tasks to find running ones
    api('/api/tasks?limit=20').then(function(res) {
        if (!res || !res.tasks) return;
        var running = res.tasks.filter(function(t) { return t.status === 'running'; });
        var queued = res.tasks.filter(function(t) { return t.status === 'queued'; });

        if (running.length === 0 && queued.length === 0) {
            if (bar) bar.style.display = 'none';
            return;
        }

        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'runningTaskBar';
            bar.style.cssText = [
                'position:sticky;top:0;z-index:500;',
                'background:rgba(12,14,26,0.97);border-bottom:1px solid #1e293b;',
                'padding:6px 12px;font-family:monospace;font-size:10px;color:#94a3b8;',
            ].join('');
            // Insert above the chat messages area
            var msgInner = document.getElementById('msgInner');
            if (msgInner && msgInner.parentElement) {
                msgInner.parentElement.insertBefore(bar, msgInner);
            }
        }

        var html = '';
        running.forEach(function(t) {
            var tokens = (t.progress && t.progress.tokens) || 0;
            var tool = (t.progress && t.progress.current_tool) || '';
            var preview = (t.progress && t.progress.preview) || '';
            var elapsed = _formatElapsed(t.started_at);
            var elapsedAttr = t.started_at ? ' data-elapsed-start="' + escapeHtml(t.started_at) + '"' : '';

            // Workspace badge
            var ws = t.workspace ? escapeHtml(t.workspace.split('/').pop()) : '';
            var wsBadge = ws ? '<span style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);border-radius:3px;padding:1px 4px;margin-right:6px;font-size:8px;color:#00ff88;">📂 ' + ws + '</span>' : '';

            // Profile badge
            var prof = t.profile ? escapeHtml(t.profile) : '';
            var profBadge = prof ? '<span style="background:rgba(100,116,139,0.2);border:1px solid rgba(100,116,139,0.3);border-radius:3px;padding:1px 4px;margin-right:6px;font-size:8px;color:#94a3b8;">👤 ' + prof + '</span>' : '';

            // Model badge
            var mdl = t.model ? escapeHtml(t.model.split('/').pop()) : '';
            var mdlBadge = mdl ? '<span style="background:rgba(255,204,0,0.1);border:1px solid rgba(255,204,0,0.3);border-radius:3px;padding:1px 4px;margin-right:6px;font-size:8px;color:#ffcc00;">🤖 ' + mdl + '</span>' : '';

            // Tool line (prominent)
            var toolLine = tool
                ? '<div style="margin-top:3px;display:flex;align-items:center;gap:6px;">' +
                  '<span style="color:#ffcc00;font-size:9px;">🔧 ' + escapeHtml(tool) + '</span>' +
                  '<span style="color:#475569;">|</span>' +
                  '<span style="color:#64748b;">' + tokens + ' tokens</span>' +
                  '</div>'
                : (tokens ? '<div style="margin-top:3px;color:#64748b;">' + tokens + ' tokens</div>' : '');

            // Preview snippet (last line of output)
            var previewSnippet = preview ? escapeHtml(preview.substring(preview.length - 80)) : '';
            var previewLine = previewSnippet
                ? '<div style="margin-top:2px;color:#475569;max-width:600px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:9px;">' + previewSnippet + '</div>'
                : '';

            html += '<div style="padding:4px 0;border-bottom:1px solid #1e293b;margin-bottom:4px;">' +
                '<div style="display:flex;align-items:center;gap:8px;">' +
                '  <span style="color:#ffcc00;font-weight:bold;">⚙️ ' + escapeHtml((t.prompt || '').substring(0, 50)) + '</span>' +
                '  <span style="color:#ffcc00;font-size:9px;"' + elapsedAttr + '>' + elapsed + '</span>' +
                '  <button onclick="event.stopPropagation();watchTask(\'' + t.task_id + '\')" style="background:none;border:1px solid #00ff88;color:#00ff88;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:9px;margin-left:auto;">Watch Live</button>' +
                '</div>' +
                '<div style="margin-top:3px;">' + wsBadge + profBadge + mdlBadge + '</div>' +
                toolLine +
                previewLine +
                '</div>';
        });
        if (queued.length > 0) {
            html += '<div style="color:#64748b;padding-top:4px;">⏳ ' + queued.length + ' queued</div>';
        }
        bar.innerHTML = html;
        bar.style.display = 'block';
        _startElapsedTicker();
    }).catch(function() {});
}

// Update the bar on each poll cycle
var _origPoll = pollTasks;
pollTasks = async function() {
    await _origPoll();
    updateRunningTaskBar();
};
// Also update when panel refreshes
var _origRefresh = refreshTaskList;
refreshTaskList = async function() {
    await _origRefresh();
    updateRunningTaskBar();
};

/* ─── Task panel toggle button ─────────────────────────────────────────────── */

function createBgTasksButton() {
    if (document.getElementById('btnBgTasks')) return;
    var btn = document.createElement('button');
    btn.id = 'btnBgTasks';
    btn.innerHTML = '⏳';
    btn.title = 'Background Tasks';
    btn.style.cssText = [
        'position:fixed;top:12px;right:116px;background:rgba(12,14,26,0.9);',
        'border:1px solid #1e293b;border-radius:6px;width:32px;height:32px;',
        'display:flex;align-items:center;justify-content:center;',
        'cursor:pointer;z-index:1100;color:#64748b;font-size:14px;',
        'transition:border-color 0.2s,color 0.2s;',
    ].join('');
    btn.onclick = toggleTaskPanel;
    document.body.appendChild(btn);
}

/* ─── Init ───────────────────────────────────────────────────────────────── */

function initTaskSystem() {
    createTaskListPanel();
    createBgTasksButton();
    initBackgroundMode();
    // Restore any active tasks from previous session
    refreshTaskList();
    checkNotifications();
    // Show running-task bar if tasks are active
    updateRunningTaskBar();
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
window.toggleTaskLog = toggleTaskLog;
window.copyTaskResult = copyTaskResult;

// ── Live task stream viewer ──────────────────────────────────────────────────

function watchTask(taskId) {
    // Open SSE stream to watch a running task in real-time
    const prefix = (location.pathname.match(/^\/[^\/]+/) || [''])[0];
    const url = `${location.origin}${prefix}/api/task/stream?task_id=${taskId}`;
    
    // Create modal for live viewing
    const modal = document.createElement('div');
    modal.id = 'taskStreamModal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:1000;display:flex;align-items:center;justify-content:center;';
    modal.innerHTML = `
        <div style="background:#0c0e1a;border:1px solid #1e293b;border-radius:8px;padding:16px;width:90%;max-width:700px;max-height:80vh;display:flex;flex-direction:column;position:relative;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span style="color:#00ff88;font-family:monospace;font-size:11px;">⚡ LIVE: ${taskId}</span>
                <div>
                    <button id="taskStreamCancel" style="background:none;border:1px solid #ff4488;color:#ff4488;border-radius:4px;padding:4px 8px;cursor:pointer;font-size:10px;margin-right:8px;">Cancel Task</button>
                    <button id="taskStreamClose" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:16px;">✕</button>
                </div>
            </div>
            <div id="taskStreamThinking" style="display:none;background:rgba(100,116,139,0.1);border:1px solid #334155;border-radius:4px;padding:8px;margin-bottom:8px;max-height:120px;overflow-y:auto;">
                <div style="color:#64748b;font-size:9px;margin-bottom:4px;">💭 Thinking...</div>
                <pre id="taskStreamThinkingText" style="color:#94a3b8;font-size:10px;margin:0;white-space:pre-wrap;font-family:monospace;"></pre>
            </div>
            <div id="taskStreamContent" style="flex:1;overflow-y:auto;font-family:monospace;color:#e2e8f0;font-size:12px;white-space:pre-wrap;padding:8px;background:rgba(30,41,59,0.3);border-radius:4px;min-height:200px;"></div>
            <div id="taskStreamStatus" style="margin-top:8px;color:#64748b;font-size:9px;font-family:monospace;">Connecting...</div>
        </div>
    `;
    document.body.appendChild(modal);
    
    const contentEl = document.getElementById('taskStreamContent');
    const thinkingEl = document.getElementById('taskStreamThinking');
    const thinkingText = document.getElementById('taskStreamThinkingText');
    const statusEl = document.getElementById('taskStreamStatus');
    let tokenCount = 0;
    let es = null;
    
    function closeStream() {
        if (es) { es.close(); es = null; }
        modal.remove();
    }
    
    document.getElementById('taskStreamClose').onclick = closeStream;
    modal.onclick = (e) => { if (e.target === modal) closeStream(); };
    document.getElementById('taskStreamCancel').onclick = () => {
        cancelTask(taskId);
        closeStream();
    };
    
    // Open EventSource
    es = new EventSource(url);
    
    es.addEventListener('token', (e) => {
        try {
            const data = JSON.parse(e.data);
            const text = data.text || '';
            contentEl.textContent += text;
            tokenCount++;
            statusEl.textContent = `⚡ Streaming... ${tokenCount} tokens`;
            contentEl.scrollTop = contentEl.scrollHeight;
        } catch(err) {}
    });
    
    es.addEventListener('thinking', (e) => {
        try {
            const data = JSON.parse(e.data);
            const text = data.text || '';
            thinkingEl.style.display = 'block';
            thinkingText.textContent += text;
            thinkingText.scrollTop = thinkingText.scrollHeight;
        } catch(err) {}
    });
    
    es.addEventListener('tool_call', (e) => {
        try {
            const data = JSON.parse(e.data);
            statusEl.textContent = `🔧 Tool: ${data.name || 'unknown'}`;
        } catch(err) {}
    });
    
    es.addEventListener('done', (e) => {
        statusEl.textContent = `✅ Complete — ${tokenCount} tokens`;
        statusEl.style.color = '#00ff88';
        if (es) es.close();
        refreshTaskList();
    });
    
    es.addEventListener('error', (e) => {
        // SSE error can mean stream ended or connection lost
        if (es && es.readyState === EventSource.CLOSED) {
            statusEl.textContent = '⏹ Stream ended';
        } else {
            statusEl.textContent = '❌ Connection lost — task continues in background';
            statusEl.style.color = '#ff4488';
        }
        if (es) es.close();
    });
    
    es.addEventListener('cancel', (e) => {
        statusEl.textContent = '🚫 Task cancelled';
        statusEl.style.color = '#64748b';
        if (es) es.close();
    });
    
    es.onerror = () => {
        // Suppress default EventSource reconnect
        if (es) es.close();
        es = null;
        if (statusEl.textContent === 'Connecting...') {
            statusEl.textContent = '❌ Failed to connect — task may have already finished';
            statusEl.style.color = '#ff4488';
        }
    };
}

window.watchTask = watchTask;

// ── Inject background task result into main chat stream ──────────────────────

async function injectTaskResultIntoChat(task) {
    // Only inject if the completed task belongs to the currently active session
    if (typeof S === 'undefined' || !S.session) return;
    if (task.session_id !== S.session.session_id) return;

    // Fetch the full result
    try {
        var res = await api('/api/task/result?task_id=' + encodeURIComponent(task.task_id));
        if (!res || !res.result) return;

        // Check if we already injected this task (avoid duplicates on re-poll)
        var alreadyInjected = S.messages.some(function(m) {
            return m._bg_task === task.task_id;
        });
        if (alreadyInjected) return;

        // Add user message (the prompt)
        S.messages.push({
            role: 'user',
            content: task.prompt || '',
            _ts: Date.now() / 1000,
            _bg_task: task.task_id,
            _bg_model: res.model || task.model || '',
            _bg_workspace: res.workspace || task.workspace || '',
            _bg_profile: res.profile || task.profile || '',
        });

        // Add assistant message (the result)
        S.messages.push({
            role: 'assistant',
            content: res.result,
            _ts: Date.now() / 1000 + 0.001,
            _bg_task: task.task_id,
            _bg_model: res.model || task.model || '',
            _bg_workspace: res.workspace || task.workspace || '',
            _bg_profile: res.profile || task.profile || '',
            _bg_duration: res.duration || '',
            _bg_status: res.status || task.status || '',
        });

        // Re-render the chat
        if (typeof renderMessages === 'function') {
            renderMessages();
        }

        // Scroll to bottom
        var chatEl = document.getElementById('chatMessages') || document.querySelector('.messages');
        if (chatEl) chatEl.scrollTop = chatEl.scrollHeight;

    } catch (e) {
        // Silent — result will still be in the task panel
    }
}

// Also inject when user loads a session that has completed bg tasks
// (the server-side _inject_into_session already saved them — this handles
// the case where the user was watching the session live when the task finished)
