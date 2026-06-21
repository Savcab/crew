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

  // Build one labelled field. kind: 'text' | 'textarea' | 'checkbox'.
  function field(id, label, kind, value, ph) {
    if (kind === 'checkbox') {
      return `<label class="f-check"><input type="checkbox" id="${id}" ${value ? 'checked' : ''}> ${esc(label)}</label>`;
    }
    const ctrl = kind === 'textarea'
      ? `<textarea id="${id}" rows="3" placeholder="${esc(ph || '')}">${esc(value || '')}</textarea>`
      : `<input type="text" id="${id}" value="${esc(value || '')}" placeholder="${esc(ph || '')}">`;
    return `<div class="f-row"><label for="${id}">${esc(label)}</label>${ctrl}</div>`;
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
      ${field('a-home', 'Home directory (optional)', 'text', '', 'defaults to ./<name>')}
      ${field('a-repo', '…or a git repo to branch a worktree from (optional)', 'text', '', '/path/to/repo')}
      ${field('a-launch-cmd', 'Launch command', 'text', '', 'claude --dangerously-skip-permissions')}
      ${field('a-launch', 'Launch it now', 'checkbox', true)}
      <div class="f-actions"><button class="btn primary" id="a-go">Create agent</button></div>
      <div class="f-hint">One agent per directory — homes can't overlap or nest, so crew members never collide on disk. The agent's identity is written into its home as <code>identity.md</code> + an auto-loaded <code>CLAUDE.md</code>. Leave the launch command blank to use the default.</div>
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
      ${field('e-label', 'Label', 'text', '', 'leads → builder')}
      ${field('e-desc', `What does ${esc(sourceName)} do, what does ${esc(targetName)} do?`, 'textarea', '', 'how these two relate')}
      ${field('e-when', `When should ${esc(sourceName)} message ${esc(targetName)}?`, 'textarea', '', 'e.g. when a qualified lead is found')}
      ${field('e-undirected', 'Two-way (either may message the other)', 'checkbox', false)}
      <div class="f-actions"><button class="btn primary" id="e-go">Connect</button></div>
      <div class="f-hint">This edge authorizes messaging: ${esc(sourceName)} → ${esc(targetName)} only (unless two-way). It's written into both agents' identity.md.</div>
    `);
    document.getElementById('e-go').onclick = () => {
      submit(() => api.edgeCreate({
        source: sourceName, target: targetName,
        label: val('e-label'), description: val('e-desc'),
        condition: val('e-when'), directed: !checked('e-undirected'),
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
      ${field('e-desc', 'Relationship', 'textarea', edge.description, '')}
      ${field('e-when', 'When to message', 'textarea', edge.condition, '')}
      ${field('e-undirected', 'Two-way (either may message the other)', 'checkbox', edge.directed === false)}
      <div class="f-actions">
        <button class="btn danger" id="e-del">Delete</button>
        <button class="btn primary" id="e-save">Save</button>
      </div>
    `);
    document.getElementById('e-save').onclick = () => {
      submit(() => api.edgeUpdate({
        guid: edge._guid, label: val('e-label'), description: val('e-desc'),
        condition: val('e-when'), directed: !checked('e-undirected'),
      }), 'edge updated');
    };
    document.getElementById('e-del').onclick = () => {
      submit(() => api.edgeDelete({ guid: edge._guid }), 'edge deleted');
    };
  }

  return { isOpen, closeModal: close, openCreateAgent, openConnect, openEditEdge };
}
