const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default'};
const INFLIGHT={};  // keyed by session_id while request in-flight
const MSG_QUEUE=[];  // messages queued while a request is in-flight
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Dynamic model labels -- populated by populateModelDropdown(), fallback to static map
let _dynamicModelLabels={};

// ── Smart model resolver ────────────────────────────────────────────────────
// Finds the best matching option value in a <select> for a given model ID.
// Handles mismatches like 'claude-sonnet-4-6' vs 'anthropic/claude-sonnet-4.6'.
// Returns the matched option's value (already in the list), or null if no match.
function _findModelInDropdown(modelId, sel){
  if(!modelId||!sel) return null;
  const opts=Array.from(sel.options).map(o=>o.value);
  // 1. Exact match
  if(opts.includes(modelId)) return modelId;
  // 2. Normalize: lowercase, strip namespace prefix, replace hyphens→dots
  const norm=s=>s.toLowerCase().replace(/^[^/]+\//,'').replace(/-/g,'.');
  const target=norm(modelId);
  const exact=opts.find(o=>norm(o)===target);
  if(exact) return exact;
  // 3. Prefix/substring: target starts with or contains a significant chunk
  const base=target.replace(/\.\d+$/,'');  // strip trailing version number
  const partial=opts.find(o=>norm(o).startsWith(base)||norm(o).includes(base));
  return partial||null;
}

// Set the model picker to the best match for modelId.
// Returns the resolved value that was actually set, or null if nothing matched.
function _applyModelToDropdown(modelId, sel){
  if(!modelId||!sel) return null;
  const resolved=_findModelInDropdown(modelId,sel);
  if(resolved){
    sel.value=resolved;
    return resolved;
  }
  return null;
}

async function populateModelDropdown(){
  const sel=$('modelSelect');
  if(!sel) return;
  try{
    const data=await fetch(new URL('/console/api/models',location.origin).href,{credentials:'include'}).then(r=>r.json());
    if(!data.groups||!data.groups.length) return; // keep HTML defaults
    // Clear existing options
    sel.innerHTML='';
    _dynamicModelLabels={};
    for(const g of data.groups){
      const og=document.createElement('optgroup');
      og.label=g.provider;
      for(const m of g.models){
        const opt=document.createElement('option');
        opt.value=m.id;
        opt.textContent=m.label;
        og.appendChild(opt);
        _dynamicModelLabels[m.id]=m.label;
      }
      sel.appendChild(og);
    }
    // Set default model from server if no localStorage preference
    if(data.default_model && !localStorage.getItem('hermes-webui-model')){
      _applyModelToDropdown(data.default_model, sel);
    }
  }catch(e){
    // API unavailable -- keep the hardcoded HTML options as fallback
    console.warn('Failed to load models from server:',e.message);
  }
}

// ── Scroll pinning ──────────────────────────────────────────────────────────
// When streaming, auto-scroll only if the user hasn't manually scrolled up.
// Once the user scrolls back to within 80px of the bottom, re-pin.
let _scrollPinned=true;
(function(){
  const el=document.getElementById('messages');
  if(!el) return;
  el.addEventListener('scroll',()=>{
    const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<80;
    _scrollPinned=nearBottom;
  });
})();
function _fmtTokens(n){if(!n||n<0)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return String(n);}

function scrollIfPinned(){
  if(!_scrollPinned) return;
  const el=$('messages');
  if(el) el.scrollTop=el.scrollHeight;
}
function scrollToBottom(){
  _scrollPinned=true;
  const el=$('messages');
  if(el) el.scrollTop=el.scrollHeight;
}

function getModelLabel(modelId){
  if(!modelId) return 'Unknown';
  // Check dynamic labels first, then fall back to splitting the ID
  if(_dynamicModelLabels[modelId]) return _dynamicModelLabels[modelId];
  // Static fallback for common models
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-2.5-pro':'Gemini 2.5 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3','meta-llama/llama-4-scout':'Llama 4 Scout'};
  if(STATIC_LABELS[modelId]) return STATIC_LABELS[modelId];
  return modelId.split('/').pop()||'Unknown';
}

function renderMd(raw){
  let s=raw||'';
  // Pre-pass: convert safe inline HTML tags the model may emit into their
  // markdown equivalents so the pipeline can render them correctly.
  // Only runs OUTSIDE fenced code blocks and backtick spans (stash + restore).
  // Unsafe tags (anything not in the allowlist) are left as-is and will be
  // HTML-escaped by esc() when they reach an innerHTML assignment -- no XSS risk.
  const fence_stash=[];
  s=s.replace(/(```[\s\S]*?```|`[^`\n]+`)/g,m=>{fence_stash.push(m);return '\x00F'+(fence_stash.length-1)+'\x00';});
  // Safe tag → markdown equivalent (these produce the same output as **text** etc.)
  s=s.replace(/<strong>([\s\S]*?)<\/strong>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<b>([\s\S]*?)<\/b>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<em>([\s\S]*?)<\/em>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<i>([\s\S]*?)<\/i>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<code>([^<]*?)<\/code>/gi,(_,t)=>'`'+t+'`');
  s=s.replace(/<br\s*\/?>/gi,'\n');
  // Restore stashed code blocks
  s=s.replace(/\x00F(\d+)\x00/g,(_,i)=>fence_stash[+i]);
  // Mermaid blocks: render as diagram containers (processed after DOM insertion)
  s=s.replace(/```mermaid\n?([\s\S]*?)```/g,(_,code)=>{
    const id='mermaid-'+Math.random().toString(36).slice(2,10);
    return `<div class="mermaid-block" data-mermaid-id="${id}">${esc(code.trim())}</div>`;
  });
  s=s.replace(/```([\w+-]*)\n?([\s\S]*?)```/g,(_,lang,code)=>{const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';return `${h}<pre><code>${esc(code.replace(/\n$/,''))}</code></pre>`;});
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>`<code>${esc(c)}</code>`);
  // inlineMd: process bold/italic/code/links within a single line of text.
  // Used inside list items and blockquotes where the text may already contain
  // HTML from the pre-pass → bold pipeline, so we cannot call esc() directly.
  function inlineMd(t){
    t=t.replace(/\*\*\*(.+?)\*\*\*/g,(_,x)=>`<strong><em>${esc(x)}</em></strong>`);
    t=t.replace(/\*\*(.+?)\*\*/g,(_,x)=>`<strong>${esc(x)}</strong>`);
    t=t.replace(/\*([^*\n]+)\*/g,(_,x)=>`<em>${esc(x)}</em>`);
    t=t.replace(/`([^`\n]+)`/g,(_,x)=>`<code>${esc(x)}</code>`);
    t=t.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,lb,u)=>`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(lb)}</a>`);
    // Escape any plain text that isn't already wrapped in a tag we produced
    // by escaping bare < > that aren't part of our own tags
    const SAFE_INLINE=/^<\/?(strong|em|code|a)([\s>]|$)/i;
    t=t.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_INLINE.test(tag)?tag:esc(tag));
    return t;
  }
  s=s.replace(/\*\*\*(.+?)\*\*\*/g,(_,t)=>`<strong><em>${esc(t)}</em></strong>`);
  s=s.replace(/\*\*(.+?)\*\*/g,(_,t)=>`<strong>${esc(t)}</strong>`);
  s=s.replace(/\*([^*\n]+)\*/g,(_,t)=>`<em>${esc(t)}</em>`);
  s=s.replace(/^### (.+)$/gm,(_,t)=>`<h3>${inlineMd(t)}</h3>`).replace(/^## (.+)$/gm,(_,t)=>`<h2>${inlineMd(t)}</h2>`).replace(/^# (.+)$/gm,(_,t)=>`<h1>${inlineMd(t)}</h1>`);
  s=s.replace(/^---+$/gm,'<hr>');
  s=s.replace(/^> (.+)$/gm,(_,t)=>`<blockquote>${inlineMd(t)}</blockquote>`);
  // B8: improved list handling supporting up to 2 levels of indentation
  s=s.replace(/((?:^(?:  )?[-*+] .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ul>';
    for(const l of lines){
      const indent=/^ {2,}/.test(l);
      const text=l.replace(/^ {0,4}[-*+] /,'');
      if(indent) html+=`<li style="margin-left:16px">${inlineMd(text)}</li>`;
      else html+=`<li>${inlineMd(text)}</li>`;
    }
    return html+'</ul>';
  });
  s=s.replace(/((?:^(?:  )?\d+\. .+\n?)+)/gm,block=>{
    const lines=block.trimEnd().split('\n');
    let html='<ol>';
    for(const l of lines){
      const text=l.replace(/^ {0,4}\d+\. /,'');
      html+=`<li>${inlineMd(text)}</li>`;
    }
    return html+'</ol>';
  });
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,(_,label,url)=>`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>`);
  // Tables: | col | col | header row followed by | --- | --- | separator then data rows
  s=s.replace(/((?:^\|.+\|\n?)+)/gm,block=>{
    const rows=block.trim().split('\n').filter(r=>r.trim());
    if(rows.length<2)return block;
    const isSep=r=>/^\|[\s|:-]+\|$/.test(r.trim());
    if(!isSep(rows[1]))return block;
    const parseRow=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${esc(c.trim())}</td>`).join('');
    const parseHeader=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${esc(c.trim())}</th>`).join('');
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    return `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
  });
  // Escape any remaining HTML tags that are NOT from our own markdown output.
  // Our pipeline only emits: <strong>,<em>,<code>,<pre>,<h1-6>,<ul>,<ol>,<li>,
  // <table>,<thead>,<tbody>,<tr>,<th>,<td>,<hr>,<blockquote>,<p>,<br>,<a>,
  // <div class="..."> (mermaid/pre-header). Everything else is untrusted input.
  const SAFE_TAGS=/^<\/?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|hr|blockquote|p|br|a|div)([\s>]|$)/i;
  s=s.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_TAGS.test(tag)?tag:esc(tag));
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|pre|hr|blockquote)/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  return s;
}

function setStatus(t){
  const bar=$('activityBar');
  const txt=$('activityText');
  const dismiss=$('btnDismissStatus');
  if(!bar||!txt)return;
  if(!t){
    bar.style.display='none';
    txt.textContent='';
    if(dismiss)dismiss.style.display='none';
  } else {
    txt.textContent=t;
    bar.style.display='';
    // Show dismiss X only for static/error messages, not transient busy ones
    const transient = t.endsWith('…') || t === 'Hermes is thinking…';
    if(dismiss)dismiss.style.display=(!transient && !S.busy)?'inline':'none';
  }
}
function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn) return;
  const hasContent=$('msg').value.trim().length>0||S.pendingFiles.length>0;
  const shouldShow=hasContent&&!S.busy;
  if(shouldShow&&btn.style.display==='none'){
    btn.style.display='';
    // Remove then re-add class to retrigger animation each time
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  } else if(!shouldShow&&btn.style.display!=='none'){
    btn.style.display='none';
    btn.classList.remove('visible');
  }
}
function setBusy(v){
  S.busy=v;
  $('btnSend').disabled=v;
  updateSendBtn();
  const dots=$('activityDots');
  if(dots) dots.style.display=v?'flex':'none';
  if(!v){
    setStatus('');
    // Always hide Cancel button when not busy
    const _cb=$('btnCancel');if(_cb)_cb.style.display='none';
    updateQueueBadge();
    // Drain one queued message after UI settles
    if(MSG_QUEUE.length>0){
      const next=MSG_QUEUE.shift();
      updateQueueBadge();
      setTimeout(()=>{ $('msg').value=next; send(); }, 120);
    }
  }
}

function updateQueueBadge(){
  let badge=$('queueBadge');
  if(MSG_QUEUE.length>0){
    if(!badge){
      badge=document.createElement('div');
      badge.id='queueBadge';
      badge.style.cssText='position:fixed;bottom:80px;right:24px;background:rgba(124,185,255,.18);border:1px solid rgba(124,185,255,.4);color:var(--blue);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px;z-index:50;pointer-events:none;backdrop-filter:blur(8px);';
      document.body.appendChild(badge);
    }
    badge.textContent=MSG_QUEUE.length===1?'1 message queued':`${MSG_QUEUE.length} messages queued`;
  } else {
    if(badge) badge.remove();
  }
}
function showToast(msg,ms){const el=$('toast');el.textContent=msg;el.classList.add('show');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),ms||2800);}

function copyMsg(btn){
  const row=btn.closest('.msg-row');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  navigator.clipboard.writeText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML='&#10003;';btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast('Copy failed'));
}

// ── Reconnect banner (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking

function markInflight(sid, streamId) {
  localStorage.setItem(INFLIGHT_KEY, JSON.stringify({sid, streamId, ts: Date.now()}));
}
function clearInflight() {
  localStorage.removeItem(INFLIGHT_KEY);
}
function showReconnectBanner(msg) {
  $('reconnectMsg').textContent = msg || 'A response may have been in progress when you last left.';
  $('reconnectBanner').classList.add('visible');
}
function dismissReconnect() {
  $('reconnectBanner').classList.remove('visible');
  clearInflight();
}
async function refreshSession() {
  dismissReconnect();
  if (!S.session) return;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
    S.session = data.session;
    S.messages = (data.session.messages || []).filter(m => {
      if (!m || !m.role || m.role === 'tool') return false;
      if (m.role === 'assistant') { let c = m.content || ''; if (Array.isArray(c)) c = c.map(p => p.text||'').join(''); return String(c).trim().length > 0; }
      return true;
    });
    syncTopbar(); renderMessages();
    showToast('Conversation refreshed');
  } catch(e) { setStatus('Refresh failed: ' + e.message); }
}
async function checkInflightOnBoot(sid) {
  const raw = localStorage.getItem(INFLIGHT_KEY);
  if (!raw) return;
  try {
    const {sid: inflightSid, streamId, ts} = JSON.parse(raw);
    if (inflightSid !== sid) { clearInflight(); return; }
    // Only show banner if the in-flight entry is less than 10 minutes old
    if (Date.now() - ts > 10 * 60 * 1000) { clearInflight(); return; }
    // Check if stream is still active
    const status = await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId || '')}`);
    if (status.active) {
      // Stream is genuinely still running -- show the banner
      showReconnectBanner('A response is still being generated. Reload when ready?');
    } else {
      // Stream finished. Only show banner if reload happened within 90 seconds
      // (longer gap = normal completed session, not a mid-stream reload)
      if (Date.now() - ts < 90 * 1000) {
        showReconnectBanner('A response was in progress when you last left. Messages may have updated.');
      } else {
        clearInflight();  // completed normally, no banner needed
      }
    }
  } catch(e) { clearInflight(); }
}

