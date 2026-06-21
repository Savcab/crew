// modal.js — the crew's forms: create an agent, describe a relationship, edit or
// delete an edge, adopt an independent session. Owns #cmodal (+ #modalTitle /
// #modalBody / #modalClose). Every submit posts through `api` and then calls
// refresh() so the graph repaints from the new server state.
//
// createModalController({ api, toast, refresh })

function esc(s) {
  return (s || '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

export function createModalController({ api, toast, refresh }) {
  toast = toast || (() => {});
  refresh = refresh || (() => {});
  const modal = document.getElementById('cmodal');
  const titleEl = document.getElementById('modalTitle');
  const bodyEl = document.getElementById('modalBody');
  const closeBtn = document.getElementById('modalClose');

  function isOpen() { return modal.classList.contains('show'); }
  function close() { modal.classList.remove('show'); bodyEl.innerHTML = ''; }
  if (closeBtn) closeBtn.onclick = close;
  modal.addEventListener('mousedown', e => { if (e.target === modal) close(); });

  // Build one labelled field. kind: 'text' | 'textarea' | 'checkbox'. `note` is an
  // optional one-line helper rendered under the control (plain-language guidance).
  function field(id, label, kind, value, ph, note) {
    const hint = note ? `<div class="f-note">${esc(note)}</div>` : '';
    if (kind === 'checkbox') {
      return `<label class="f-check"><input type="checkbox" id="${id}" ${value ? 'checked' : ''}> ${esc(label)}</label>${hint}`;
    }
    const ctrl = kind === 'textarea'
      ? `<textarea id="${id}" rows="3" placeholder="${esc(ph || '')}">${esc(value || '')}</textarea>`
      : `<input type="text" id="${id}" value="${esc(value || '')}" placeholder="${esc(ph || '')}">`;
    return `<div class="f-row"><label for="${id}">${esc(label)}</label>${ctrl}${hint}</div>`;
  }

  function open(title, html) {
    titleEl.textContent = title;
    bodyEl.innerHTML = html;
    modal.classList.add('show');
  }

  const val = id => (document.getElementById(id) || {}).value;
  const checked = id => !!(document.getElementById(id) || {}).checked;

  async function submit(fn, okMsg) {
    let r;
    try { r = await fn(); }
    catch (e) { toast('request failed', true); return; }
    if (r && r.ok === false) { toast(r.error || 'failed', true); return; }
    toast(okMsg);
    close();
    refresh(true);
  }

  // ---- + Agent ---- //
  function openCreateAgent() {
    open('Create agent', `
      ${field('a-name', 'Name (unique, no spaces)', 'text', '', 'leads')}
      ${field('a-role', 'Role (short)', 'text', '', 'finds businesses with no website')}
      ${field('a-identity', 'Identity / mission', 'textarea', '', 'who this agent is and what it owns')}
      ${field('a-home', 'Home folder (optional)', 'text', '', 'defaults to ./<name>',
              'the agent lives and works only here; one agent per folder')}
      ${field('a-repo', 'Start on a copy of a repo (optional)', 'text', '', '/path/to/repo',
              'instead of a home folder, give it a fresh branch (git worktree) of an existing repo')}
      ${field('a-launch-cmd', 'Launch command', 'text', '', 'claude --dangerously-skip-permissions',
              'runs the agent with permission prompts disabled so it works unattended — fine for its own isolated folder. Blank = default.')}
      ${field('a-launch', 'Launch it now', 'checkbox', true)}
      <div class="f-actions"><button class="btn primary" id="a-go">Create agent</button></div>
      <div class="f-hint">Homes can't overlap or nest, so crew members never collide on disk. The agent's identity is saved in its folder and loaded automatically every time it starts.</div>
    `);
    document.getElementById('a-go').onclick = () => {
      const name = (val('a-name') || '').trim();
      if (!name) { toast('name required', true); return; }
      submit(() => api.agentCreate({
        name, role: val('a-role'), identity: val('a-identity'),
        home: val('a-home') || undefined, repo: val('a-repo') || undefined,
        launch_cmd: val('a-launch-cmd') || undefined,
        launch: checked('a-launch'),
      }), `creating ${name}…`);
    };
  }

  // ---- connect (describe a new edge between two agents) ---- //
  function openConnect(sourceName, targetName) {
    open('Describe the relationship', `
      <div class="f-pair"><b>${esc(sourceName)}</b> <span class="arrow">→</span> <b>${esc(targetName)}</b></div>
      ${field('e-label', 'Label', 'text', '', 'qualified lead')}
      ${field('e-when', `When should ${esc(sourceName)} message ${esc(targetName)}?`, 'textarea', '', 'e.g. when a qualified lead is found')}
      ${field('e-does', `What should ${esc(targetName)} do when it gets that message?`, 'textarea', '', `e.g. build a one-page demo and reply with the URL`)}
      ${field('e-reply', `${esc(targetName)} should reply back to ${esc(sourceName)}`, 'checkbox', false)}
      ${field('e-max', 'Limit messages per hour (0 = no limit)', 'text', '0', '0',
              'caps back-and-forth so two agents never loop forever')}
      ${field('e-undirected', 'Two-way (either may message the other)', 'checkbox', false)}
      <div class="f-actions"><button class="btn primary" id="e-go">Connect</button></div>
      <div class="f-hint">This is the only channel that exists: ${esc(sourceName)} → ${esc(targetName)} (unless two-way). Both the trigger and what ${esc(targetName)} does are written into each agent's identity.</div>
    `);
    document.getElementById('e-go').onclick = () => {
      submit(() => api.edgeCreate({
        source: sourceName, target: targetName,
        label: val('e-label'), condition: val('e-when'),
        target_action: val('e-does'), reply_expected: checked('e-reply'),
        max_turns: parseInt(val('e-max'), 10) || 0,
        directed: !checked('e-undirected'),
      }), `connected ${sourceName} → ${targetName}`);
    };
  }

  // ---- edit / delete an existing edge ---- //
  function openEditEdge(edge) {
    const dirName = edge.directed === false ? ' (two-way)' : '';
    open('Edit relationship', `
      <div class="f-pair"><b>${esc(edge.source_name)}</b>
        <span class="arrow">${edge.directed === false ? '↔' : '→'}</span>
        <b>${esc(edge.target_name)}</b><span class="dim">${dirName}</span></div>
      ${field('e-label', 'Label', 'text', edge.label, '')}
      ${field('e-when', `When should ${esc(edge.source_name)} message ${esc(edge.target_name)}?`, 'textarea', edge.condition, '')}
      ${field('e-does', `What should ${esc(edge.target_name)} do on receipt?`, 'textarea', edge.target_action, '')}
      ${field('e-reply', `${esc(edge.target_name)} should reply back`, 'checkbox', !!edge.reply_expected)}
      ${field('e-max', 'Limit messages per hour (0 = no limit)', 'text', String(edge.max_turns || 0), '0')}
      ${field('e-undirected', 'Two-way (either may message the other)', 'checkbox', edge.directed === false)}
      <div class="f-actions">
        <button class="btn danger" id="e-del">Delete</button>
        <button class="btn primary" id="e-save">Save</button>
      </div>
    `);
    document.getElementById('e-save').onclick = () => {
      submit(() => api.edgeUpdate({
        guid: edge._guid, label: val('e-label'), condition: val('e-when'),
        target_action: val('e-does'), reply_expected: checked('e-reply'),
        max_turns: parseInt(val('e-max'), 10) || 0,
        directed: !checked('e-undirected'),
      }), 'edge updated');
    };
    document.getElementById('e-del').onclick = () => {
      submit(() => api.edgeDelete({ guid: edge._guid }), 'edge deleted');
    };
  }

  return { isOpen, closeModal: close, openCreateAgent, openConnect, openEditEdge };
}
