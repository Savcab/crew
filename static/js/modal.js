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

const STATUS_LABEL = {
  working: 'working…', needs_input: 'needs you', idle: 'idle',
  unknown: 'state unknown', not_started: 'runtime not started',
  down: 'session down',
};
function statusLabel(status) {
  return STATUS_LABEL[status] || status || 'state unknown';
}

// Preserve exactly what the operator entered until the strict API boundary
// validates it. A form's visible default "0" is the deliberate no-cap value;
// deleting that value is incomplete input, not an implicit zero.
export function normalizeEdgeCapText(value) {
  return String(value == null ? '' : value).trim();
}

export function createModalController({ api, toast, refresh }) {
  toast = toast || (() => {});
  refresh = refresh || (() => {});
  const modal = document.getElementById('cmodal');
  const titleEl = document.getElementById('modalTitle');
  const bodyEl = document.getElementById('modalBody');
  const closeBtn = document.getElementById('modalClose');
  const focusableSelector = [
    'a[href]', 'button:not([disabled])', 'input:not([disabled])',
    'select:not([disabled])', 'textarea:not([disabled])', 'summary',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');
  let previousFocus = null;
  let submitInFlight = null;
  // Every open/close advances the generation. Async callbacks capture the
  // generation they belong to and must not touch a form that replaced it.
  let modalEpoch = 0;

  function isOpen() { return modal.classList.contains('show'); }
  function setBackgroundInert(on) {
    document.querySelectorAll('body > header, body > #main').forEach(el => {
      el.inert = on;
    });
  }
  function focusableControls() {
    return [...modal.querySelectorAll(focusableSelector)].filter(el =>
      !el.hidden && el.getClientRects().length > 0
      && getComputedStyle(el).visibility !== 'hidden');
  }
  function close() {
    if (!isOpen()) return;
    modalEpoch += 1;
    submitInFlight = null;
    modal.classList.remove('show');
    bodyEl.innerHTML = '';
    setBackgroundInert(false);
    const target = previousFocus;
    previousFocus = null;
    if (target && target.isConnected && typeof target.focus === 'function') {
      target.focus();
    }
  }
  if (closeBtn) closeBtn.onclick = close;
  modal.addEventListener('mousedown', e => { if (e.target === modal) close(); });
  modal.addEventListener('keydown', e => {
    if (e.key !== 'Tab' || !isOpen()) return;
    const controls = focusableControls();
    if (!controls.length) { e.preventDefault(); return; }
    const first = controls[0], last = controls[controls.length - 1];
    const outside = !modal.contains(document.activeElement);
    if (e.shiftKey && (outside || document.activeElement === first)) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (outside || document.activeElement === last)) {
      e.preventDefault(); first.focus();
    }
  });

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

  function runtimeField() {
    return `<div class="f-row"><label for="a-runtime">Runtime</label>
      <select id="a-runtime">
        <option value="claude" selected>Claude Code</option>
        <option value="codex">Codex CLI</option>
        <option value="custom">Custom command</option>
      </select>
      <div class="f-note" id="a-runtime-note">Unattended Claude; permission prompts are disabled. Native identity: CLAUDE.md</div></div>`;
  }

  function open(title, html) {
    if (!isOpen()) previousFocus = document.activeElement;
    modalEpoch += 1;
    submitInFlight = null;
    const epoch = modalEpoch;
    titleEl.textContent = title;
    bodyEl.innerHTML = html;
    modal.classList.add('show');
    setBackgroundInert(true);
    requestAnimationFrame(() => {
      if (epoch !== modalEpoch || !isOpen()) return;
      const controls = focusableControls();
      const firstBodyControl = controls.find(el => bodyEl.contains(el));
      (firstBodyControl || controls[0] || modal).focus();
    });
  }

  const val = id => (document.getElementById(id) || {}).value;
  const checked = id => !!(document.getElementById(id) || {}).checked;
  // Keep numeric text lossless until the strict server boundary validates it.
  // parseInt('1x') would silently become 1, while fallback-to-zero would turn
  // a typo or explicitly cleared field into an accepted *unlimited* cap.
  const numericText = id => normalizeEdgeCapText(val(id));
  const readEdgeCaps = () => ({
    max_turns: numericText('e-max'),
    token_cap: numericText('e-token-cap'),
    cost_cap: numericText('e-cost-cap'),
  });
  // WAVE B: prefill helpers — used to write a `/api/expand` result (or its
  // verbatim fallback) into the manual form's existing fields.
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v == null ? '' : v; };
  const setChecked = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };

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
  // WAVE B: overwrite a condition-list's rows wholesale (used to prefill from
  // an `/api/expand` result) — same shape condList() itself renders.
  const setCondList = (id, values) => {
    const el = document.getElementById(id);
    if (!el) return;
    const vals = (values && values.length) ? values : [''];
    el.innerHTML = vals.map(v => clRow(v, el.dataset.ph)).join('');
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
    if (!tog || !back) return () => {};
    const sync = () => {
      const on = tog.checked;
      back.classList.toggle('disabled', !on);
      back.querySelectorAll('input,textarea,button').forEach(el => { el.disabled = !on; });
      back.querySelectorAll('.cl-rows').forEach(r => r.classList.toggle('off', !on));
      if (arrow) arrow.textContent = on ? '↔' : '→';
    };
    tog.addEventListener('change', sync); sync();
    // returned so a WAVE B prefill (which sets `.checked` programmatically —
    // that doesn't fire 'change') can re-sync the back-direction section/arrow.
    return sync;
  }

  async function submit(fn, okMsg) {
    if (submitInFlight) return;
    const epoch = modalEpoch;
    const buttons = [...bodyEl.querySelectorAll('.f-actions button')];
    const ticket = { epoch, buttons };
    submitInFlight = ticket;
    buttons.forEach(button => { button.disabled = true; });
    const unlock = () => {
      if (submitInFlight === ticket) submitInFlight = null;
      buttons.forEach(button => {
        if (epoch === modalEpoch && button.isConnected) button.disabled = false;
      });
    };
    let r;
    try { r = await fn(); }
    catch (e) {
      const current = epoch === modalEpoch && isOpen();
      unlock();
      if (current) toast('request failed', true);
      return;
    }
    if (!r || r.ok !== true) {
      const current = epoch === modalEpoch && isOpen();
      unlock();
      if (current) toast((r && r.error) || 'failed', true);
      return;
    }
    if (submitInFlight === ticket) submitInFlight = null;
    const current = epoch === modalEpoch && isOpen();
    toast(okMsg);
    if (current) close();
    refresh(true);
  }

  // ---- + Agent ---- //
  // WAVE B: opens in BLOB MODE — one textarea + Generate, so describing an
  // agent in plain words is the default path. "fill manually instead" skips
  // straight to the form below (unchanged from before this wave); Generate
  // prefills that SAME form and opens its fold so the user always reviews/
  // edits real fields before submitting — nothing is ever created from the
  // blob text directly. The advanced inputs stay in the DOM even collapsed,
  // so the reads in #a-go's handler below always work either way.
  function openCreateAgent() {
    open('Create agent', `
      <div id="a-blob-mode">
        ${field('a-blob', 'Describe this agent in plain words', 'textarea', '',
                'e.g. finds businesses with no website and cold-emails them')}
        <div class="f-actions">
          <button class="btn primary" id="a-generate">Generate</button>
          <span id="a-gen-spinner" class="spinner" style="display:none">generating…</span>
        </div>
        <div class="f-hint"><a href="#" id="a-manual-link">fill manually instead</a></div>
      </div>
      <details class="f-adv" id="a-form-fold">
        <summary>Advanced / manual fields</summary>
        ${field('a-name', 'Name', 'text', '', 'leads')}
        ${field('a-role', 'What does it do?', 'text', '', 'finds businesses with no website')}
        ${field('a-identity', 'Identity / mission', 'textarea', '', 'who this agent is and what it owns')}
        ${field('a-home', 'Home folder', 'text', '', 'defaults under $CREW_ROOT/<project>/<name>',
                'blank uses the project-scoped Crew root; one non-overlapping home per agent')}
        ${field('a-repo', 'Start on a copy of a repo', 'text', '', '/path/to/repo',
                'instead of a home folder, give it a fresh branch (git worktree) of an existing repo')}
        ${runtimeField()}
        ${field('a-launch-cmd', 'Launch command', 'text', '', 'claude --dangerously-skip-permissions',
                'blank = default (runs unattended with permission prompts off — fine for its own isolated folder)')}
        ${field('a-launch', 'Launch it now', 'checkbox', true)}
      </details>
      <div class="f-actions"><button class="btn primary" id="a-go">Create agent</button></div>
      <div class="f-hint">A name and what it does is all you need — crew gives it its own folder, writes its identity, and launches the runtime you choose.</div>
    `);
    const syncRuntime = () => {
      const key = val('a-runtime') || 'claude';
      const cmd = document.getElementById('a-launch-cmd');
      const note = document.getElementById('a-runtime-note');
      if (!cmd || !note) return;
      const info = {
        claude: ['claude --dangerously-skip-permissions', 'Unattended Claude; permission prompts are disabled. Native identity: CLAUDE.md'],
        codex: ['codex --dangerously-bypass-approvals-and-sandbox --disable hooks', 'Unattended Codex; approvals and sandboxing are disabled. Native identity: AGENTS.md'],
        custom: ['required custom command', 'Portable identity.md only; no runtime-specific file is written automatically'],
      }[key];
      cmd.placeholder = info[0];
      cmd.required = key === 'custom';
      note.textContent = info[1];
    };
    document.getElementById('a-runtime').addEventListener('change', syncRuntime);
    syncRuntime();
    document.getElementById('a-manual-link').onclick = (e) => {
      e.preventDefault();
      document.getElementById('a-blob-mode').style.display = 'none';
      document.getElementById('a-form-fold').open = true;
    };
    document.getElementById('a-generate').onclick = async () => {
      const text = (val('a-blob') || '').trim();
      if (!text) { toast('describe the agent first', true); return; }
      const epoch = modalEpoch;
      const btn = document.getElementById('a-generate'), sp = document.getElementById('a-gen-spinner');
      btn.disabled = true; sp.style.display = '';
      let r;
      try { r = await api.expand({ kind: 'agent', text }); }
      catch (e) { r = { ok: false, fallback: { role: text, identity: text } }; }
      if (epoch !== modalEpoch || !btn.isConnected || !sp.isConnected) return;
      btn.disabled = false; sp.style.display = 'none';
      const f = (r && r.ok) ? r.fields : ((r && r.fallback) || {});
      setVal('a-name', f.name); setVal('a-role', f.role); setVal('a-identity', f.identity);
      document.getElementById('a-form-fold').open = true;
      toast(r && r.ok ? 'generated — review below' : 'could not generate — filled in your text, review below', !(r && r.ok));
    };
    document.getElementById('a-go').onclick = () => {
      const name = (val('a-name') || '').trim();
      if (!name) { toast('name required', true); return; }
      if (val('a-runtime') === 'custom' && !(val('a-launch-cmd') || '').trim()) {
        toast('custom runtime requires a launch command', true); return;
      }
      submit(() => api.agentCreate({
        name, role: val('a-role'), identity: val('a-identity'),
        home: val('a-home') || undefined, repo: val('a-repo') || undefined,
        runtime: val('a-runtime'),
        launch_cmd: val('a-launch-cmd') || undefined,
        launch: checked('a-launch'),
      }), `creating ${name}…`);
    };
  }

  // ---- connect (describe a new edge between two agents) ---- //
  // WAVE B: same blob-mode pattern as openCreateAgent above — one textarea +
  // Generate prefills this SAME manual form (inside its fold) instead of
  // creating anything directly; "fill manually instead" skips straight to it.
  function openConnect(sourceName, targetName) {
    open('Describe the relationship', `
      <div class="f-pair"><b>${esc(sourceName)}</b> <span class="arrow" id="e-arrow">→</span> <b>${esc(targetName)}</b></div>
      <div id="e-blob-mode">
        ${field('e-blob', 'Describe this relationship in plain words', 'textarea', '',
                `e.g. ${esc(sourceName)} sends qualified leads to ${esc(targetName)}, who replies with a demo link`)}
        <div class="f-actions">
          <button class="btn primary" id="e-generate">Generate</button>
          <span id="e-gen-spinner" class="spinner" style="display:none">generating…</span>
        </div>
        <div class="f-hint"><a href="#" id="e-manual-link">fill manually instead</a></div>
      </div>
      <details class="f-adv" id="e-form-fold">
        <summary>Advanced / manual fields</summary>
        ${field('e-label', 'Label', 'text', '', 'qualified lead')}
        <div class="edge-dir">
          <div class="edge-dir-h">${esc(sourceName)} <span class="arrow">→</span> ${esc(targetName)}</div>
          ${condList('e-when', `When should ${esc(sourceName)} message ${esc(targetName)}?`, [], 'e.g. when a lead is qualified')}
          ${field('e-does', `What should ${esc(targetName)} do on receipt?`, 'textarea', '', 'e.g. build a one-page demo and reply with the URL')}
          ${field('e-reply', `${esc(targetName)} should reply back`, 'checkbox', false, '',
                  'requires a Two-way relationship so the reply is authorized')}
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
        ${field('e-token-cap', 'Token budget/hr (0 = uncapped)', 'text', '0', '0',
                "refuses sends once the target's hourly token spend hits this")}
        ${field('e-cost-cap', '$ budget/hr (0 = uncapped)', 'text', '0', '0',
                "refuses sends once the target's hourly $ spend hits this")}
      </details>
      <div class="f-actions"><button class="btn primary" id="e-go">Connect</button></div>
      <div class="f-hint">This is the only channel that exists between them. Each direction's triggers + what the receiver does are written into both agents' identity.</div>
    `);
    const syncTwoWay = wireTwoWay();
    document.getElementById('e-manual-link').onclick = (e) => {
      e.preventDefault();
      document.getElementById('e-blob-mode').style.display = 'none';
      document.getElementById('e-form-fold').open = true;
    };
    document.getElementById('e-generate').onclick = async () => {
      const text = (val('e-blob') || '').trim();
      if (!text) { toast('describe the relationship first', true); return; }
      const epoch = modalEpoch;
      const btn = document.getElementById('e-generate'), sp = document.getElementById('e-gen-spinner');
      btn.disabled = true; sp.style.display = '';
      let r;
      try { r = await api.expand({ kind: 'edge', text, source: sourceName, target: targetName }); }
      catch (e) { r = { ok: false, fallback: { conditions: [text] } }; }
      if (epoch !== modalEpoch || !btn.isConnected || !sp.isConnected) return;
      btn.disabled = false; sp.style.display = 'none';
      const f = (r && r.ok) ? r.fields : ((r && r.fallback) || {});
      setVal('e-label', f.label);
      setCondList('e-when', f.conditions);
      setVal('e-does', f.target_action);
      setChecked('e-reply', f.reply_expected);
      setChecked('e-undirected', f.directed === false);
      setCondList('e-when-back', f.back_conditions);
      setVal('e-does-back', f.back_action);
      setChecked('e-reply-back', f.back_reply);
      setVal('e-max', String(f.max_turns || 0));
      setVal('e-token-cap', String(f.token_cap || 0));
      setVal('e-cost-cap', String(f.cost_cap || 0));
      syncTwoWay();   // programmatic .checked above doesn't fire 'change'
      document.getElementById('e-form-fold').open = true;
      toast(r && r.ok ? 'generated — review below' : 'could not generate — filled in your text, review below', !(r && r.ok));
    };
    document.getElementById('e-go').onclick = () => {
      if ((checked('e-reply') || checked('e-reply-back')) && !checked('e-undirected')) {
        toast('Replies require a Two-way relationship', true); return;
      }
      submit(() => api.edgeCreate({
        source: sourceName, target: targetName, label: val('e-label'),
        conditions: readCondList('e-when'), target_action: val('e-does'), reply_expected: checked('e-reply'),
        back_conditions: readCondList('e-when-back'), back_action: val('e-does-back'), back_reply: checked('e-reply-back'),
        ...readEdgeCaps(),
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
        ${field('e-reply', `${T} should reply back`, 'checkbox', !!edge.reply_expected, '',
                'requires a Two-way relationship so the reply is authorized')}
      </div>
      ${field('e-undirected', 'Two-way — both can message each other', 'checkbox', two)}
      <div class="edge-dir edge-back" id="e-back-wrap">
        <div class="edge-dir-h">${T} <span class="arrow">→</span> ${S} <span class="dim">(two-way only)</span></div>
        ${condList('e-when-back', `When should ${T} message ${S}?`, edgeConds(edge, true), '')}
        ${field('e-does-back', `What should ${S} do on receipt?`, 'textarea', edge.back_action, '')}
        ${field('e-reply-back', `${S} should reply back`, 'checkbox', !!edge.back_reply)}
      </div>
      ${field('e-max', 'Limit messages per hour (0 = no limit)', 'text', String(edge.max_turns || 0), '0')}
      ${field('e-token-cap', 'Token budget/hr (0 = uncapped)', 'text', String(edge.token_cap || 0), '0',
              "refuses sends once the target's hourly token spend hits this")}
      ${field('e-cost-cap', '$ budget/hr (0 = uncapped)', 'text', String(edge.cost_cap || 0), '0',
              "refuses sends once the target's hourly $ spend hits this")}
      ${edge.blessed === false ? `<div class="f-row"><label>Review</label>
        <div><button class="btn sm" id="e-bless">bless this edge</button>
        <span class="f-note">agent-authored change, not yet reviewed</span></div></div>` : ''}
      <div class="f-actions">
        <button class="btn danger" id="e-del">Delete</button>
        <button class="btn primary" id="e-save">Save</button>
      </div>
    `);
    wireTwoWay();
    document.getElementById('e-save').onclick = () => {
      if ((checked('e-reply') || checked('e-reply-back')) && !checked('e-undirected')) {
        toast('Replies require a Two-way relationship', true); return;
      }
      submit(() => api.edgeUpdate({
        guid: edge._guid, label: val('e-label'),
        conditions: readCondList('e-when'), target_action: val('e-does'), reply_expected: checked('e-reply'),
        back_conditions: readCondList('e-when-back'), back_action: val('e-does-back'), back_reply: checked('e-reply-back'),
        ...readEdgeCaps(),
        directed: !checked('e-undirected'),
      }), 'edge updated');
    };
    const blessBtn = document.getElementById('e-bless');
    if (blessBtn) blessBtn.onclick = () => submit(() => api.edgeBless(edge._guid), 'edge blessed');
    document.getElementById('e-del').onclick = () => {
      submit(() => api.edgeDelete({ guid: edge._guid }), 'edge deleted');
    };
  }

  // ---- identity card (read-only view of who an agent is + its channels) ---- //
  // Built entirely from the graph snapshot (agent record + edges) — the same data
  // identity.md is rendered from — so it needs no extra server call.
  const _conds = (edge, back) => {
    const k = back ? 'back_conditions' : 'conditions';
    if (Array.isArray(edge[k]) && edge[k].length) return edge[k].filter(Boolean);
    if (!back && edge.condition) return [edge.condition];
    return [];
  };
  const _condText = cs => cs.length ? cs.map(esc).join(' · ') : '<span class="dim">any time</span>';
  const _positiveNumber = value => {
    const n = Number(value || 0);
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  const _caps = e => {
    const turns = _positiveNumber(e.max_turns);
    const tokens = _positiveNumber(e.token_cap);
    const cost = _positiveNumber(e.cost_cap);
    const parts = [];
    if (turns) parts.push(`${turns.toLocaleString('en-US')} msg/hr`);
    if (tokens) parts.push(`${tokens.toLocaleString('en-US')} tok/hr`);
    if (cost) parts.push(`$${cost.toLocaleString('en-US')}/hr`);
    return parts.length
      ? ` <span class="dim">(cap: ${parts.map(esc).join(' · ')})</span>`
      : '';
  };
  const _detail = value => {
    const text = String(value == null ? '' : value).trim();
    return text
      ? `<span class="id-copy">${esc(text).replace(/\n/g, '<br>')}</span>`
      : '<span class="dim">—</span>';
  };
  const _grants = grants => {
    const rows = Array.isArray(grants) ? grants : [];
    if (!rows.length) return '<span class="dim">none</span>';
    const items = rows.map(grant => {
      const name = String((grant || {}).name || '?');
      const path = String((grant || {}).path || '?');
      const mode = String((grant || {}).mode || 'ro');
      return `<li><span class="mono">refs/${esc(name)}</span> → `
        + `<span class="mono">${esc(path)}</span> (${esc(mode)})</li>`;
    }).join('');
    return `<div><ul class="id-list">${items}</ul>`
      + '<span class="f-note">Recorded intent, not filesystem enforcement.</span></div>';
  };

  function openIdentity(worker, edges) {
    if (!worker) return;
    const name = worker.name;
    const two = e => e.directed === false;
    // outgoing: peers THIS agent may message (forward when it's the source; the back
    // direction of an undirected edge when it's the target).
    const out = [], inc = [];
    for (const e of (edges || [])) {
      if (e.source_name === name) {
        out.push({ peer: e.target_name, conds: _conds(e, false), reply: !!e.reply_expected, e });
        inc.push({ peer: e.target_name, conds: _conds(e, true), act: e.back_action, when: two(e), back: true, e });
      }
      if (e.target_name === name) {
        inc.push({ peer: e.source_name, conds: _conds(e, false), act: e.target_action, when: true, e });
        if (two(e)) out.push({ peer: e.source_name, conds: _conds(e, true), reply: !!e.back_reply, e });
      }
    }
    const outRows = out.filter(r => r.when !== false).map(r =>
      `<li><b>${esc(r.peer)}</b> — ${_condText(r.conds)}${_caps(r.e)}${r.reply ? ' <span class="dim">· reply expected</span>' : ''}</li>`).join('');
    const incRows = inc.filter(r => r.when).map(r =>
      `<li><b>${esc(r.peer)}</b>${r.conds.length ? ' — ' + _condText(r.conds) : ''}${r.act ? `<div class="dim">you: ${esc(r.act)}</div>` : ''}</li>`).join('');
    const st = worker.live_status || (worker.session_alive ? 'unknown' : 'down');
    const isBlessed = worker.blessed !== false;
    const isForeman = !!worker.can_edit_graph;
    const blessedRow = isBlessed
      ? `<span class="dim">yes</span>`
      : `<button class="btn sm" id="id-bless">bless</button> <span class="f-note" style="display:inline">agent-authored, not yet reviewed</span>`;
    const foremanRow = `${isForeman ? '<span class="dim">yes</span>' : '<span class="dim">no</span>'} `
      + `<button class="btn sm" id="id-foreman-toggle">${isForeman ? 'revoke foreman' : 'make foreman'}</button>`;
    open(`${name} — identity`, `
      <div class="id-card">
        <div class="id-row"><span class="id-k">role</span><span>${esc(worker.role) || '<span class="dim">—</span>'}</span></div>
        <div class="id-row"><span class="id-k">identity / mission</span>${_detail(worker.identity)}</div>
        <div class="id-row"><span class="id-k">notes</span>${_detail(worker.notes)}</div>
        <div class="id-row"><span class="id-k">file grants</span>${_grants(worker.grants)}</div>
        <div class="id-row"><span class="id-k">home</span><span class="mono">${esc(worker.home) || '<span class="dim">—</span>'}</span></div>
        <div class="id-row"><span class="id-k">runtime</span><span>${esc(worker.runtime || 'claude')}</span></div>
        <div class="id-row"><span class="id-k">status</span><span>${esc(statusLabel(st))}</span></div>
        <div class="id-row"><span class="id-k">blessed</span><span>${blessedRow}</span></div>
        <div class="id-row"><span class="id-k">foreman</span><span>${foremanRow}</span></div>
        <div class="id-sec">talks to <span class="arrow">→</span></div>
        <ul class="id-list">${outRows || '<li class="dim">no one yet — drag an edge from this node to connect it</li>'}</ul>
        <div class="id-sec">hears from <span class="arrow">←</span></div>
        <ul class="id-list">${incRows || '<li class="dim">no one yet</li>'}</ul>
      </div>
      <div class="f-hint">This mirrors the agent's identity.md. Edit a channel by clicking its edge in the graph.</div>
    `);
    const blessBtn = document.getElementById('id-bless');
    if (blessBtn) blessBtn.onclick = () => submit(() => api.agentBless(name), `blessed ${name}`);
    document.getElementById('id-foreman-toggle').onclick = () => submit(
      () => api.agentForeman({ name, revoke: isForeman }),
      isForeman ? `revoked foreman from ${name}` : `${name} is now foreman`);
  }

  // ---- WAVE 4: pending-approval tray ---- //
  function ageText(createdAt) {
    if (!createdAt) return '?';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - createdAt));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86400)}d`;
  }

  function pendingRowHtml(r) {
    const state = r.result || 'pending';
    const actionable = state === 'pending';
    let attention = '';
    if (state === 'applying') {
      attention = 'Reconciliation/manual review required: the mutation may have started. Do not replay it blindly.';
    } else if (state === 'approval_failed') {
      attention = `${r.reason || 'Approval failed without a stored reason.'} Manual review is required before recovery or retry.`;
    }
    return `<div class="pend-row" data-guid="${esc(r._guid)}">
      <div class="pend-main">
        <div><b>${esc(r.actor)}</b> <span class="dim">${esc(r.op)}</span>
          <span class="pend-state ${esc(state)}">${esc(state)}</span></div>
        <div class="pend-summary">${esc(r.summary || '')}</div>
        ${attention ? `<div class="pend-attention">${esc(attention)}</div>` : ''}
        <div class="dim" style="font-size:11px">${esc(ageText(r.created_at))} ago</div>
      </div>
      <div class="pend-actions">
        ${actionable ? `<button class="btn sm primary pend-approve">approve</button>
        <button class="btn sm danger pend-reject">reject</button>` : ''}
      </div>
    </div>`;
  }

  function openPending(rows) {
    const list = rows || [];
    const body = list.length
      ? `<div class="pend-list">${list.map(pendingRowHtml).join('')}</div>`
      : '<div class="empty">no pending requests</div>';
    open('Approval attention', body);
    bodyEl.querySelectorAll('.pend-row').forEach(row => {
      const guid = row.dataset.guid;
      const approveBtn = row.querySelector('.pend-approve');
      const rejectBtn = row.querySelector('.pend-reject');
      if (approveBtn) approveBtn.onclick = () =>
        submit(() => api.pendingApprove(guid), 'approved');
      if (rejectBtn) rejectBtn.onclick = () => {
        const reason = window.prompt('reason for rejecting (optional):', '') || '';
        submit(() => api.pendingReject(guid, reason), 'rejected');
      };
    });
  }

  return {
    isOpen, closeModal: close, openCreateAgent, openConnect, openEditEdge,
    openIdentity, openPending,
  };
}