function syncTopbar(){
  if(!S.session){
    document.title='Hermes';
    // Show default workspace name even without a session
    const sidebarName=$('sidebarWsName');
    if(sidebarName && sidebarName.textContent==='Workspace'){
      sidebarName.textContent='No workspace';
    }
    return;
  }
  const sessionTitle=S.session.title||'Untitled';
  $('topbarTitle').textContent=sessionTitle;
  document.title=sessionTitle+' \u2014 Hermes';
  const vis=S.messages.filter(m=>m&&m.role&&m.role!=='tool');
  $('topbarMeta').textContent=`${vis.length} messages`;
  // If a profile switch just happened, apply its model rather than the session's stale value.
  // S._pendingProfileModel is set by switchToProfile() and cleared here after one application.
  const modelOverride=S._pendingProfileModel;
  if(modelOverride){
    S._pendingProfileModel=null;
    _applyModelToDropdown(modelOverride,$('modelSelect'));
  } else {
    const m=S.session.model||'';
    const applied=_applyModelToDropdown(m,$('modelSelect'));
    // If the model isn't in the list at all, add it so the session value is preserved
    if(!applied && m){
      const opt=document.createElement('option');
      opt.value=m;
      opt.textContent=getModelLabel(m);
      $('modelSelect').appendChild(opt);
      $('modelSelect').value=m;
    }
  }
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  const displayModel=$('modelSelect').value||m;
  $('modelChip').textContent=getModelLabel(displayModel);
  const ws=S.session.workspace||'';
  // Update sidebar workspace display
  const sidebarName=$('sidebarWsName');
  const sidebarPath=$('sidebarWsPath');
  if(sidebarName){
    sidebarName.textContent=getWorkspaceFriendlyName(ws);
  }
  if(sidebarPath){
    sidebarPath.textContent=ws;
  }
  // modelSelect already set above
  // Update profile chip label
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
}

