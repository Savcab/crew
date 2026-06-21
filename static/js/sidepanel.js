// sidepanel.js — the right-edge slide-in side panel controller.
//
// One panel (#sidepanel) with FOUR modes, ported from the OLD monolith's
// "side panels" section (dashboard.py ~2918-3268):
//   * 'pr'   — a GitHub PR rendered NATIVELY via the gh CLI (/api/pr). GitHub
//              blocks iframe embedding (X-Frame-Options) and public CORS proxies
//              can't see private-repo auth cookies, so the only way to show a PR
//              locally is to render it from gh's JSON + unified diff.
//   * 'diff' — a worker's worktree diff via local git (/api/diff), TWO-PHASE:
//              the fast git-diff first (pr=0) then the slow PR context (pr=1)
//              patched in, with a per-target _diffCache for instant re-open.
//   * 'url'  — a Linear (or other) link in an <iframe>, with an open-in-tab
//              fallback card when the site refuses framing (frame-ancestors).
//   * 'term' — a live terminal: a TerminalPane (xterm.js) mounted into #sp-term.
//              This REPLACES the OLD snapshot-poller (spRefreshTerm/capUrl/
//              scrollKeeper/_lastHtml) entirely — xterm owns scrollback, scroll
//              position, cursor and selection natively, and live keystrokes ride
//              term.onData → /api/send inside TerminalPane.
//
// Architecture: this exports a factory `createSidePanel(ctx)` (the modular
// rewrite wires the modules together in main.js instead of leaning on globals).
// ctx = {
//   api,           // api.js — needs .getPr(url), .getDiff(target, {pr}), .ensureShell(session)
//   TerminalPane,  // term.js — the xterm-backed pane class
//   esc,           // html-escape helper (shared with the rest of the FE)
//   toast,         // toast(msg, isErr) notifier
//   getState,      // () => ({ crewSnap }) — live snapshot for the worker picker
// }
// The dashboard CHORDS that open the panel (Ctrl-G / Ctrl-T / Cmd-D / Esc) live
// in keys.js per the SPEC; they call the methods returned here. We only own the
// panel's own DOM + the width-resize handle.

