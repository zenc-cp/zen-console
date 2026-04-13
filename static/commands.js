// ── Slash commands ──────────────────────────────────────────────────────────
// Built-in commands intercepted before send(). Each command runs locally
// (no round-trip to the agent) and shows feedback via toast or local message.

const COMMANDS=[
  {name:'help',      desc:'List available commands',             fn:cmdHelp},
  {name:'clear',     desc:'Clear conversation messages',         fn:cmdClear},
  {name:'model',     desc:'Switch model (e.g. /model gpt-4o)',  fn:cmdModel,     arg:'model_name'},
  {name:'workspace', desc:'Switch workspace by name',            fn:cmdWorkspace, arg:'name'},
  {name:'new',       desc:'Start a new chat session',            fn:cmdNew},
  {name:'usage',     desc:'Toggle token usage display on/off',   fn:cmdUsage},
  // P6: ZenOps quick commands
  {name:'zenops',    desc:'ZenOps ops: status|deploy|log|restart', fn:cmdZenops, arg:'subcommand'},
  {name:'hunter',    desc:'Delegate to Hunter agent',            fn:cmdHunter,    arg:'task'},
  {name:'trader',    desc:'Check Trader status or signal',       fn:cmdTrader,    arg:'args'},
  {name:'sentinel',   desc:'Check Sentinel scan status',        fn:cmdSentinel,   arg:'args'},
  {name:'scribe',    desc:'Delegate to Scribe agent',            fn:cmdScribe,     arg:'task'},
  {name:'branch',    desc:'Branch conversation to compare A vs B', fn:cmdBranch},
];

function parseCommand(text){
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\s+/);
  const name=parts[0].toLowerCase();
  const args=parts.slice(1).join(' ').trim();
  return {name,args};
}

function executeCommand(text){
  const parsed=parseCommand(text);
  if(!parsed)return false;
  const cmd=COMMANDS.find(c=>c.name===parsed.name);
  if(!cmd)return false;
  cmd.fn(parsed.args);
  return true;
}

function getMatchingCommands(prefix){
  const q=prefix.toLowerCase();
  return COMMANDS.filter(c=>c.name.startsWith(q));
}

// ── Command handlers ────────────────────────────────────────────────────────

function cmdHelp(){
  const lines=COMMANDS.map(c=>{
    const usage=c.arg?` <${c.arg}>`:'';
    return `  /${c.name}${usage} — ${c.desc}`;
  });
  const msg={role:'assistant',content:'**Available commands:**\n'+lines.join('\n')};
  S.messages.push(msg);
  renderMessages();
  showToast('Type / to see commands');
}

function cmdClear(){
  if(!S.session)return;
  S.messages=[];S.toolCalls=[];
  clearLiveToolCards();
  renderMessages();
  $('emptyState').style.display='';
  showToast('Conversation cleared');
}

async function cmdModel(args){
  if(!args){showToast('Usage: /model <name>');return;}
  const sel=$('modelSelect');
  if(!sel)return;
  const q=args.toLowerCase();
  // Fuzzy match: find first option whose label or value contains the query
  let match=null;
  for(const opt of sel.options){
    if(opt.value.toLowerCase().includes(q)||opt.textContent.toLowerCase().includes(q)){
      match=opt.value;break;
    }
  }
  if(!match){showToast(`No model matching "${args}"`);return;}
  sel.value=match;
  await sel.onchange();
  showToast(`Switched to ${match}`);
}

async function cmdWorkspace(args){
  if(!args){showToast('Usage: /workspace <name>');return;}
  try{
    const data=await api('/api/workspaces');
    const q=args.toLowerCase();
    const ws=(data.workspaces||[]).find(w=>
      (w.name||'').toLowerCase().includes(q)||w.path.toLowerCase().includes(q)
    );
    if(!ws){showToast(`No workspace matching "${args}"`);return;}
    if(!S.session)return;
    await api('/api/session/update',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,workspace:ws.path,model:S.session.model
    })});
    S.session.workspace=ws.path;
    syncTopbar();await loadDir('.');
    showToast(`Switched to workspace: ${ws.name||ws.path}`);
  }catch(e){showToast('Workspace switch failed: '+e.message);}
}

async function cmdNew(){
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast('New session created');
}

async function cmdUsage(){
  const next=!window._showTokenUsage;
  window._showTokenUsage=next;
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({show_token_usage:next})});
  }catch(e){}
  // Update the settings checkbox if the panel is open
  const cb=$('settingsShowTokenUsage');
  if(cb) cb.checked=next;
  renderMessages();
  showToast('Token usage '+(next?'on':'off'));
}

// ── P6: ZenOps quick commands ───────────────────────────────────────────────

async function cmdZenops(args){
  const sub=(args||'').trim().toLowerCase();
  setStatus('Running /zenops '+sub+'...');
  let result, ok=false;
  try{
    const data=await api('/api/zenops/exec',{
      method:'POST',
      body:JSON.stringify({cmd:sub,args:args})
    });
    result=data.result||JSON.stringify(data);
    ok=data.ok;
  }catch(e){
    result='Error: '+e.message;
  }
  setStatus('');
  const icon=ok?'✅':'❌';
  S.messages.push({role:'assistant',content:`**${icon} /zenops ${sub}**\n\`\`\`\n${result.slice(0,2000)}\n\`\`\`\n`});
  renderMessages();
  showToast('/zenops '+(ok?'completed':'failed'));
}