function msgContent(m){
  // Extract plain text content from a message for filtering
  let c=m.content||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
  return String(c).trim();
}

function renderMessages(){
  const inner=$('msgInner');
  // P7: During live SSE streaming, skip full DOM rebuild — the SSE handler
  // (messages.js) updates the assistant row directly per token.
  // Only the 'done' event (post-stream) should trigger a full rebuild.
  if(S.busy){
    // P7: Ensure the streaming assistant row exists (user may have sent message while idle)
    // but don't rebuild the whole message list.
    return;
  }
  const vis=S.messages.filter(m=>{
    if(!m||!m.role||m.role==='tool')return false;
    return msgContent(m)||m.attachments?.length;
  });
  $('emptyState').style.display=vis.length?'none':'';
  inner.innerHTML='';
  // Track original indices (in S.messages) so truncate knows the cut point
  const visWithIdx=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    if(msgContent(m)||m.attachments?.length) visWithIdx.push({m,rawIdx});
    rawIdx++;
  }
  for(let vi=0;vi<visWithIdx.length;vi++){
    const {m,rawIdx}=visWithIdx[vi];
    let content=m.content||'';
    // Extract thinking/reasoning blocks from structured content (Claude extended thinking, o3)
    let thinkingText='';
    if(Array.isArray(content)){
      thinkingText=content.filter(p=>p&&(p.type==='thinking'||p.type==='reasoning')).map(p=>p.thinking||p.reasoning||p.text||'').join('\n');
      content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
    }
    const isUser=m.role==='user';
    const isLastAssistant=!isUser&&vi===visWithIdx.length-1;
    // Render thinking card before the assistant message (collapsed by default)
    if(thinkingText&&!isUser){
      const thinkRow=document.createElement('div');thinkRow.className='msg-row thinking-card-row';
      thinkRow.innerHTML=`<div class="thinking-card"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">&#128161;</span><span class="thinking-card-label">Thinking</span><span class="thinking-card-toggle">&#9656;</span></div><div class="thinking-card-body"><pre>${esc(thinkingText)}</pre></div></div>`;
      inner.appendChild(thinkRow);
    }
    const row=document.createElement('div');row.className='msg-row';
    row.dataset.msgIdx=rawIdx;row.dataset.role=m.role||'assistant';
    let filesHtml='';
    if(m.attachments&&m.attachments.length)
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>`<div class="msg-file-badge">&#128206; ${esc(f)}</div>`).join('')}</div>`;
    const bodyHtml = isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(String(content));
    // Action buttons for this bubble
    const editBtn  = isUser  ? `<button class="msg-action-btn" title="Edit message" onclick="editMessage(this)">&#9998;</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="Regenerate response" onclick="regenerateResponse(this)">&#8635;</button>` : '';
    const tsVal=m._ts||m.timestamp;
    const tsTitle=tsVal?new Date(tsVal*1000).toLocaleString():'';
    row.innerHTML=`<div class="msg-role ${m.role}" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon ${m.role}">${isUser?'Y':'H'}</div><span style="font-size:12px">${isUser?'You':'Hermes'}</span>${tsTitle?`<span class="msg-time">${new Date(tsVal*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>`:''}<span class="msg-actions">${editBtn}<button class="msg-copy-btn msg-action-btn" title="Copy" onclick="copyMsg(this)">&#128203;</button>${retryBtn}</span></div>${filesHtml}<div class="msg-body">${bodyHtml}</div>`;
    row.dataset.rawText = String(content).trim();
    inner.appendChild(row);
    // Feature 4: if this is a flagged error message, append an error card after the row
    if(!isUser && m._errorCard && typeof buildErrorCard === 'function') {
      const lastUserMsg = [...S.messages].slice(0, rawIdx).reverse().find(msg => msg && msg.role === 'user');
      const lastUserText = lastUserMsg ? msgContent(lastUserMsg) : '';
      const errCard = buildErrorCard(m._errorMsg || msgContent(m), lastUserText);
      inner.appendChild(errCard);
    }
  }
  // Insert settled tool call cards (history view only).
  // During live streaming, tool cards are rendered in #liveToolCards by the
  // tool SSE handler and never mixed into the message list until done fires.
  if(!S.busy && S.toolCalls && S.toolCalls.length){
    inner.querySelectorAll('.tool-card-row').forEach(el=>el.remove());
    const byAssistant = {};
    for(const tc of S.toolCalls){
      const key = tc.assistant_msg_idx !== undefined ? tc.assistant_msg_idx : -1;
      if(!byAssistant[key]) byAssistant[key] = [];
      byAssistant[key].push(tc);
    }
    const allRows = Array.from(inner.querySelectorAll('.msg-row[data-msg-idx]'));
    for(const [key, cards] of Object.entries(byAssistant)){
      const aIdx = parseInt(key);
      let insertBefore = null;
      if(aIdx === -1){
        for(let i=allRows.length-1;i>=0;i--){
          const ri=parseInt(allRows[i].dataset.msgIdx||'-1',10);
          if(ri>=0&&S.messages[ri]&&S.messages[ri].role==='assistant'){insertBefore=allRows[i];break;}
        }
      } else {
        for(const r of allRows){
          const ri=parseInt(r.dataset.msgIdx||'-1');
          if(ri>aIdx&&S.messages[ri]&&S.messages[ri].role==='assistant'){insertBefore=r;break;}
        }
      }
      const frag=document.createDocumentFragment();
      for(const tc of cards){frag.appendChild(buildToolCard(tc));}
      // Add expand/collapse toggle for groups with 2+ cards
      if(cards.length>=2){
        const toggle=document.createElement('div');
        toggle.className='tool-cards-toggle';
        // Collect card elements before they get moved to DOM
        const cardEls=Array.from(frag.querySelectorAll('.tool-card'));
        const expandBtn=document.createElement('button');
        expandBtn.textContent='Expand all';
        expandBtn.onclick=()=>cardEls.forEach(c=>c.classList.add('open'));
        const collapseBtn=document.createElement('button');
        collapseBtn.textContent='Collapse all';
        collapseBtn.onclick=()=>cardEls.forEach(c=>c.classList.remove('open'));
        toggle.appendChild(expandBtn);
        toggle.appendChild(collapseBtn);
        frag.insertBefore(toggle,frag.firstChild);
      }
      if(insertBefore) inner.insertBefore(frag,insertBefore);
      else inner.appendChild(frag);
    }
  }
  // P5: Render usage badge on any assistant message with _usage data (not just the last one)
  if(window._showTokenUsage){
    const rows=inner.querySelectorAll('.msg-row[data-msg-idx]');
    for(const row of rows){
      const idx=parseInt(row.dataset.msgIdx||'-1',10);
      const msg=S.messages[idx];
      if(!msg||msg.role!=='assistant'||!msg._usage) continue;
      if(row.querySelector('.msg-usage')) continue;
      const u=msg._usage;
      if(!u||(!u.input_tokens&&!u.output_tokens)) continue;
      const usage=document.createElement('div');
      usage.className='msg-usage';
      const inTok=u.input_tokens||0;
      const outTok=u.output_tokens||0;
      const cost=u.estimated_cost;
      let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
      if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
      usage.textContent=text;
      row.appendChild(usage);
    }
  }
  scrollToBottom();
  // P2: Only highlight code blocks that haven't been highlighted yet (avoids full-tree re-render)
  // Mark highlighted code elements with data-highlighted to skip on subsequent calls
  requestAnimationFrame(()=>{
    if(typeof Prism !== 'undefined' && Prism.highlightAllUnder){
      const el=$('msgInner');
      if(el) el.querySelectorAll('pre > code:not([data-highlighted])').forEach(codeEl=>{
        Prism.highlightElement(codeEl);
        codeEl.setAttribute('data-highlighted','1');
      });
    }
    addCopyButtons();
    renderMermaidBlocks();
  });
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
}

