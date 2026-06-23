// modal.js — the crew's forms: create an agent, describe a relationship, edit or
// delete an edge. Owns #cmodal (+ #modalTitle /
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

  // ---- condition LIST editor (multiple "when to message" triggers per direction) --
  function clRow(v, ph) {
    return `<div class="cl-row"><input type="text" class="cl-input" value="${esc(v || '')}" placeholder="${esc(ph || '')}"><button type="button" class="cl-del" title="remove">×</button></div>`;
  }
  function condList(id, label, values, ph) {
    const vals = (values && values.length) ? values : [''];
    return `<div class="f-row"><label>${esc(label)}</label>
      <div class="cl-rows" id="${id}" data-ph="${esc(ph || '')}">${vals.map(v => clRow(v, ph)).join('')}</div>
      <button type="button" class="cl-add" data-for="${id}">+ add another condition</button></div>`;
  }
  const readCondList = id => {
    const el = document.getElementById(id);
    return el ? [...el.querySelectorAll('.cl-input')].map(i => i.value.trim()).filter(Boolean) : [];
  };
  // an edge's trigger list for a direction (forward / back), with legacy fallback.
  const edgeConds = (edge, back) => {
    const k = back ? 'back_conditions' : 'conditions';
    if (Array.isArray(edge[k]) && edge[k].length) return edge[k];
    if (!back && edge.condition) return [edge.condition];
    return [];
  };
  // ONE delegated listener for the +add / ×remove buttons across any open form.
  bodyEl.addEventListener('click', (ev) => {
    const add = ev.target.closest('.cl-add');
    if (add) {
      const rows = document.getElementById(add.dataset.for);
      if (rows && !rows.classList.contains('off')) {
        rows.insertAdjacentHTML('beforeend', clRow('', rows.dataset.ph));
        const inp = rows.lastElementChild.querySelector('input'); if (inp) inp.focus();
      }
      return;
    }
    const del = ev.target.closest('.cl-del');
    if (del) {
      const rows = del.closest('.cl-rows');
      del.closest('.cl-row').remove();
      if (rows && !rows.querySelector('.cl-row')) rows.insertAdjacentHTML('beforeend', clRow('', rows.dataset.ph));
    }
  });
  // enable/disable the reverse-direction section based on the two-way toggle, and
  // flip the pair arrow → / ↔. (You can't fill out the other direction unless the
  // relationship is two-way — that wouldn't mean anything.)
  function wireTwoWay() {
    const tog = document.getElementById('e-undirected');
    const back = document.getElementById('e-back-wrap');
    const arrow = document.getElementById('e-arrow');
    if (!tog || !back) return;
    const sync = () => {
      const on = tog.checked;
      back.classList.toggle('disabled', !on);
      back.querySelectorAll('input,textarea,button').forEach(el => { el.disabled = !on; });
      back.querySelectorAll('.cl-rows').forEach(r => r.classList.toggle('off', !on));
      if (arrow) arrow.textContent = on ? '↔' : '→';
    };
    tog.addEventListener('change', sync); sync();
  }

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
  // Just two fields up front (name + what it does); everything optional is folded
  // into a native <details> so the common case is fill-two-boxes-and-go. The
  // advanced inputs stay in the DOM while collapsed, so the reads below still work.
  function openCreateAgent() {
    open('Create agent', `
      ${field('a-name', 'Name', 'text', '', 'leads')}
      ${field('a-role', 'What does it do?', 'text', '', 'finds businesses with no website')}
      <details class="f-adv">
        <summary>Advanced</summary>
        ${field('a-identity', 'Identity / mission', 'textarea', '', 'who this agent is and what it owns')}
        ${field('a-home', 'Home folder', 'text', '', 'defaults to ./<name>',
                'the agent lives and works only here; one agent per folder')}
        ${field('a-repo', 'Start on a copy of a repo', 'text', '', '/path/to/repo',
                'instead of a home folder, give it a fresh branch (git worktree) of an existing repo')}
        ${field('a-launch-cmd', 'Launch command', 'text', '', 'claude --dangerously-skip-permissions',
                'blank = default (runs unattended with permission prompts off — fine for its own isolated folder)')}
        ${field('a-launch', 'Launch it now', 'checkbox', true)}
      </details>
      <div class="f-actions"><button class="btn primary" id="a-go">Create agent</button></div>
      <div class="f-hint">A name and what it does is all you need — crew gives it its own folder, writes its identity, and launches Claude. It only ever works in that folder.</div>
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
      <div class="f-pair"><b>${esc(sourceName)}</b> <span class="arrow" id="e-arrow">→</span> <b>${esc(targetName)}</b></div>
      ${field('e-label', 'Label', 'text', '', 'qualified lead')}
      <div class="edge-dir">
        <div class="edge-dir-h">${esc(sourceName)} <span class="arrow">→</span> ${esc(targetName)}</div>
        ${condList('e-when', `When should ${esc(sourceName)} message ${esc(targetName)}?`, [], 'e.g. when a lead is qualified')}
        ${field('e-does', `What should ${esc(targetName)} do on receipt?`, 'textarea', '', 'e.g. build a one-page demo and reply with the URL')}
        ${field('e-reply', `${esc(targetName)} should reply back`, 'checkbox', false)}
      </div>
      ${field('e-undirected', 'Two-way — both can message each other', 'checkbox', false)}
      <div class="edge-dir edge-back" id="e-back-wrap">
        <div class="edge-dir-h">${esc(targetName)} <span class="arrow">→</span> ${esc(sourceName)} <span class="dim">(two-way only)</span></div>
        ${condList('e-when-back', `When should ${esc(targetName)} message ${esc(sourceName)}?`, [], 'e.g. when the demo needs changes')}
        ${field('e-does-back', `What should ${esc(sourceName)} do on receipt?`, 'textarea', '', '')}
        ${field('e-reply-back', `${esc(sourceName)} should reply back`, 'checkbox', false)}
      </div>
      ${field('e-max', 'Limit messages per hour (0 = no limit)', 'text', '0', '0',
              'rate-limits this link so a tight back-and-forth loop never runs away')}
      <div class="f-actions"><button class="btn primary" id="e-go">Connect</button></div>
      <div class="f-hint">This is the only channel that exists between them. Each direction's triggers + what the receiver does are written into both agents' identity.</div>
    `);
    wireTwoWay();
    document.getElementById('e-go').onclick = () => {
      submit(() => api.edgeCreate({
        source: sourceName, target: targetName, label: val('e-label'),
        conditions: readCondList('e-when'), target_action: val('e-does'), reply_expected: checked('e-reply'),
        back_conditions: readCondList('e-when-back'), back_action: val('e-does-back'), back_reply: checked('e-reply-back'),
        max_turns: parseInt(val('e-max'), 10) || 0,
        directed: !checked('e-undirected'),
      }), `connected ${sourceName} → ${targetName}`);
    };
  }

  // ---- edit / delete an existing edge ---- //
  function openEditEdge(edge) {
    const two = edge.directed === false;
    const S = esc(edge.source_name), T = esc(edge.target_name);
    open('Edit relationship', `
      <div class="f-pair"><b>${S}</b> <span class="arrow" id="e-arrow">${two ? '↔' : '→'}</span> <b>${T}</b></div>
      ${field('e-label', 'Label', 'text', edge.label, '')}
      <div class="edge-dir">
        <div class="edge-dir-h">${S} <span class="arrow">→</span> ${T}</div>
        ${condList('e-when', `When should ${S} message ${T}?`, edgeConds(edge, false), '')}
        ${field('e-does', `What should ${T} do on receipt?`, 'textarea', edge.target_action, '')}
        ${field('e-reply', `${T} should reply back`, 'checkbox', !!edge.reply_expected)}
      </div>
      ${field('e-undirected', 'Two-way — both can message each other', 'checkbox', two)}
      <div class="edge-dir edge-back" id="e-back-wrap">
        <div class="edge-dir-h">${T} <span class="arrow">→</span> ${S} <span class="dim">(two-way only)</span></div>
        ${condList('e-when-back', `When should ${T} message ${S}?`, edgeConds(edge, true), '')}
        ${field('e-does-back', `What should ${S} do on receipt?`, 'textarea', edge.back_action, '')}
        ${field('e-reply-back', `${S} should reply back`, 'checkbox', !!edge.back_reply)}
      </div>
      ${field('e-max', 'Limit messages per hour (0 = no limit)', 'text', String(edge.max_turns || 0), '0')}
      <div class="f-actions">
        <button class="btn danger" id="e-del">Delete</button>
        <button class="btn primary" id="e-save">Save</button>
      </div>
    `);
    wireTwoWay();
    document.getElementById('e-save').onclick = () => {
      submit(() => api.edgeUpdate({
        guid: edge._guid, label: val('e-label'),
        conditions: readCondList('e-when'), target_action: val('e-does'), reply_expected: checked('e-reply'),
        back_conditions: readCondList('e-when-back'), back_action: val('e-does-back'), back_reply: checked('e-reply-back'),
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
