const DEFAULT_MODEL='openrouter/minimax/minimax-m2.7';

// Global EventSource — only one stream active at a time.
// Closed automatically when send() is called again or loadSession() switches chats.
let _activeEs = null;

async function send(){
  const text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length)return;
  // Slash command intercept -- local commands handled without agent round-trip
  if(text.startsWith('/')&&!S.pendingFiles.length&&executeCommand(text)){
    $('msg').value='';autoResize();hideCmdDropdown();return;
  }
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area'))return;
  // If busy, queue the message instead of dropping it
  if(S.busy){
    if(text){
      MSG_QUEUE.push(text);
      $('msg').value='';autoResize();
      updateQueueBadge();
      showToast(`Queued: "${text.slice(0,40)}${text.length>40?'\u2026':''}"`,2000);
    }
    return;
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;

  setStatus(S.pendingFiles&&S.pendingFiles.length?'Uploading…':'Sending…');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles();}
  catch(e){if(!text){setStatus(`❌ ${e.message}`);return;}}

  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploaded.join(', ')}]`;
  if(!msgText){setStatus('Nothing to send');return;}

  $('msg').value='';autoResize();
  const displayText=text||(uploaded.length?`Uploaded: ${uploaded.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploaded:undefined,_ts:Date.now()/1000};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);  // activity bar shown via setBusy
  // P5: track the index where the pending assistant response will appear
  S._pendingAsstMsgIdx = S.messages.length;  // userMsg at length, asst will be at length+1 after send
  INFLIGHT[activeSid]={messages:[...S.messages],uploaded};
  startApprovalPolling(activeSid);
  S.activeStreamId = null;  // will be set after stream starts

  // Set provisional title from user message immediately so session appears
  // in the sidebar right away with a meaningful name (server may refine later)
  if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
    const provisionalTitle=displayText.slice(0,64);
    S.session.title=provisionalTitle;
    syncTopbar();
    // Persist it and refresh the sidebar now -- don't wait for done
    api('/api/session/rename',{method:'POST',body:JSON.stringify({
      session_id:activeSid, title:provisionalTitle
    })}).catch(()=>{});  // fire-and-forget, server refines on done
    renderSessionList();  // session appears in sidebar immediately
  } else {
    renderSessionList();  // ensure it's visible even if already titled
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  try{
    // Only send explicit model if user changed it from session default (auto router activates when absent)
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      model:S.session.model&&S.session.model!==DEFAULT_MODEL?S.session.model:undefined,
      workspace:S.session.workspace,
      attachments:uploaded.length?uploaded:undefined
    })});
    streamId=startData.stream_id;
    S.activeStreamId = streamId;
    markInflight(activeSid, streamId);
    // If auto-routed, update model chip to show selected tier
    const _mchip=$('modelChip');
    if(_mchip&&startData.auto_routed&&startData.model_tier){
      _mchip.textContent='Auto: '+startData.model_tier.charAt(0).toUpperCase()+startData.model_tier.slice(1);
    }
    // Show Cancel button
    const cancelBtn=$('btnCancel');
    if(cancelBtn) cancelBtn.style.display='';
  }catch(e){
    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();persistThinking();
    S.messages.push({role:'assistant',content:`**Error:** ${e.message}`});
    renderMessages();setBusy(false);setStatus('Error: '+e.message);
    return;
  }

  // Open SSE stream and render tokens live
  let assistantText='';
  let assistantRow=null;
  let assistantBody=null;
  // P1: RAF-throttled markdown buffering — only re-render every ~16ms instead of every token
  let _rafPending=false;
  let _doneProcessed=false;

  function ensureAssistantRow(){
    if(assistantRow)return;
    persistThinking();
    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');assistantRow.className='msg-row';
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    const role=document.createElement('div');role.className='msg-role assistant';
    const icon=document.createElement('div');icon.className='role-icon assistant';icon.textContent='H';
    const lbl=document.createElement('span');lbl.style.fontSize='12px';lbl.textContent='Hermes';
    role.appendChild(icon);role.appendChild(lbl);
    assistantRow.appendChild(role);assistantRow.appendChild(assistantBody);
    $('msgInner').appendChild(assistantRow);
  }

  // P1: RAF-buffered render — accumulates tokens, renders at most once per animation frame
  function _flushTokenBuffer(){
    _rafPending=false;
    // Don't render if done has already cleared the session or switched away
    if(!S.session||S.session.session_id!==activeSid||!assistantBody) return;
    if(assistantText){
      // Feature 3: Streaming markdown — handle partial code blocks.
      // Count unmatched ``` fences; if text ends with an open block, defer rendering it.
      let renderText = assistantText;
      const fenceMatches = assistantText.match(/```/g) || [];
      const isOdd = fenceMatches.length % 2 !== 0;
      if (isOdd) {
        // Find the last ``` and render everything before it;
        // append the partial block as raw text so cursor doesn't jump.
        const lastFence = assistantText.lastIndexOf('```');
        renderText = assistantText.slice(0, lastFence);
        const partial = assistantText.slice(lastFence);
        assistantBody.innerHTML = renderMd(renderText) +
          '<pre style="opacity:.5;margin-top:8px"><code>' + esc(partial) + '</code></pre>';
      } else {
        assistantBody.innerHTML = renderMd(assistantText);
      }
      // Apply Prism highlighting to any newly-completed code blocks
      requestAnimationFrame(() => {
        if (typeof Prism !== 'undefined' && Prism.highlightAllUnder && assistantBody) {
          assistantBody.querySelectorAll('pre > code:not([data-highlighted])').forEach(el => {
            Prism.highlightElement(el);
            el.setAttribute('data-highlighted', '1');
          });
        }
      });
      scrollIfPinned();
    }
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;

  function _wireSSE(source){
    source.addEventListener('token',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      ensureAssistantRow();
      // P1: throttle to RAF — no innerHTML update until next animation frame
      if(!_rafPending){
        _rafPending=true;
        requestAnimationFrame(_flushTokenBuffer);
      }
    });

    source.addEventListener('tool',e=>{
      const d=JSON.parse(e.data);
      if(S.session&&S.session.session_id===activeSid){
        setStatus(`${d.name}${d.preview?' · '+d.preview.slice(0,55):''}`);
      }
      if(!S.session||S.session.session_id!==activeSid) return;
      persistThinking();
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      const tc={name:d.name, preview:d.preview||'', args:d.args||{}, snippet:'', done:false};
      S.toolCalls.push(tc);
      // Feature 2: show activity timeline item for this tool call
      if(typeof appendActivityItem==='function') appendActivityItem(d.name, d.preview||'', d.tid||d.name);
      // Feature 6: update live todo panel if this is a todo tool call
      if(typeof updateLiveTodosFromToolCall==='function') updateLiveTodosFromToolCall(d.name, d.args||{});
      // If this tool event includes a snippet (result already here), resolve activity item
      if(d.snippet && typeof resolveActivityItem==='function') resolveActivityItem(d.name, d.tid||d.name);
      appendLiveToolCard(tc);
      scrollIfPinned();
    });

    source.addEventListener('thinking',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      appendThinkingLive(d.text||'');
      scrollIfPinned();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      d._session_id=activeSid;
      showApprovalCard(d);
    });

    // Feature 2: explicit 'tool_call' event (if backend sends it)
    source.addEventListener('tool_call',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      try {
        const d=JSON.parse(e.data);
        if(typeof appendActivityItem==='function') appendActivityItem(d.name||d.tool, d.preview||d.input||'', d.id||d.name||d.tool);
        if(typeof updateLiveTodosFromToolCall==='function') updateLiveTodosFromToolCall(d.name||d.tool, d.args||d.input||{});
      } catch(_) {}
    });

    // Feature 2: explicit 'tool_result' event (if backend sends it) — marks activity item done
    source.addEventListener('tool_result',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      try {
        const d=JSON.parse(e.data);
        if(typeof resolveActivityItem==='function') resolveActivityItem(d.name||d.tool, d.id||d.name||d.tool);
        // Feature 5: detect file writes in tool result and show file artifact card
        const content=d.output||d.content||d.result||'';
        const toolName=d.name||d.tool||'';
        if(typeof buildFileArtifactCard==='function') {
          const card=buildFileArtifactCard(toolName, typeof content==='string'?content:JSON.stringify(content), {
            filename:(d.args&&(d.args.path||d.args.filename||d.args.file))||'',
            args:d.args||{}
          });
          if(card) {
            const container=$('liveToolCards');
            if(container){container.style.display='';container.appendChild(card);}
          }
        }
      } catch(_) {}
    });

    // Feature 6: 'todo' SSE event — direct todo updates from backend
    source.addEventListener('todo',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      try {
        const d=JSON.parse(e.data);
        // d may be {items:[...]} for full list or {index, status, text} for update
        if(typeof updateLiveTodosFromToolCall==='function') updateLiveTodosFromToolCall('todo', d);
      } catch(_) {}
    });

        // ── Multica: status dot + context + cost ────────────────────────────
    source.addEventListener('agent_status', function(e) {
      try { var d = JSON.parse(e.data); updateAgentStatusDot(d.status, d.detail || ''); } catch(_) {}
    });
    source.addEventListener('context_info', function(e) {
      try {
        var d = JSON.parse(e.data);
        var row = document.querySelector('.msg-row.assistant:last-of-type');
        if (row) renderContextBadge(row, d.input_tokens, d.model_label || d.model);
      } catch(_) {}
    });
    source.addEventListener('cost', function(e) {
      try {
        var d = JSON.parse(e.data);
        var row = document.querySelector('.msg-row.assistant:last-of-type');
        if (row) renderCostBadge(row, d.input_fmt, d.output_fmt, d.cost_str);
      } catch(_) {}
    });

    source.addEventListener('done',e=>{
      source.close();
      _doneProcessed=true;  // P1: blocks stale RAF callbacks from firing after DOM rebuild
      const d=JSON.parse(e.data);
      delete INFLIGHT[activeSid];
      clearInflight();
      stopApprovalPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard();
      S.busy=false;  // R13: must precede renderMessages()
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
        const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
        S.session=d.session;S.messages=d.session.messages||[];
        const lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
        if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp) lastAsst._ts=Date.now()/1000;
        if(d.usage) S.lastUsage=d.usage;
        if(d.session.tool_calls&&d.session.tool_calls.length){
          S.toolCalls=d.session.tool_calls.map(tc=>({...tc,done:true}));
        } else {
          S.toolCalls=S.toolCalls.map(tc=>({...tc,done:true}));
        }
        if(uploaded.length){
          const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
          if(lastUser)lastUser.attachments=uploaded;
        }
        // P5: attach usage data to the assistant message that just completed
        const pendingIdx = S._pendingAsstMsgIdx;
        if(pendingIdx !== undefined && S.messages[pendingIdx] && S.messages[pendingIdx].role === 'assistant'){
          S.messages[pendingIdx]._usage = d.usage || null;
        }
        clearLiveToolCards();
        clearLiveThinkingBuffer();
        // Feature 2: clear activity timeline items
        if(typeof clearActivityItems==='function') clearActivityItems();
        // Feature 6: clear live todo panel
        if(typeof clearLiveTodos==='function') clearLiveTodos();
        // Restore model chip from auto-routing or session default
        const _chip=$('modelChip');
        if(_chip&&d.session&&d.session.model){
          const _tier=startData?.model_tier;
          if(_tier){
            _chip.textContent=_tier.charAt(0).toUpperCase()+_tier.slice(1);
          } else {
            const _sm=d.session.model.split('/').pop();
            _chip.textContent=_sm||'Model';
          }
        }
        syncTopbar();renderMessages();loadDir('.');  // P3: fire-and-forget — don't block render pipeline
        // B4: start workspace file watcher after session done
        if(window._startWorkspaceWatch && S.session) window._startWorkspaceWatch(S.session.workspace);
        // B5: auto-summary — if session is still Untitled and has 5+ messages, summarize it
        if(S.session && S.session.title === 'Untitled' && S.messages.length >= 5){
          const sid = S.session.session_id;
          api('/api/session/summarize', {method:'POST', body:JSON.stringify({session_id:sid})})
            .then(data => {
              if(data.ok && data.title && data.title !== 'Untitled'){
                S.session.title = data.title;
                // Update the session list item title in the sidebar without a full re-render
                const item = document.querySelector(`.session-item[data-sid="${sid}"] .session-title`);
                if(item) item.textContent = data.title;
                renderSessionList();  // refresh sidebar
              }
            }).catch(() => {});  // best-effort
        }
      }
      renderSessionList();setBusy(false);setStatus('');
    });

    source.addEventListener('thinking',e=>{
      if(!S.session||S.session.session_id!==activeSid) return;
      const d=JSON.parse(e.data);
      appendThinkingLive(d.text||'');
      scrollIfPinned();
    });

    source.addEventListener('apperror',e=>{
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      source.close();
      delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
        clearLiveToolCards();if(!assistantText)persistThinking();
        try{
          const d=JSON.parse(e.data);
          const isRateLimit=d.type==='rate_limit';
          const icon=isRateLimit?'⏱️':'⚠️';
          const label=isRateLimit?'Rate limit reached':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          // Feature 4: store structured error info for retryable error card
          const errMsg = `${label}: ${d.message}${hint}`;
          S.messages.push({role:'assistant',content:`**${icon} ${label}:** ${d.message}${hint}`, _errorCard:true, _errorMsg:errMsg});
        }catch(_){
          S.messages.push({role:'assistant',content:'**⚠️ Error:** An error occurred. Check server logs.'});
        }
        renderMessages();
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        try{const d=JSON.parse(e.data);trackBackgroundError(activeSid,_errTitle,d.message||'Error');}
        catch(_){trackBackgroundError(activeSid,_errTitle,'Error');}
      }
      setBusy(false);setStatus('');
    });

    source.addEventListener('warning',e=>{
      // Non-fatal warning from server (e.g. fallback activated, retrying)
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        // Show as a small inline notice, not a full error
        setStatus(`⚠️ ${d.message||'Warning'}`);
        // If it's a fallback notice, show it briefly then clear
        if(d.type==='fallback') setTimeout(()=>setStatus(''),4000);
      }catch(_){}
    });

    source.addEventListener('error',e=>{
      source.close();
      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setStatus('Connection lost \u2014 reconnecting\u2026');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setStatus('Reconnected');
              _activeEs = new EventSource(new URL(`/console/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{withCredentials:true});
              _wireSSE(_activeEs);
              return;
            }
          }catch(_){}
          _handleStreamError();
        },1500);
        return;
      }
      _handleStreamError();
    });

    source.addEventListener('cancel',e=>{
      source.close();
      delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
      if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;const _cbc=$('btnCancel');if(_cbc)_cbc.style.display='none';
      }
      if(S.session&&S.session.session_id===activeSid){
        clearLiveToolCards();if(!assistantText)persistThinking();
        // Feature 2: clear activity items on cancel
        if(typeof clearActivityItems==='function') clearActivityItems();
        // Feature 6: clear live todos on cancel
        if(typeof clearLiveTodos==='function') clearLiveTodos();
        S.messages.push({role:'assistant',content:'*Task cancelled.*'});renderMessages();
      }
      renderSessionList();
      setBusy(false);setStatus('');
    });
  }

  function _handleStreamError(){
    delete INFLIGHT[activeSid];clearInflight();stopApprovalPolling();
    if(!_approvalSessionId||_approvalSessionId===activeSid) hideApprovalCard();
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;const _cbe=$('btnCancel');if(_cbe)_cbe.style.display='none';
      clearLiveToolCards();if(!assistantText)persistThinking();
      S.messages.push({role:'assistant',content:'**Error:** Connection lost', _errorCard:true, _errorMsg:'Connection lost'});renderMessages();
    }else{
      // User switched away — show background error banner
      if(typeof trackBackgroundError==='function'){
        // Look up session title from the session list cache so the banner names it correctly
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection lost');
      }
    }
    // Always release busy state on stream error — prevents queue from getting stuck
    setBusy(false);setStatus('Error: Connection lost');
  }

  // Close any previous stream before starting a new one
  if(_activeEs){_activeEs.close();_activeEs=null;}
  _activeEs = new EventSource(new URL(`/console/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{withCredentials:true});
  _wireSSE(_activeEs);

}

function transcript(){
  const lines=[`# Hermes session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    lines.push(`## ${m.role}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

function autoResize(){const el=$('msg');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px';updateSendBtn();}


// ── Approval polling ──
let _approvalPollTimer = null;

// showApprovalCard moved above respondApproval

function hideApprovalCard() {
  $("approvalCard").classList.remove("visible");
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
let _approvalSessionId = null;

function showApprovalCard(pending) {
  $("approvalDesc").textContent = pending.description || "";
  $("approvalCmd").textContent = pending.command || "";
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  $("approvalDesc").textContent = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  _approvalSessionId = pending._session_id || (S.session && S.session.session_id) || null;
  $("approvalCard").classList.add("visible");
}

async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  hideApprovalCard();
  _approvalSessionId = null;
  try {
    await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice })
    });
  } catch(e) { setStatus("Approval error: " + e.message); }
}

function startApprovalPolling(sid) {
  stopApprovalPolling();
  _approvalPollTimer = setInterval(async () => {
    if (!S.busy || !S.session || S.session.session_id !== sid) {
      stopApprovalPolling(); hideApprovalCard(); return;
    }
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid));
      if (data.pending) { data.pending._session_id=sid; showApprovalCard(data.pending); }
      else { hideApprovalCard(); }
    } catch(e) { /* ignore poll errors */ }
  }, 4000);
}

function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
}
// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