function toolIcon(name){
  const icons={terminal:'⬛',read_file:'📄',write_file:'✏️',search_files:'🔍',
    web_search:'🌐',web_extract:'🌐',execute_code:'⚙️',patch:'🔧',
    memory:'🧠',skill_manage:'📚',todo:'✅',cronjob:'⏱️',delegate_task:'🤖',
    send_message:'💬',browser_navigate:'🌐',vision_analyze:'👁️',
    subagent_progress:'🔀'};
  return icons[name]||'🔧';
}

function buildToolCard(tc){
  const row=document.createElement('div');
  row.className='msg-row tool-card-row';
  const icon=toolIcon(tc.name);
  const hasDetail=tc.snippet||(tc.args&&Object.keys(tc.args).length>0);
  let displaySnippet='';
  if(tc.snippet){
    const s=tc.snippet;
    if(s.length<=220){displaySnippet=s;}
    else{
      const cutoff=s.slice(0,220);
      const lastBreak=Math.max(cutoff.lastIndexOf('. '),cutoff.lastIndexOf('\n'),cutoff.lastIndexOf('; '));
      displaySnippet=lastBreak>80?s.slice(0,lastBreak+1):cutoff;
    }
  }
  const hasMore=tc.snippet&&tc.snippet.length>displaySnippet.length;
  const runIndicator=tc.done===false?'<span class="tool-card-running-dot"></span>':'';
  const isSubagent=tc.name==='subagent_progress';
  const isDelegation=tc.name==='delegate_task';
  const cardClass='tool-card'+(tc.done===false?' tool-card-running':'')+(isSubagent?' tool-card-subagent':'');
  // Clean up subagent preview: strip leading 🔀 emoji since the icon already shows it
  let displayName=tc.name;
  if(isSubagent) displayName='Subagent';
  if(isDelegation) displayName='Delegate task';
  let previewText=tc.preview||displaySnippet||'';
  if(isSubagent) previewText=previewText.replace(/^🔀\s*/,'');
  row.innerHTML=`
    <div class="${cardClass}">
      <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
        ${runIndicator}
        <span class="tool-card-icon">${icon}</span>
        <span class="tool-card-name">${esc(displayName)}</span>
        <span class="tool-card-preview">${esc(previewText)}</span>
        ${hasDetail?'<span class="tool-card-toggle">▸</span>':''}
      </div>
      ${hasDetail?`<div class="tool-card-detail">
        ${tc.args&&Object.keys(tc.args).length?`<div class="tool-card-args">${
          Object.entries(tc.args).map(([k,v])=>`<div><span class="tool-arg-key">${esc(k)}</span> <span class="tool-arg-val">${esc(String(v))}</span></div>`).join('')
        }</div>`:''}
        ${displaySnippet?`<div class="tool-card-result">
          <pre>${esc(displaySnippet)}</pre>
          ${hasMore?`<button class="tool-card-more" data-full="${esc(tc.snippet||'').replace(/"/g,'&quot;')}" data-short="${esc(displaySnippet||'').replace(/"/g,'&quot;')}" onclick="event.stopPropagation();const p=this.previousElementSibling;const full=this.dataset.full;const short=this.dataset.short;p.textContent=p.textContent===short?full:short;this.textContent=p.textContent===short?'Show more':'Show less'">Show more</button>`:''}
        </div>`:''}
        ${(function(){
          // Feature 5: check if this is a file write and render a file artifact card inline
          if(tc.done!==false && typeof buildFileArtifactCard==='function'){
            const fileCard=buildFileArtifactCard(tc.name, tc.snippet||'', {filename:(tc.args&&(tc.args.path||tc.args.filename||tc.args.file))||'', args:tc.args||{}});
            if(fileCard) return fileCard.outerHTML;
          }
          return '';
        })()}
      </div>`:''}
    </div>`;
  return row;
}

// ── Live tool card helpers (called during SSE streaming) ──
function appendLiveToolCard(tc){
  const container=$('liveToolCards');
  if(!container)return;
  container.style.display='';
  // Update existing card if same tool call id (e.g. snippet arrives after done)
  const existing=container.querySelector(`[data-tid="${CSS.escape(tc.tid||'')}"]`);
  if(existing){existing.replaceWith(buildToolCard(tc));return;}
  const card=buildToolCard(tc);
  if(tc.tid)card.dataset.tid=tc.tid;
  container.appendChild(card);
}

function clearLiveToolCards(){
  const container=$('liveToolCards');
  if(!container)return;
  container.innerHTML='';
  container.style.display='none';
}

// ── Edit + Regenerate ──

function editMessage(btn) {
  if(S.busy) return;
  const row = btn.closest('.msg-row');
  if(!row) return;
  const msgIdx = parseInt(row.dataset.msgIdx, 10);
  const originalText = row.dataset.rawText || '';
  const body = row.querySelector('.msg-body');
  if(!body || row.dataset.editing) return;
  row.dataset.editing = '1';

  // Replace msg-body with an editable textarea
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-area';
  ta.value = originalText;
  body.replaceWith(ta);
  // Resize after DOM insertion so scrollHeight is correct
  requestAnimationFrame(() => { autoResizeTextarea(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
  ta.addEventListener('input', () => autoResizeTextarea(ta));

  // Action bar below the textarea
  const bar = document.createElement('div');
  bar.className = 'msg-edit-bar';
  bar.innerHTML = `<button class="msg-edit-send">Send edit</button><button class="msg-edit-cancel">Cancel</button>`;
  ta.after(bar);

  bar.querySelector('.msg-edit-send').onclick = async () => {
    const newText = ta.value.trim();
    if(!newText) return;
    await submitEdit(msgIdx, newText);
  };
  bar.querySelector('.msg-edit-cancel').onclick = () => cancelEdit(row, originalText, body);

  ta.addEventListener('keydown', e => {
    if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
    if(e.key==='Escape') { e.preventDefault(); cancelEdit(row, originalText, body); }
  });
}

function cancelEdit(row, originalText, originalBody) {
  delete row.dataset.editing;
  const ta = row.querySelector('.msg-edit-area');
  const bar = row.querySelector('.msg-edit-bar');
  if(ta) ta.replaceWith(originalBody);
  if(bar) bar.remove();
}

function autoResizeTextarea(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 300) + 'px';
}

async function submitEdit(msgIdx, newText) {
  if(!S.session || S.busy) return;
  // Truncate session at msgIdx (keep messages before the edited one)
  // then re-send the edited text
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: S.session.session_id,
      keep_count: msgIdx  // keep messages[0..msgIdx-1], discard from msgIdx onward
    })});
    S.messages = S.messages.slice(0, msgIdx);
    renderMessages();
    // Now send the edited message as a new chat
    $('msg').value = newText;
    await send();
  } catch(e) { setStatus('Edit failed: ' + e.message); }
}

async function regenerateResponse(btn) {
  if(!S.session || S.busy) return;
  // Find the last user message and re-run it
  // Remove the last assistant message first (truncate to before it)
  const row = btn.closest('.msg-row');
  if(!row) return;
  const assistantIdx = parseInt(row.dataset.msgIdx, 10);
  // Find the last user message text (one before this assistant message)
  let lastUserText = '';
  for(let i = assistantIdx - 1; i >= 0; i--) {
    const m = S.messages[i];
    if(m && m.role === 'user') { lastUserText = msgContent(m); break; }
  }
  if(!lastUserText) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: S.session.session_id,
      keep_count: assistantIdx  // remove the assistant message
    })});
    S.messages = S.messages.slice(0, assistantIdx);
    renderMessages();
    $('msg').value = lastUserText;
    await send();
  } catch(e) { setStatus('Regenerate failed: ' + e.message); }
}