export function createSidePanel(ctx) {
  const { api, TerminalPane, esc, toast, getState } = ctx;

  // Element ids are defined by the shell author (index.html); rely on them.
  const sp = document.getElementById('sidepanel');
  const spIframe = document.getElementById('sp-iframe');
  const spTerm = document.getElementById('sp-term');
  const spPick = document.getElementById('sp-pick');
  const spPr = document.getElementById('sp-pr');
  const spTitle = document.getElementById('sp-title');
  const spOpen = document.getElementById('sp-open');

  // --- panel state ---
  // spMode is the SINGLE source of truth for "which panel is showing"; every
  // async callback re-checks it so a response that lands after the panel was
  // closed or switched is dropped (the OLD `if(spMode!==…) return` guards).
  let spMode = null;
  let spLoadCheck = null;   // setTimeout handle for the iframe blank-load fallback
  // 'diff' mode bookkeeping
  let spDiffTarget = null, spDiffName = '';
  // 'term' mode: the one live TerminalPane this panel owns (xterm.js).
  let spPane = null;
  let spLive = false;

  // A real GitHub PR URL — anything else falls through to the generic url panel.
  const PR_URL_RE = /^https?:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+/;

  // ---- shared teardown helpers (every open* calls into these) ----------------

  // Tear down whatever the previous mode left running so modes don't leak state
  // into each other (the OLD code cleared spTimer/spLoadCheck on every switch).
  function _clearTimers() {
    if (spLoadCheck) { clearTimeout(spLoadCheck); spLoadCheck = null; }
  }
  // Dispose the live terminal pane (if any). Releasing it also tears down its
  // EventSource stream + tmux pipe-pane (see TerminalPane.dispose). Idempotent.
  function _disposePane() {
    if (spPane) { try { spPane.dispose(); } catch (e) {} spPane = null; }
    spLive = false;
    sp.classList.remove('live');
  }

  function close() {
    sp.classList.remove('show', 'live', 'prmode');
    spLive = false;
    spMode = null;
    _clearTimers();
    _disposePane();
    spIframe.src = 'about:blank';
    spPr.style.display = 'none';
    spPr.innerHTML = '';
  }

  // ============================ PR mode (native gh) ==========================

  function openGithubPanel(url) {
    // A real PR URL → render natively via gh. Anything else → generic url panel.
    if (url && PR_URL_RE.test(url.trim())) return openPrPanel(url.trim());
    openUrlPanel('GitHub', url, 'GitHub blocks embedding (X-Frame-Options).');
  }

  function openPrPanel(url) {
    spMode = 'pr';
    sp.classList.add('show', 'prmode');
    sp.classList.remove('live');
    _disposePane();
    spTitle.textContent = 'Pull Request';
    spPick.style.display = 'none';
    spOpen.style.display = '';
    spOpen.textContent = 'open ↗';
    spOpen.onclick = () => window.open(url, '_blank');
    _clearTimers();
    spClearFallback();
    spTerm.style.display = 'none';
    spIframe.style.display = 'none';
    spPr.style.display = 'flex';
    spPr.innerHTML = '<div class="pr-loading">Loading PR via gh…</div>';
    api.getPr(url).then(j => {
      if (spMode !== 'pr') return;               // panel was closed/switched meanwhile
      if (!j.ok) {
        spPr.innerHTML = '<div class="pr-err">' + esc(j.error || 'failed to load PR') + '</div>'
          + '<div style="text-align:center"><button class="btn primary" id="pr-open2">Open in new tab ↗</button></div>';
        const b = document.getElementById('pr-open2');
        if (b) b.onclick = () => window.open(url, '_blank');
        return;
      }
      renderPr(j);
    }).catch(() => { if (spMode === 'pr') spPr.innerHTML = '<div class="pr-err">request failed</div>'; });
  }

  function renderPr(j) {
    const m = j.meta || {};
    let stateClass = 'open', stateLabel = 'open';
    if (m.state === 'MERGED') { stateClass = 'merged'; stateLabel = 'merged'; }
    else if (m.state === 'CLOSED') { stateClass = 'closed'; stateLabel = 'closed'; }
    else if (m.isDraft) { stateClass = 'draft'; stateLabel = 'draft'; }
    // checks rollup → pass/fail/pending counts
    const checks = (m.statusCheckRollup || []);
    let pass = 0, fail = 0, pend = 0;
    checks.forEach(c => {
      const s = (c.conclusion || c.state || c.status || '').toUpperCase();
      if (['SUCCESS', 'NEUTRAL', 'SKIPPED'].includes(s)) pass++;
      else if (['FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'].includes(s)) fail++;
      else pend++;
    });
    let checksHtml = '';
    if (checks.length) {
      checksHtml = '<div class="pr-checks">'
        + (pass ? '<span class="pr-chk pass">✓ ' + pass + ' passing</span>' : '')
        + (fail ? '<span class="pr-chk fail">✕ ' + fail + ' failing</span>' : '')
        + (pend ? '<span class="pr-chk pend">● ' + pend + ' pending</span>' : '') + '</div>';
    }
    const rev = m.reviewDecision ? '<span>review: ' + esc(m.reviewDecision.toLowerCase().replace(/_/g, ' ')) + '</span>' : '';
    const head = `<div class="pr-head">
    <div class="pr-title">${esc(m.title || '')} <span class="num">#${m.number || ''}</span></div>
    <div class="pr-sub">
      <span class="pr-state ${stateClass}">${stateLabel}</span>
      <span>${esc((m.author && (m.author.login || m.author.name)) || '')}</span>
      <span>${esc(m.baseRefName || '')} ← ${esc(m.headRefName || '')}</span>
      <span class="pr-diffstat"><span class="add">+${m.additions || 0}</span> <span class="del">−${m.deletions || 0}</span> · ${m.changedFiles || 0} files</span>
      ${rev}
    </div>
    ${checksHtml}
  </div>`;
    const body = m.body ? '<div class="pr-body">' + esc(m.body) + '</div>' : '';
    const files = renderDiff(j.diff || '', m.files || []);
    const trunc = j.truncated ? '<div class="pr-trunc">⚠ diff truncated — open in tab for the full diff.</div>' : '';
    spPr.innerHTML = head + body + '<div class="pr-files">' + files + '</div>' + trunc;
    // collapse/expand a file's hunks by clicking its header
    spPr.querySelectorAll('.pr-file-h').forEach(h => h.onclick = () => {
      const hn = h.nextElementSibling; if (hn) hn.style.display = hn.style.display === 'none' ? '' : 'none';
    });
  }

  // split a unified diff into per-file blocks with line-level coloring
  function renderDiff(diff, fileMeta) {
    if (!diff) return '<div class="pr-loading">(no diff available)</div>';
    const statByPath = {}; (fileMeta || []).forEach(f => statByPath[f.path] = f);
    const lines = diff.split('\n');
    const blocks = []; let cur = null;
    const flush = () => { if (cur) blocks.push(cur); };
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      if (ln.startsWith('diff --git')) {
        flush();
        // "diff --git a/x b/y" → take the b/ path
        const mm = ln.match(/ b\/(.+)$/);
        const path = mm ? mm[1] : ln.replace('diff --git ', '');
        cur = { path, rows: [] };
      } else if (cur) {
        cur.rows.push(ln);
      }
    }
    flush();
    if (!blocks.length) return '<div class="pr-loading">(no file changes)</div>';
    return blocks.map(b => {
      const st = statByPath[b.path];
      const stat = st ? `<span class="fs"><span style="color:var(--green)">+${st.additions}</span> <span style="color:#f85149">−${st.deletions}</span></span>` : '';
      const rows = b.rows.map(r => {
        let cls = 'ctx';
        if (r.startsWith('@@')) cls = 'hh';
        else if (r.startsWith('+++') || r.startsWith('---') || r.startsWith('index ') || r.startsWith('new file') || r.startsWith('deleted file') || r.startsWith('rename ') || r.startsWith('similarity ')) cls = 'meta';
        else if (r.startsWith('+')) cls = 'add';
        else if (r.startsWith('-')) cls = 'del';
        return '<span class="dl ' + cls + '">' + esc(r || ' ') + '</span>';
      }).join('');
      return `<div class="pr-file">
      <div class="pr-file-h"><span class="fp">${esc(b.path)}</span>${stat}</div>
      <div class="pr-hunks"><pre>${rows}</pre></div>
    </div>`;
    }).join('');
  }

  // ===================== worktree diff (local git) ===========================
  // Opens the side panel and renders `git diff` of a session's worktree — what
  // the worker has changed on its branch + working tree. `target` is a session
  // name. Reuses the PR render machinery (renderDiff) for the file hunks.
  //
  // cache the last successful diff payload PER target, so re-opening Cmd-D shows
  // the previous result INSTANTLY while we revalidate in the background. Keyed by
  // the target string.
  const _diffCache = {};

  function openDiffPanel(target, name) {
    spMode = 'diff'; spDiffTarget = target; spDiffName = name || target;
    sp.classList.add('show', 'prmode');
    sp.classList.remove('live');
    _disposePane();
    spTitle.textContent = 'Diff · ' + (name || target);
    spPick.style.display = 'none';
    spOpen.style.display = '';
    spOpen.textContent = '↻ refresh';
    spOpen.onclick = () => loadDiff(true);
    _clearTimers();
    spClearFallback();
    spTerm.style.display = 'none';
    spIframe.style.display = 'none';
    spPr.style.display = 'flex';
    // INSTANT: if we rendered this target before, paint the cached result now
    // (with a subtle "refreshing" marker) so the panel never shows a blank
    // spinner on re-open.
    const cached = _diffCache[target];
    if (cached) { renderWorktreeDiff(cached); markDiffRefreshing(true); }
    else spPr.innerHTML = '<div class="pr-loading">Loading worktree diff via git…</div>';
    loadDiff(false);
  }

  // signature of a diff payload for cheap change-detection (avoid re-render flicker).
  function diffSig(j) { try { return JSON.stringify({ d: j.diff, f: j.files, p: j.pr }); } catch (e) { return Math.random() + ''; } }

  function markDiffRefreshing(on) {
    const h = spPr.querySelector('.pr-head'); if (!h) return;
    let b = h.querySelector('.pr-refreshing');
    if (on) { if (!b) { b = document.createElement('span'); b.className = 'pr-refreshing'; b.textContent = '↻ refreshing…'; h.appendChild(b); } }
    else if (b) { b.remove(); }
  }

  // Two-phase load: the FAST git diff first (pr=0, ~tens of ms) so the code shows
  // immediately, then the SLOW PR context (pr=1, the multi-second gh call) which
  // patches in CI/comments when it returns. `force` skips the cache short-circuit.
  function loadDiff(force) {
    if (spMode !== 'diff' || !spDiffTarget) return;
    const target = spDiffTarget;
    if (!_diffCache[target] || force) {
      if (force) markDiffRefreshing(true);
      else if (!_diffCache[target]) spPr.innerHTML = '<div class="pr-loading">Loading worktree diff via git…</div>';
    }
    // phase 1 — git diff only (fast)
    api.getDiff(target, { pr: 0 }).then(j => {
      if (spMode !== 'diff' || spDiffTarget !== target) return;   // switched/closed meanwhile
      if (!j.ok) { if (!_diffCache[target]) spPr.innerHTML = '<div class="pr-err">' + esc(j.error || 'diff failed') + '</div>'; return; }
      // carry over the cached PR block so phase-1 doesn't wipe the PR chip/comments
      if (_diffCache[target] && _diffCache[target].pr && !j.pr) j.pr = _diffCache[target].pr;
      const prevSig = _diffCache[target] ? diffSig(_diffCache[target]) : null;
      _diffCache[target] = j;
      if (diffSig(j) !== prevSig) renderWorktreeDiff(j);
      markDiffRefreshing(true);   // PR context still loading
      // phase 2 — PR context (slow gh); patch in when it lands
      api.getDiff(target, { pr: 1 }).then(j2 => {
        if (spMode !== 'diff' || spDiffTarget !== target) return;
        if (!j2.ok) { markDiffRefreshing(false); return; }
        const prev = _diffCache[target] ? diffSig(_diffCache[target]) : null;
        _diffCache[target] = j2;
        if (diffSig(j2) !== prev) renderWorktreeDiff(j2);
        markDiffRefreshing(false);
      }).catch(() => { markDiffRefreshing(false); });
    }).catch(() => { if (spMode === 'diff' && !_diffCache[target]) spPr.innerHTML = '<div class="pr-err">request failed</div>'; markDiffRefreshing(false); });
  }

  function renderWorktreeDiff(j) {
    const files = j.files || [];
    const adds = files.reduce((a, f) => a + (f.additions || 0), 0);
    const dels = files.reduce((a, f) => a + (f.deletions || 0), 0);
    const empty = !(j.diff || '').trim();
    const pr = j.pr || null;
    // PR link chip + CI checks + review decision (when the branch has a PR)
    let prLink = '', checksHtml = '', revHtml = '';
    if (pr && pr.url) {
      const st = pr.isDraft ? 'draft' : (pr.state || '').toLowerCase();
      prLink = `<a class="pr-link" id="diff-pr-link">🔗 PR #${pr.number || ''}${st ? ' · ' + esc(st) : ''}</a>`;
      const c = pr.checks || {};
      if ((c.pass || c.fail || c.pend)) {
        checksHtml = '<span class="pr-checks" style="margin-top:0">'
          + (c.pass ? '<span class="pr-chk pass">✓ ' + c.pass + '</span>' : '')
          + (c.fail ? '<span class="pr-chk fail">✕ ' + c.fail + '</span>' : '')
          + (c.pend ? '<span class="pr-chk pend">● ' + c.pend + '</span>' : '') + '</span>';
      }
      if (pr.reviewDecision) revHtml = '<span>review: ' + esc(pr.reviewDecision.toLowerCase().replace(/_/g, ' ')) + '</span>';
    }
    const head = `<div class="pr-head">
    <div class="pr-title">${esc(spDiffName)} <span class="num">${esc(j.branch || '')}</span></div>
    <div class="pr-sub">
      ${prLink}
      <span>vs <b>${esc(j.base || '')}</b>${j.mergebase ? ' @ ' + esc(j.mergebase) : ''}</span>
      <span class="pr-diffstat"><span class="add">+${adds}</span> <span class="del">−${dels}</span> · ${files.length} file${files.length === 1 ? '' : 's'}</span>
      ${revHtml}
      ${checksHtml}
    </div>
  </div>`;
    // conversation: PR comments + review bodies (newest-ordered by the backend).
    // Bot boilerplate (e.g. the Graphite stack comment) carries c.bot → .bot class
    // which the css dims (.pr-cmt.bot{opacity:.5}).
    let convHtml = '';
    if (pr && pr.comments && pr.comments.length) {
      const rows = pr.comments.map(c => {
        const k = (c.kind === 'review' && c.state) ? '<span class="ck ' + esc(c.state.toLowerCase()) + '">' + esc(c.state.replace(/_/g, ' ')) + '</span>' : '';
        const when = c.at ? '<span class="ct2">' + esc((c.at || '').slice(0, 10)) + '</span>' : '';
        const bodyTxt = (c.body || '').trim();
        return `<div class="pr-cmt${c.bot ? ' bot' : ''}"><span class="ca">${esc(c.author || '')}</span>${k}${when}
        ${bodyTxt ? '<div class="cb">' + esc(bodyTxt) + '</div>' : ''}</div>`;
      }).join('');
      convHtml = '<div class="pr-conv"><h5>conversation · ' + pr.comments.length + '</h5>' + rows + '</div>';
    }
    const body = empty
      ? '<div class="pr-loading">No changes vs ' + esc(j.base || 'base') + ' — clean working tree.</div>'
      : '<div class="pr-files">' + renderDiff(j.diff, files) + '</div>';
    const trunc = j.truncated ? '<div class="pr-trunc">⚠ diff truncated at the size cap.</div>' : '';
    spPr.innerHTML = head + convHtml + body + trunc;
    // clicking the PR chip opens the native PR render (Ctrl-G style), not a new tab
    const pl = document.getElementById('diff-pr-link');
    if (pl && pr && pr.url) pl.onclick = () => openPrPanel(pr.url);
    spPr.querySelectorAll('.pr-file-h').forEach(h => h.onclick = () => {
      const hn = h.nextElementSibling; if (hn) hn.style.display = hn.style.display === 'none' ? '' : 'none';
    });
  }

  // ===================== url mode (Linear iframe + fallback) ==================

  function spFillWorkerPicker(selName) {
    const ws = (getState().crewSnap.workers || []);
    spPick.innerHTML = ws.map(w => `<option value="${esc(w.name)}"${w.name === selName ? ' selected' : ''}>${esc(w.name)}</option>`).join('')
      || '<option>(no workers)</option>';
  }

  function openLinkPanel(title, url) { openUrlPanel(title, url, title + ' may block embedding.'); }

  function openUrlPanel(title, url, blockMsg) {
    spMode = 'url';
    sp.classList.add('show');
    sp.classList.remove('live', 'prmode');
    _disposePane();
    spTitle.textContent = title;
    spPick.style.display = 'none';
    spOpen.style.display = '';
    spOpen.textContent = 'open ↗';
    spOpen.onclick = () => url && window.open(url, '_blank');
    _clearTimers();
    spPr.style.display = 'none';
    spPr.innerHTML = '';
    spClearFallback();
    spTerm.style.display = 'none';
    spIframe.style.display = '';
    if (!url) { spShowFallback('No ' + title + ' link on this task yet.'); return; }
    spIframe.src = url;
    // Try to embed; if the site refuses framing (X-Frame-Options / frame-ancestors)
    // the iframe stays blank, so after a beat show an open-in-tab fallback overlay.
    if (spLoadCheck) clearTimeout(spLoadCheck);
    spLoadCheck = setTimeout(() => spShowFallback(blockMsg + ' Use “open ↗”.', url, true), 1500);
  }

  function spShowFallback(msg, url, keepIframe) {
    // overlay a card; keep iframe underneath in case it did load
    const body = document.getElementById('sp-body');
    let fb = document.getElementById('sp-fallback');
    if (!fb) { fb = document.createElement('div'); fb.id = 'sp-fallback'; body.appendChild(fb); }
    fb.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;'
      + 'align-items:center;justify-content:center;gap:12px;background:var(--bg);'
      + 'color:var(--dim);font-size:13px;text-align:center;padding:30px;';
    fb.innerHTML = `<div>${esc(msg)}</div>` + (url ? `<button class="btn primary" id="sp-fb-open">Open in new tab ↗</button>
    <div style="font-size:11px;word-break:break-all;max-width:90%">${esc(url)}</div>` : '');
    if (url) { const b = document.getElementById('sp-fb-open'); if (b) b.onclick = () => window.open(url, '_blank'); }
  }

  function spClearFallback() { const fb = document.getElementById('sp-fallback'); if (fb) fb.remove(); }

  // a real cross-origin load still fires 'load'; only blank/blocked stays empty.
  // We can't read cross-origin, so leave the fallback as a manual escape hatch.
  spIframe.addEventListener('load', () => {});

  // ===================== terminal mode (TerminalPane / xterm) =================
  // OLD mounted a <pre> and snapshot-polled it (spRefreshTerm/capUrl/scrollKeeper
  // /_lastHtml). The rewrite mounts a TerminalPane (xterm.js) into #sp-term:
  // xterm owns scrollback / scroll-position / cursor / selection NATIVELY and
  // live keystrokes ride term.onData → /api/send inside the pane. The whole
  // jitter / snap-to-top / size-thrash problem class is deleted.

  function openTermPanel(workerName) {
    spMode = 'term';
    sp.classList.add('show');
    sp.classList.remove('prmode');
    spTitle.textContent = 'Terminal';
    spOpen.style.display = 'none';
    _clearTimers();
    spClearFallback();
    spIframe.style.display = 'none';
    spPr.style.display = 'none';
    spPr.innerHTML = '';
    spTerm.style.display = '';
    spPick.style.display = '';
    spFillWorkerPicker(workerName);
    spPick.onchange = () => spStartTerm(spPick.value);
    spStartTerm(spPick.value);
  }

  async function spStartTerm(workerName) {
    const w = (getState().crewSnap.workers || []).find(x => x.name === workerName);
    const session = w ? (w.session || w.name) : workerName;
    // tear down any previous pane (its stream + pipe-pane) before opening a new one
    _disposePane();
    if (!session) { spTerm.textContent = '(no worker)'; return; }
    // ensure a shell window exists; stream THAT (fall back to the claude window).
    let target = session;
    try {
      const j = await api.ensureShell(session);
      target = (j && j.ok) ? j.target : session;
    } catch (e) { target = session; }
    if (spMode !== 'term') return;   // switched/closed while awaiting the shell
    spTerm.textContent = '';         // clear the "(no worker)" placeholder, if any
    // mount a fresh xterm pane and stream the resolved target.
    spPane = new TerminalPane();
    spPane.attach(spTerm);
    spPane.open(target);
    // become live when the user focuses/selects into the pane (matches OLD:
    // mouseup-with-no-selection → live). xterm's own onData carries keystrokes.
    spSetLive(spLive);
  }

  // toggle the LIVE marker (#sp-live "● LIVE — Esc to release") and let the pane
  // know it owns input. With xterm the keystrokes flow through the pane's onData,
  // so this is just the visual/focus affordance + a focus() poke.
  function spSetLive(on) {
    spLive = !!on;
    sp.classList.toggle('live', spLive);
    if (spPane && spPane.setLive) spPane.setLive(spLive);
    if (spLive && spPane && spPane.focus) spPane.focus();
  }

  // ===================== width-resize handle =================================
  // Drag the side panel's LEFT edge to resize its width (all modes share
  // #sidepanel). The panel is anchored to the right, so width = distance from
  // the cursor to the right edge; clamp to keep a sliver of the app visible.
  (function () {
    const handle = document.getElementById('spResize');
    let dragging = false;
    handle.addEventListener('mousedown', e => {
      dragging = true; sp.classList.add('resizing');
      document.body.style.userSelect = 'none'; e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
      if (!dragging) return;
      let w = window.innerWidth - e.clientX;
      w = Math.max(320, Math.min(window.innerWidth - 120, w));  // keep a sliver of the app visible
      sp.style.width = w + 'px';
      // let the live terminal pane re-fit to the new width as we drag (xterm's
      // fit-addon recomputes cols/rows → /api/resize; only this focused pane).
      if (spMode === 'term' && spPane && spPane.fit) spPane.fit();
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return; dragging = false;
      sp.classList.remove('resizing'); document.body.style.userSelect = '';
      if (spMode === 'term' && spPane && spPane.fit) spPane.fit();
    });
  })();

  // ---- close affordance + Esc-to-release-live (wired here so the controller is
  // self-contained; the dashboard CHORDS that OPEN the panel live in keys.js). --
  document.getElementById('sp-close').onclick = close;

  // ===================== public surface ======================================
  return {
    openGithubPanel,
    openPrPanel,
    openDiffPanel,
    openUrlPanel,
    openLinkPanel,
    openTermPanel,
    close,
    setLive: spSetLive,
    // accessors the chord dispatcher (keys.js) needs to decide what to do
    isOpen: () => sp.classList.contains('show'),
    isLive: () => spLive,
    mode: () => spMode,
  };
}