async function cmdHunter(args){
  if(!args.trim()){showToast('Usage: /hunter <task>');return;}
  setStatus('Delegating to Hunter...');
  try{
    const data=await api('/api/zenops/exec',{
      method:'POST',
      body:JSON.stringify({cmd:'hunter',args})
    });
    const icon=data.ok?'✅':'❌';
    S.messages.push({role:'assistant',content:`**${icon} Hunter**\n\`\`\`\n${(data.result||'').slice(0,2000)}\n\`\`\`\n`});
    renderMessages();
    showToast(data.ok?'Delegated to Hunter':'Hunter error');
  }catch(e){
    S.messages.push({role:'assistant',content:`**❌ Hunter delegation failed:** ${e.message}`});
    renderMessages();
  }
  setStatus('');
}

async function cmdTrader(args){
  setStatus('Checking Trader...');
  try{
    const data=await api('/api/zenops/exec',{
      method:'POST',
      body:JSON.stringify({cmd:'trader',args})
    });
    const icon=data.ok?'✅':'❌';
    S.messages.push({role:'assistant',content:`**${icon} Trader**\n\`\`\`\n${(data.result||'').slice(0,2000)}\n\`\`\`\n`});
    renderMessages();
    showToast(data.ok?'Trader status retrieved':'Trader error');
  }catch(e){
    S.messages.push({role:'assistant',content:`**❌ Trader check failed:** ${e.message}`});
    renderMessages();
  }
  setStatus('');
}

async function cmdSentinel(args){
  setStatus('Checking Sentinel...');
  try{
    const data=await api('/api/zenops/exec',{
      method:'POST',
      body:JSON.stringify({cmd:'sentinel',args})
    });
    const icon=data.ok?'✅':'❌';
    S.messages.push({role:'assistant',content:`**${icon} Sentinel**\n\`\`\`\n${(data.result||'').slice(0,2000)}\n\`\`\`\n`});
    renderMessages();
    showToast(data.ok?'Sentinel status retrieved':'Sentinel error');
  }catch(e){
    S.messages.push({role:'assistant',content:`**❌ Sentinel check failed:** ${e.message}`});
    renderMessages();
  }
  setStatus('');
}

async function cmdScribe(args){
  if(!args.trim()){showToast('Usage: /scribe <task>');return;}
  setStatus('Delegating to Scribe...');
  try{
    const data=await api('/api/zenops/exec',{
      method:'POST',
      body:JSON.stringify({cmd:'scribe',args})
    });
    const icon=data.ok?'✅':'❌';
    S.messages.push({role:'assistant',content:`**${icon} Scribe**\n\`\`\`\n${(data.result||'').slice(0,2000)}\n\`\`\`\n`});
    renderMessages();
    showToast(data.ok?'Delegated to Scribe':'Scribe error');
  }catch(e){
    S.messages.push({role:'assistant',content:`**❌ Scribe delegation failed:** ${e.message}`});
    renderMessages();
  }
  setStatus('');
}

// P6/B3: Conversation branching
async function cmdBranch(){
  if(!S.session){showToast('No active session to branch');return;}
  const parentSid=S.session.session_id;
  // Find the last user message index as the branch point
  let branchIdx=S.messages.length;
  for(let i=S.messages.length-1;i>=0;i--){
    if(S.messages[i].role==='user'){branchIdx=i+1;break;}
  }
  try{
    const data=await api('/api/session/branch',{
      method:'POST',
      body:JSON.stringify({session_id:parentSid,branch_index:branchIdx})
    });
    if(data.session){
      await newSession();
      S.session=data.session;
      S.messages=data.session.messages||[];
      localStorage.setItem('hermes-webui-session',S.session.session_id);
      syncTopbar();renderMessages();
      showToast('Branched from session "'+(S.session.title||'Untitled')+'"');
    } else {
      showToast('Branch failed: '+JSON.stringify(data));
    }
  }catch(e){
    showToast('Branch failed: '+e.message);
  }
}

// ── Autocomplete dropdown ───────────────────────────────────────────────────

let _cmdSelectedIdx=-1;

function showCmdDropdown(matches){
  const dd=$('cmdDropdown');
  if(!dd)return;
  dd.innerHTML='';
  _cmdSelectedIdx=-1;
  for(let i=0;i<matches.length;i++){
    const c=matches[i];
    const el=document.createElement('div');
    el.className='cmd-item';
    el.dataset.idx=i;
    const usage=c.arg?` <span class="cmd-item-arg">${esc(c.arg)}</span>`:'';
    el.innerHTML=`<div class="cmd-item-name">/${esc(c.name)}${usage}</div><div class="cmd-item-desc">${esc(c.desc)}</div>`;
    el.onmousedown=(e)=>{
      e.preventDefault();
      $('msg').value='/'+c.name+(c.arg?' ':'');
      hideCmdDropdown();
      $('msg').focus();
    };
    dd.appendChild(el);
  }
  dd.classList.add('open');
}

function hideCmdDropdown(){
  const dd=$('cmdDropdown');
  if(dd)dd.classList.remove('open');
  _cmdSelectedIdx=-1;
}

function navigateCmdDropdown(dir){
  const dd=$('cmdDropdown');
  if(!dd)return;
  const items=dd.querySelectorAll('.cmd-item');
  if(!items.length)return;
  items.forEach(el=>el.classList.remove('selected'));
  _cmdSelectedIdx+=dir;
  if(_cmdSelectedIdx<0)_cmdSelectedIdx=items.length-1;
  if(_cmdSelectedIdx>=items.length)_cmdSelectedIdx=0;
  items[_cmdSelectedIdx].classList.add('selected');
}

function selectCmdDropdownItem(){
  const dd=$('cmdDropdown');
  if(!dd)return;
  const items=dd.querySelectorAll('.cmd-item');
  if(_cmdSelectedIdx>=0&&_cmdSelectedIdx<items.length){
    items[_cmdSelectedIdx].onmousedown({preventDefault:()=>{}});
  } else if(items.length===1){
    items[0].onmousedown({preventDefault:()=>{}});
  }
  hideCmdDropdown();
}