function highlightCode(container) {
  // P2: Only highlight code blocks not yet highlighted — avoids O(n) re-highlight on every render
  if(typeof Prism === 'undefined' || !Prism.highlightAllUnder) return;
  const el = container || $('msgInner');
  if(!el) return;
  el.querySelectorAll('pre > code:not([data-highlighted])').forEach(codeEl=>{
    Prism.highlightElement(codeEl);
    codeEl.setAttribute('data-highlighted','1');
  });
}

function addCopyButtons(container){
  const el=container||$('msgInner');
  if(!el) return;
  el.querySelectorAll('pre > code').forEach(codeEl=>{
    const pre=codeEl.parentElement;
    if(pre.querySelector('.code-copy-btn')) return;
    const btn=document.createElement('button');
    btn.className='code-copy-btn';
    btn.textContent='Copy';
    btn.onclick=(e)=>{
      e.stopPropagation();
      navigator.clipboard.writeText(codeEl.textContent).then(()=>{
        btn.textContent='Copied!';
        setTimeout(()=>{btn.textContent='Copy';},1500);
      });
    };
    const header=pre.previousElementSibling;
    if(header&&header.classList.contains('pre-header')){
      header.style.display='flex';
      header.style.justifyContent='space-between';
      header.style.alignItems='center';
      header.appendChild(btn);
    }else{
      pre.style.position='relative';
      btn.style.cssText='position:absolute;top:6px;right:6px;';
      pre.appendChild(btn);
    }
  });
}

let _mermaidLoading=false;
let _mermaidReady=false;

function renderMermaidBlocks(){
  const blocks=document.querySelectorAll('.mermaid-block:not([data-rendered])');
  if(!blocks.length) return;
  if(!_mermaidReady){
    if(!_mermaidLoading){
      _mermaidLoading=true;
      const script=document.createElement('script');
      script.src='https://cdn.jsdelivr.net/npm/mermaid@10.9.3/dist/mermaid.min.js';
      script.integrity='sha384-R63zfMfSwJF4xCR11wXii+QUsbiBIdiDzDbtxia72oGWfkT7WHJfmD/I/eeHPJyT';
      script.crossOrigin='anonymous';
      script.onload=()=>{
        if(typeof mermaid!=='undefined'){
          mermaid.initialize({startOnLoad:false,theme:'dark',themeVariables:{
            primaryColor:'#4a6fa5',primaryTextColor:'#e2e8f0',lineColor:'#718096',
            secondaryColor:'#2d3748',tertiaryColor:'#1a202c',primaryBorderColor:'#4a5568',
          }});
          _mermaidReady=true;
          renderMermaidBlocks();
        }
      };
      document.head.appendChild(script);
    }
    return;
  }
  blocks.forEach(async(block)=>{
    block.dataset.rendered='true';
    const code=block.textContent;
    const id=block.dataset.mermaidId||('m-'+Math.random().toString(36).slice(2));
    try{
      const {svg}=await mermaid.render(id,code);
      block.innerHTML=svg;
      block.classList.add('mermaid-rendered');
    }catch(e){
      // Fall back to showing as a code block
      block.innerHTML=`<div class="pre-header">mermaid</div><pre><code>${esc(code)}</code></pre>`;
    }
  });
}

function appendThinking(){
  $('emptyState').style.display='none';
  const row=document.createElement('div');row.className='msg-row';row.id='thinkingRow';
  row.innerHTML=`<div class="msg-role assistant"><div class="role-icon assistant">H</div>Hermes</div><div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
  $('msgInner').appendChild(row);scrollToBottom();
}
function removeThinking(){const el=$('thinkingRow');if(el)el.remove();}

// Live thinking card: shows thinking text in real-time during SSE stream
let _liveThinkingBuffer='';
function appendThinkingLive(text){
  $('emptyState').style.display='none';
  // Find or create the thinking row
  let row=$('thinkingRow');
  if(!row){
    row=document.createElement('div');row.className='msg-row';row.id='thinkingRow';
    row.innerHTML=`<div class="msg-role assistant"><div class="role-icon assistant">H</div>Hermes</div><div class="thinking" id="liveThinkingContent"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
    $('msgInner').appendChild(row);
  }
  // Accumulate thinking text
  _liveThinkingBuffer+=text;
  const content=$('liveThinkingContent');
  if(content){
    // Show thinking text inline with animated dots, or just dots if no text yet
    content.innerHTML=_liveThinkingBuffer
      ? `<div style="font-size:11px;color:var(--muted);line-height:1.5;max-height:120px;overflow-y:auto;margin-bottom:4px">${esc(_liveThinkingBuffer.slice(-400))}</div><div class="dot"></div><div class="dot"></div><div class="dot"></div>`
      : `<div class="dot"></div><div class="dot"></div><div class="dot"></div>`;
  }
  scrollToBottom();
}
function clearLiveThinkingBuffer(){_liveThinkingBuffer='';}

function fileIcon(name, type){
  if(type==='dir') return '📁';
  const e=fileExt(name);
  if(IMAGE_EXTS.has(e)) return '📷';
  if(MD_EXTS.has(e))    return '📝';
  if(typeof DOWNLOAD_EXTS!=='undefined'&&DOWNLOAD_EXTS.has(e)) return '⬇️';
  if(e==='.py')   return '🐍';
  if(e==='.js'||e==='.ts'||e==='.jsx'||e==='.tsx') return '⚡';
  if(e==='.json'||e==='.yaml'||e==='.yml'||e==='.toml') return '⚙';
  if(e==='.sh'||e==='.bash') return '💻';
  if(e==='.pdf') return '⬇️';
  return '📄';
}

function renderBreadcrumb(){
  const bar=$('breadcrumbBar');
  const upBtn=$('btnUpDir');
  if(!bar)return;
  if(S.currentDir==='.'){
    bar.style.display='none';
    if(upBtn)upBtn.style.display='none';
    return;
  }
  bar.style.display='flex';
  if(upBtn)upBtn.style.display='';
  bar.innerHTML='';
  // Root segment
  const root=document.createElement('span');
  root.className='breadcrumb-seg breadcrumb-link';
  root.textContent='~';
  root.onclick=()=>loadDir('.');
  bar.appendChild(root);
  // Path segments
  const parts=S.currentDir.split('/');
  let accumulated='';
  for(let i=0;i<parts.length;i++){
    const sep=document.createElement('span');
    sep.className='breadcrumb-sep';sep.textContent='/';
    bar.appendChild(sep);
    accumulated+=(accumulated?'/':'')+parts[i];
    const seg=document.createElement('span');
    seg.textContent=parts[i];
    if(i<parts.length-1){
      seg.className='breadcrumb-seg breadcrumb-link';
      const target=accumulated;
      seg.onclick=()=>loadDir(target);
    } else {
      seg.className='breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

// Track expanded directories for tree view
if(!S._expandedDirs) S._expandedDirs=new Set();
// Cache of fetched directory contents: path -> entries[]
if(!S._dirCache) S._dirCache={};

function renderFileTree(){
  const box=$('fileTree');box.innerHTML='';
  // Cache current dir entries
  S._dirCache[S.currentDir||'.']=S.entries;
  _renderTreeItems(box, S.entries, 0);
}

function _renderTreeItems(container, entries, depth){
  for(const item of entries){
    const el=document.createElement('div');el.className='file-item';
    el.style.paddingLeft=(8+depth*16)+'px';

    if(item.type==='dir'){
      // Toggle arrow for directories
      const arrow=document.createElement('span');
      arrow.className='file-tree-toggle';
      const isExpanded=S._expandedDirs.has(item.path);
      arrow.textContent=isExpanded?'\u25BE':'\u25B8';
      el.appendChild(arrow);
    }

    // Icon
    const iconEl=document.createElement('span');
    iconEl.className='file-icon';iconEl.textContent=fileIcon(item.name,item.type);
    el.appendChild(iconEl);

    // Name
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;nameEl.title='Double-click to rename';
    nameEl.ondblclick=(e)=>{
      e.stopPropagation();
      // For directories, double-click navigates (breadcrumb view)
      if(item.type==='dir'){loadDir(item.path);return;}
      const inp=document.createElement('input');
      inp.className='file-rename-input';inp.value=item.name;
      inp.onclick=(e2)=>e2.stopPropagation();
      const finish=async(save)=>{
        inp.onblur=null;
        if(save){
          const newName=inp.value.trim();
          if(newName&&newName!==item.name){
            try{
              await api('/api/file/rename',{method:'POST',body:JSON.stringify({
                session_id:S.session.session_id,path:item.path,new_name:newName
              })});
              showToast(`Renamed to ${newName}`);
              // Invalidate cache and re-render
              delete S._dirCache[S.currentDir];
              await loadDir(S.currentDir);
            }catch(err){showToast('Rename failed: '+err.message);}
          }
        }
        inp.replaceWith(nameEl);
      };
      inp.onkeydown=(e2)=>{
        if(e2.key==='Enter'){e2.preventDefault();finish(true);}
        if(e2.key==='Escape'){e2.preventDefault();finish(false);}
      };
      inp.onblur=()=>finish(false);
      nameEl.replaceWith(inp);
      setTimeout(()=>{inp.focus();inp.select();},10);
    };
    el.appendChild(nameEl);

    // Size -- only for files
    if(item.type==='file'&&item.size){
      const sizeEl=document.createElement('span');
      sizeEl.className='file-size';
      sizeEl.textContent=`${(item.size/1024).toFixed(1)}k`;
      el.appendChild(sizeEl);
    }

    // Delete button -- for files
    if(item.type==='file'){
      const del=document.createElement('button');
      del.className='file-del-btn';del.title='Delete';del.textContent='\u00d7';
      del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceFile(item.path,item.name);};
      el.appendChild(del);
    }

    if(item.type==='dir'){
      // Single-click toggles expand/collapse
      el.onclick=async(e)=>{
        e.stopPropagation();
        if(S._expandedDirs.has(item.path)){
          S._expandedDirs.delete(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          renderFileTree();
        }else{
          S._expandedDirs.add(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          // Fetch children if not cached
          if(!S._dirCache[item.path]){
            try{
              const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(item.path)}`);
              S._dirCache[item.path]=data.entries||[];
            }catch(e2){S._dirCache[item.path]=[];}
          }
          renderFileTree();
        }
      };
    }else{
      el.onclick=async()=>openFile(item.path);
    }

    container.appendChild(el);

    // Render children if directory is expanded
    if(item.type==='dir'&&S._expandedDirs.has(item.path)){
      const children=S._dirCache[item.path]||[];
      if(children.length){
        _renderTreeItems(container, children, depth+1);
      }else{
        const empty=document.createElement('div');
        empty.className='file-item file-empty';
        empty.style.paddingLeft=(8+(depth+1)*16)+'px';
        empty.textContent='(empty)';
        container.appendChild(empty);
      }
    }
  }
}

async function deleteWorkspaceFile(relPath, name){
  if(!S.session)return;
  if(!confirm(`Delete ${name}?`))return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(`Deleted ${name}`);
    // Close preview if we just deleted the viewed file
    if($('previewPathText').textContent===relPath)$('btnClearPreview').onclick();
    await loadDir(S.currentDir);
  }catch(e){setStatus('Delete failed: '+e.message);}
}

async function promptNewFile(){
  if(!S.session)return;
  const name=prompt('New file name (e.g. notes.md):','');
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath,content:''})});
    showToast(`Created ${name.trim()}`);
    await loadDir(S.currentDir);
    openFile(relPath);
  }catch(e){setStatus('Create failed: '+e.message);}
}

async function promptNewFolder(){
  if(!S.session)return;
  const name=prompt('New folder name:','');
  if(!name||!name.trim())return;
  const relPath=S.currentDir==='.'?name.trim():(S.currentDir+'/'+name.trim());
  try{
    await api('/api/file/create-dir',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(`Created folder ${name.trim()}`);
    await loadDir(S.currentDir);
  }catch(e){setStatus('Create folder failed: '+e.message);}
}

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');updateSendBtn();return;}
  tray.classList.add('has-files');
  updateSendBtn();
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    chip.innerHTML=`&#128206; ${esc(f.name)} <button title="Remove">&#10005;</button>`;
    chip.querySelector('button').onclick=()=>{S.pendingFiles.splice(i,1);renderTray();};
    tray.appendChild(chip);
  });
}
function addFiles(files){for(const f of files){if(!S.pendingFiles.find(p=>p.name===f.name))S.pendingFiles.push(f);}renderTray();}

async function uploadPendingFiles(){
  if(!S.pendingFiles.length||!S.session)return[];
  const names=[];let failures=0;
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  barWrap.classList.add('active');bar.style.width='0%';
  const total=S.pendingFiles.length;
  for(let i=0;i<total;i++){
    const f=S.pendingFiles[i];const fd=new FormData();
    fd.append('session_id',S.session.session_id);fd.append('file',f,f.name);
    try{
      const res=await fetch(new URL('/console/api/upload',location.origin).href,{method:'POST',credentials:'include',body:fd});
      if(!res.ok){const err=await res.text();throw new Error(err);}
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      names.push(data.filename);
    }catch(e){failures++;setStatus(`\u274c Upload failed: ${f.name} \u2014 ${e.message}`);}
    bar.style.width=`${Math.round((i+1)/total*100)}%`;
  }
  barWrap.classList.remove('active');bar.style.width='0%';
  S.pendingFiles=[];renderTray();
  if(failures===total&&total>0)throw new Error(`All ${total} upload(s) failed`);
  return names;
}


// ═══════════════════════════════════════════════════════════════════════════
// HERMES UI UPGRADE — NEW FEATURE FUNCTIONS
// Features: 1=Collapsible Tool Outputs, 2=Activity Timeline,
//           5=File Artifact Cards, 6=Todo Progress Panel
// ═══════════════════════════════════════════════════════════════════════════

// ── Feature 1: Build a collapsible tool output card ──
// Called from messages.js when a tool_result SSE event arrives.
// Also used by renderMessages() for history view of tool messages.
// Returns a DOM element (div.tool-output-card).
function buildToolOutputCard(toolName, content, opts) {
  opts = opts || {};
  const card = document.createElement('div');
  card.className = 'tool-output-card';

  // Determine a human-friendly label for the header
  let label = toolName || 'Tool output';
  let icon = '🔧';
  let parsedContent = content;

  // Attempt to parse JSON content
  let parsed = null;
  if (typeof content === 'string') {
    try { parsed = JSON.parse(content); } catch(_) {}
  }

  // Normalize tool names for smart headers
  const tn = (toolName || '').toLowerCase();
  if (tn === 'terminal' || tn === 'run_command' || tn === 'bash' || tn === 'execute_command') {
    icon = '⬛';
    const cmd = (opts.command || (parsed && (parsed.command || parsed.cmd)) || '').slice(0, 60);
    label = cmd ? `$ ${cmd}` : 'Terminal output';
  } else if (tn === 'read_file' || tn === 'read') {
    icon = '📄';
    const fname = opts.filename || (parsed && parsed.filename) || '';
    const lines = opts.lines || (parsed && parsed.lines) || '';
    label = fname ? `Read ${fname}${lines ? ` (${lines} lines)` : ''}` : 'Read file';
  } else if (tn === 'write_file' || tn === 'write') {
    icon = '✏️';
    const fname = opts.filename || (parsed && parsed.filename) || '';
    label = fname ? `Wrote ${fname}` : 'Write file';
  } else if (tn === 'search_files' || tn === 'grep') {
    icon = '🔍';
    label = 'Search results';
  } else if (tn === 'web_search') {
    icon = '🌐';
    label = 'Web search results';
  } else if (tn === 'memory') {
    icon = '🧠';
    label = 'Memory operation';
  }

  // Render the output text — prefer string, fall back to formatted JSON
  let outputText = '';
  if (typeof content === 'string') {
    outputText = content;
  } else if (parsed !== null) {
    outputText = JSON.stringify(parsed, null, 2);
  } else {
    outputText = String(content);
  }

  card.innerHTML = `
    <div class="tool-output-card-header" onclick="this.closest('.tool-output-card').classList.toggle('open')">
      <span class="tool-output-card-icon">${icon}</span>
      <span class="tool-output-card-label">${esc(label)}</span>
      <span class="tool-output-card-toggle">▸</span>
    </div>
    <div class="tool-output-card-body">
      <pre>${esc(outputText)}</pre>
    </div>`;

  return card;
}

// ── Feature 2: Activity timeline helpers ──
// Keeps a map of activity item DOM elements keyed by tool-call id (or name).
const _activityItems = {};

// Append a new "running" activity item into the live tool cards area.
function appendActivityItem(toolName, preview, tid) {
  const key = tid || toolName;
  // Don't add duplicates
  if (_activityItems[key]) return;

  const container = $('liveToolCards');
  if (!container) return;
  container.style.display = '';

  const item = document.createElement('div');
  item.className = 'activity-item';
  item.dataset.actKey = key;
  const desc = _activityLabel(toolName, preview);
  item.innerHTML = `
    <div class="activity-spinner">
      <span></span><span></span><span></span>
    </div>
    <span class="activity-text">${esc(desc)}</span>`;
  _activityItems[key] = item;
  container.appendChild(item);
  scrollIfPinned();
}

// Mark an activity item as done (replace spinner with checkmark).
function resolveActivityItem(toolName, tid) {
  const key = tid || toolName;
  const item = _activityItems[key];
  if (!item) return;
  item.classList.add('done');
  item.querySelector('.activity-spinner').outerHTML = '<span class="activity-check">✓</span>';
  const textEl = item.querySelector('.activity-text');
  if (textEl) {
    const prev = textEl.textContent;
    textEl.textContent = prev.replace(/^Running |^Calling |^Executing /, '') + ' · done';
  }
  // Auto-remove done items after 4s to keep the stream clean
  setTimeout(() => {
    if (item.parentElement) item.remove();
    delete _activityItems[key];
  }, 4000);
}

// Clear all activity items (called on stream done/cancel)
function clearActivityItems() {
  Object.keys(_activityItems).forEach(k => {
    if (_activityItems[k] && _activityItems[k].parentElement) {
      _activityItems[k].remove();
    }
    delete _activityItems[k];
  });
}

function _activityLabel(toolName, preview) {
  const tn = (toolName || '').toLowerCase();
  const p = preview ? preview.slice(0, 55) : '';
  if (tn === 'terminal' || tn === 'bash' || tn === 'execute_command' || tn === 'run_command') {
    return p ? `Running: ${p}` : 'Running terminal command…';
  }
  if (tn === 'read_file' || tn === 'read') return p ? `Reading ${p}` : 'Reading file…';
  if (tn === 'write_file' || tn === 'write') return p ? `Writing ${p}` : 'Writing file…';
  if (tn === 'web_search') return p ? `Searching: ${p}` : 'Searching the web…';
  if (tn === 'web_extract') return p ? `Fetching: ${p}` : 'Fetching URL…';
  if (tn === 'search_files' || tn === 'grep') return p ? `Searching files: ${p}` : 'Searching files…';
  if (tn === 'memory') return 'Accessing memory…';
  if (tn === 'todo') return 'Updating task list…';
  if (tn === 'delegate_task' || tn === 'subagent_progress') return p ? `Delegating: ${p}` : 'Delegating task…';
  return p ? `${toolName}: ${p}` : `Calling ${toolName}…`;
}

// ── Feature 5: Build a file artifact card ──
// Shown when a tool_result reveals a written file.
// Returns a DOM element (div.file-artifact-card) or null if not a file artifact.
function buildFileArtifactCard(toolName, content, opts) {
  opts = opts || {};
  const tn = (toolName || '').toLowerCase();
  const isWriteFile = tn === 'write_file' || tn === 'write' || tn === 'create_file';

  // Detect file write from content text if tool name doesn't match
  let detectedFilename = opts.filename || '';
  let detectedSize = opts.size || '';

  if (!detectedFilename) {
    // Try to parse JSON output for filename
    if (typeof content === 'string') {
      try {
        const p = JSON.parse(content);
        detectedFilename = p.filename || p.path || p.file || '';
        detectedSize = p.size || p.bytes || '';
      } catch(_) {
        // Try regex patterns: "Wrote /path/to/file.ext" or "wrote file.ext"
        const m = content.match(/[Ww]rote\s+(?:to\s+)?([^\s,\n(]+\.[a-zA-Z0-9]+)/);
        if (m) detectedFilename = m[1];
      }
    }
  }

  // Only render a file card if we have a filename and it's a write operation
  if (!isWriteFile && !detectedFilename) return null;
  if (!detectedFilename && !isWriteFile) return null;
  const fname = detectedFilename || (opts.args && opts.args.path) || 'file';

  // Determine icon and extension
  const ext = fname.split('.').pop().toLowerCase();
  const imageExts = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'ico']);
  const isImage = imageExts.has(ext);
  let fileIcon = '📄';
  if (isImage) fileIcon = '🖼️';
  else if (ext === 'pdf') fileIcon = '📕';
  else if (['js', 'ts', 'jsx', 'tsx'].includes(ext)) fileIcon = '⚡';
  else if (ext === 'py') fileIcon = '🐍';
  else if (['md', 'txt', 'rst'].includes(ext)) fileIcon = '📝';
  else if (['json', 'yaml', 'yml', 'toml'].includes(ext)) fileIcon = '⚙️';
  else if (['zip', 'tar', 'gz', 'bz2'].includes(ext)) fileIcon = '📦';
  else if (['mp3', 'wav', 'ogg', 'm4a'].includes(ext)) fileIcon = '🎵';
  else if (['mp4', 'webm', 'mov', 'avi'].includes(ext)) fileIcon = '🎬';

  // Format size
  let sizeText = '';
  if (detectedSize) {
    const bytes = parseInt(detectedSize, 10);
    if (!isNaN(bytes)) {
      sizeText = bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
               : bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB`
               : `${bytes} B`;
    } else {
      sizeText = String(detectedSize);
    }
  }

  // Build workspace-relative open link (if session is active)
  const baseName = fname.replace(/.*[\\/]/, ''); // strip path, keep filename
  const openHref = S.session
    ? new URL(`/console/api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(fname)}`, location.origin).href
    : '#';

  const card = document.createElement('div');
  card.className = 'file-artifact-card';

  // For images, show a small thumbnail
  const thumbHtml = isImage && S.session
    ? `<img class="file-artifact-thumb" src="${esc(openHref)}" alt="${esc(baseName)}" onerror="this.style.display='none'">`
    : `<span class="file-artifact-icon">${fileIcon}</span>`;

  card.innerHTML = `
    ${thumbHtml}
    <div class="file-artifact-info">
      <div class="file-artifact-name">${esc(baseName)}</div>
      <div class="file-artifact-meta">${sizeText ? sizeText + ' · ' : ''}${ext.toUpperCase()}</div>
    </div>
    <a class="file-artifact-open" href="${esc(openHref)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Open</a>`;

  return card;
}

// ── Feature 6: Todo Progress Panel ──
// _liveTodos: array of {text, status} objects updated from SSE 'tool' events
// for the 'todo' tool.
let _liveTodos = [];

function updateLiveTodosFromToolCall(toolName, args) {
  if ((toolName || '').toLowerCase() !== 'todo') return;
  if (!args) return;
  // The todo tool typically passes items as args.items (array) or args.text
  const items = args.items || args.todos;
  if (Array.isArray(items)) {
    // Full replacement (create_todo_list style)
    _liveTodos = items.map(it => {
      if (typeof it === 'string') return {text: it, status: 'pending'};
      return {
        text: it.description || it.text || it.title || String(it),
        status: it.status || 'pending'
      };
    });
    renderLiveTodoPanel();
    return;
  }
  // Single item update
  const text = args.text || args.description || args.title || '';
  const status = args.status || args.action || 'pending';
  const idx = args.index !== undefined ? parseInt(args.index, 10) : -1;
  if (idx >= 0 && idx < _liveTodos.length) {
    _liveTodos[idx].status = _normalizeStatus(status);
    if (text) _liveTodos[idx].text = text;
  } else if (text) {
    // Find existing by text match
    const existing = _liveTodos.find(t => t.text === text);
    if (existing) {
      existing.status = _normalizeStatus(status);
    } else if (status !== 'update') {
      _liveTodos.push({text, status: _normalizeStatus(status)});
    }
  }
  renderLiveTodoPanel();
}

function _normalizeStatus(s) {
  const v = (s || '').toLowerCase();
  if (v === 'completed' || v === 'done' || v === 'complete' || v === 'finish') return 'done';
  if (v === 'in_progress' || v === 'in-progress' || v === 'active' || v === 'running') return 'in-progress';
  return 'pending';
}

function renderLiveTodoPanel() {
  const container = $('liveToolCards');
  if (!container || !_liveTodos.length) return;
  container.style.display = '';

  let panel = container.querySelector('.todo-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'todo-panel open';
    // Prepend so it sits above the tool activity items
    container.insertBefore(panel, container.firstChild);
  }

  const doneCount = _liveTodos.filter(t => t.status === 'done').length;
  const total = _liveTodos.length;

  panel.innerHTML = `
    <div class="todo-panel-header" onclick="this.closest('.todo-panel').classList.toggle('open')">
      <span class="todo-panel-title">✅ Task Progress</span>
      <span class="todo-panel-count">${doneCount}/${total} done</span>
      <span class="todo-panel-toggle">▸</span>
    </div>
    <div class="todo-panel-body">
      ${_liveTodos.map(item => _renderTodoItem(item)).join('')}
    </div>`;
  scrollIfPinned();
}

function _renderTodoItem(item) {
  const s = item.status || 'pending';
  const statusIcon = s === 'done' ? '●' : s === 'in-progress' ? '◐' : '○';
  return `<div class="todo-item ${s}">
    <span class="todo-item-status">${statusIcon}</span>
    <span class="todo-item-text">${esc(item.text)}</span>
  </div>`;
}

// Render a static todo panel from a parsed list (used in renderMessages history view).
function buildTodoPanelElement(items) {
  if (!items || !items.length) return null;
  const panel = document.createElement('div');
  panel.className = 'todo-panel';
  const doneCount = items.filter(t => (t.status || '') === 'done' || (t.status || '') === 'completed').length;
  const total = items.length;
  panel.innerHTML = `
    <div class="todo-panel-header" onclick="this.closest('.todo-panel').classList.toggle('open')">
      <span class="todo-panel-title">✅ Task Progress</span>
      <span class="todo-panel-count">${doneCount}/${total} done</span>
      <span class="todo-panel-toggle">▸</span>
    </div>
    <div class="todo-panel-body">
      ${items.map(item => {
        const s = (item.status || 'pending').toLowerCase()
          .replace('completed','done').replace('in_progress','in-progress');
        const icon = s === 'done' ? '●' : s === 'in-progress' ? '◐' : '○';
        return `<div class="todo-item ${s}">
          <span class="todo-item-status">${icon}</span>
          <span class="todo-item-text">${esc(item.description || item.text || item.title || String(item))}</span>
        </div>`;
      }).join('')}
    </div>`;
  return panel;
}

// Clear live todos when a stream ends
function clearLiveTodos() {
  _liveTodos = [];
  const container = $('liveToolCards');
  if (container) {
    const panel = container.querySelector('.todo-panel');
    if (panel) panel.remove();
  }
}

// ── Feature 4: Error Card rendering helpers ──
// Called from messages.js to render a retryable error card.
function buildErrorCard(errorMsg, lastUserText) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-row';

  // Build model options from the current modelSelect
  const sel = $('modelSelect');
  let modelOptions = '';
  if (sel) {
    Array.from(sel.options).forEach(opt => {
      const selected = opt.value === (S.session && S.session.model) ? ' selected' : '';
      modelOptions += `<option value="${esc(opt.value)}"${selected}>${esc(opt.textContent)}</option>`;
    });
  }

  const card = document.createElement('div');
  card.className = 'error-card';
  card.innerHTML = `
    <div class="error-card-header">⚠️ Error</div>
    <div class="error-card-message">${esc(errorMsg)}</div>
    <div class="error-card-actions">
      <button class="error-retry-btn" onclick="retryLastMessage()">↩ Retry</button>
      ${modelOptions ? `
      <select class="error-model-select" id="errorModelSwitch" onchange="">
        ${modelOptions}
      </select>
      <button class="error-retry-btn" onclick="retryWithModel(document.getElementById('errorModelSwitch').value)">Retry with model</button>
      ` : ''}
    </div>`;
  wrap.appendChild(card);
  return wrap;
}

// Retry by re-sending the last user message
async function retryLastMessage() {
  if (S.busy) return;
  // Find last user message
  const lastUser = [...S.messages].reverse().find(m => m && m.role === 'user');
  if (!lastUser) return;
  const text = msgContent(lastUser);
  if (!text) return;
  // Remove the last error assistant message if present
  const last = S.messages[S.messages.length - 1];
  if (last && last.role === 'assistant') S.messages.pop();
  $('msg').value = text;
  await send();
}

// Retry with a different model
async function retryWithModel(modelId) {
  if (!modelId || S.busy) return;
  // Switch model on the session
  if (S.session) {
    S.session.model = modelId;
    _applyModelToDropdown(modelId, $('modelSelect'));
    $('modelChip').textContent = getModelLabel(modelId);
  }
  await retryLastMessage();
}

// ── Feature 6: Sidebar todo panel sync ──
// loadTodos() is called by renderMessages() when the panelTodos panel is active.
// It renders _liveTodos (if active) or a history message from the last tool call
// that included todo data.
function loadTodos() {
  const panel = $('todoPanel');
  if (!panel) return;

  // If there are live todos from the current stream, show them
  if (typeof _liveTodos !== 'undefined' && _liveTodos.length) {
    panel.innerHTML = '';
    const panelEl = buildTodoPanelElement(_liveTodos);
    if (panelEl) {
      panelEl.classList.add('open'); // expanded by default in sidebar
      panel.appendChild(panelEl);
    }
    return;
  }

  // Otherwise try to find todo data in the session's tool calls
  if (!S.toolCalls || !S.toolCalls.length) {
    panel.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px 0;opacity:.6">No active task list. Task progress will appear here during agent runs.</div>';
    return;
  }

  // Find the last 'todo' tool call and extract items from its snippet
  const todoCall = [...S.toolCalls].reverse().find(tc => (tc.name || '').toLowerCase() === 'todo');
  if (!todoCall) {
    panel.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px 0;opacity:.6">No todo data found in this session.</div>';
    return;
  }

  let items = [];
  if (todoCall.args && (todoCall.args.items || todoCall.args.todos)) {
    items = (todoCall.args.items || todoCall.args.todos).map(it =>
      typeof it === 'string' ? {text: it, status: 'pending'} :
      {text: it.description || it.text || it.title || String(it), status: it.status || 'pending'}
    );
  } else if (todoCall.snippet) {
    try {
      const p = JSON.parse(todoCall.snippet);
      if (Array.isArray(p)) items = p;
      else if (p.items) items = p.items;
    } catch(_) {}
  }

  if (items.length) {
    panel.innerHTML = '';
    const panelEl = buildTodoPanelElement(items);
    if (panelEl) { panelEl.classList.add('open'); panel.appendChild(panelEl); }
  } else {
    panel.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px 0;opacity:.6">No task items found.</div>';
  }
}
