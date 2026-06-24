// term.js — ONE terminal pane = one xterm.js Terminal bound to a real tmux client.
//
// TRANSPORT = PTY-ATTACH (the correct model; proven in ng/spike_pty.py). The
// server runs a REAL `tmux attach` inside a pseudo-terminal (PTY) — exactly what
// `tmux attach` in a terminal does, and how ttyd/gotty/wetty work. tmux treats our
// PTY as a CLIENT: it sizes the window to the PTY (TIOCSWINSZ), renders the pane
// into it with full escapes, and reflows on resize — all natively. We just pipe
// bytes both ways:
//   GET  /api/pty/stream?t=&cols=&rows=  → SSE: `id` event (PTY id) then `data`
//        events (raw PTY output, base64) → term.write.
//   POST /api/pty/input  {id, b64}       ← keystrokes / MOUSE / chord escapes.
//   POST /api/pty/resize {id, cols,rows} ← grid → TIOCSWINSZ → tmux window follows.
// This DELETES the entire scrape-and-reconstruct era (capture-pane snapshots,
// pipe-pane, ansi→html, scrollKeeper, fit_session, seed/size events, the passive
// font-shrink model) — and with it the whole bug class: scatter, frozen-wide
// scrollback, letterbox, size races. tmux owns rendering/sizing/scroll/mouse.
//
// MOUSE: when tmux/Claude enables mouse mode, xterm emits the SGR mouse sequences
// through onData/onBinary → forwarded to the PTY → drag/scroll/click just work.

import { api } from './api.js';

// Vendored xterm.js + addon-fit are UMD bundles on window (loaded by <script> in
// index.html before this module). The fit-addon sizes the GRID to the box; we push
// that to the PTY so tmux resizes the window to match (grid follows the box).
const Terminal = window.Terminal;
const FitAddon = window.FitAddon;

// base64 (the SSE wire format) → Uint8Array of raw PTY bytes; hand straight to
// term.write so xterm's own UTF-8 + escape parser decodes it.
const dec = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
// UTF-8 encode a JS string → Uint8Array (onData strings may contain multibyte).
const _enc = new TextEncoder();
const _utf8 = s => _enc.encode(s);
// Uint8Array → base64 (Latin-1 path; bytes are already 0..255).
const _b64 = u8 => { let s = ''; for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]); return btoa(s); };

export class TerminalPane {
  constructor(opts = {}) {
    // Match the spike's Terminal options so the rendering is identical to the
    // proven transport. scrollback is xterm's native ring buffer (replaces the
    // old 2000-line capture-pane history-mode dance). convertEol:false because
    // the pane stream already carries real CRs/LFs — letting xterm translate
    // would double-space Claude's TUI.
    this.baseFont = opts.fontSize || 12.5;
    this.term = new Terminal(Object.assign({
      fontSize: this.baseFont,
      theme: { background: '#010409' },
      scrollback: 5000,
      convertEol: false,
      cursorBlink: true,
    }, opts));
    // fit-addon sizes the GRID to the host box; we push that size to the PTY
    // (→ tmux resizes the window). The PTY model wants the grid to follow the box.
    this.fitAddon = new FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);

    this.target = null;     // session NAME (resolved to a live pane server-side).
    this.es = null;         // the live EventSource (PTY output), or null when closed.
    this.ptyId = null;      // server-side PTY id for THIS stream (input/resize routing).
    this.host = null;       // the element we were attached to (gets .live class).
    this._ro = null;        // ResizeObserver → fit FONT + push resize to the PTY.
    this._cols = 0; this._rows = 0;   // last grid we told the PTY.

    // ALL live input → raw bytes to the PTY (api.ptyInput). A real tmux client in a
    // PTY means xterm's own encoders produce the correct bytes for typing, ctrl-
    // combos, arrows, AND mouse (when the app enables mouse mode, xterm emits the
    // SGR mouse sequences here). onData = UTF-8 text; onBinary = raw single-bytes.
    this.term.onData(s => this._toPty(_utf8(s)));
    this.term.onBinary(s => { const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i) & 255; this._toPty(u); });

    // macOS editing chords + Shift+Enter that xterm does NOT emit on its own → write
    // the equivalent terminal byte sequences straight to the PTY. (With the PTY
    // transport these are real escape bytes, not tmux named keys.)
    this.term.attachCustomKeyEventHandler(e => {
      if (e.type !== 'keydown') return true;
      const bytes = this._chordBytes(e);
      if (!bytes) return true;                       // not ours → let xterm emit normally
      e.preventDefault();
      this._toPty(_utf8(bytes));
      return false;
    });
  }

  // Map a macOS editing chord / Shift+Enter → the raw terminal escape string to
  // send to the PTY. Returns null for anything we don't claim. Sequences are the
  // standard xterm/readline ones the shell + Claude TUI understand:
  //   Cmd+←/→ = Home/End (\e[H, \e[F)   Cmd+⌫ = C-u (\x15)   Cmd+⌦ = C-k (\x0b)
  //   Opt+←/→ = word back/fwd (\eb,\ef) Opt+⌫ = C-w (\x17)   Shift+Enter = \e\r (Meta+Enter)
  _chordBytes(e) {
    const k = e.key;
    if (e.metaKey && !e.ctrlKey && !e.altKey) {
      if (k === 'ArrowLeft') return '\x1b[H';
      if (k === 'ArrowRight') return '\x1b[F';
      if (k === 'Backspace') return '\x15';   // C-u kill to line start
      if (k === 'Delete') return '\x0b';       // C-k kill to line end
    }
    if (e.altKey && !e.metaKey && !e.ctrlKey) {
      if (k === 'ArrowLeft') return '\x1bb';   // M-b word back
      if (k === 'ArrowRight') return '\x1bf';  // M-f word fwd
      if (k === 'Backspace') return '\x17';    // C-w delete word
    }
    if (k === 'Enter' && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      return '\x1b\r';                          // Meta+Enter = newline-in-prompt
    }
    return null;
  }

  // POST raw bytes to this pane's PTY (keystrokes / mouse / chord escapes).
  _toPty(u8) {
    if (!this.ptyId || !u8 || !u8.length) return;
    api.ptyInput(this.ptyId, _b64(u8));
  }

  // attach(el): mount the terminal. The ResizeObserver fits the FONT to the box
  // AND pushes the new (cols,rows) to the PTY (TIOCSWINSZ → tmux resizes the window
  // to match xterm — the real-client behavior). Debounced so a drag-resize doesn't
  // spam resizes mid-drag.
  attach(el) {
    this.host = el;
    this.term.open(el);
    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(() => {
        if (this._fitT) clearTimeout(this._fitT);
        this._fitT = setTimeout(() => this._fitAndPush(), 60);
      });
      this._ro.observe(el);
    }
    return this;
  }

  // open(target): point this pane at a tmux session and start a PTY-ATTACH stream.
  // The server spawns a real `tmux attach` client in a PTY (grouped session so it
  // doesn't yank the user's real terminal's window) and streams its raw output.
  // We compute our grid from the box, hand it to the stream so the PTY starts at
  // our size, capture the server-side PTY id (first `id` event) for input/resize,
  // then write every output `data` event straight to xterm. No seed/size/scroll
  // hacks — tmux renders, sizes, reflows, and scrolls natively through the PTY.
  open(target) {
    this._closeStream();
    this.target = target;
    this.ptyId = null;
    this.term.reset();
    if (!target) return this;
    // fit the font + compute the grid for THIS box before connecting, so the PTY
    // is created at the right size (avoids an initial wrong-size paint+resize).
    this._fitFont();
    const c = this.term.cols, r = this.term.rows;
    this._cols = c; this._rows = r;
    this.es = new EventSource('/api/pty/stream?t=' + encodeURIComponent(target) + '&cols=' + c + '&rows=' + r);
    // first event: the server-side PTY id → enables input/resize routing.
    // on the id event the box has had a moment to lay out; RE-FIT (don't trust the
    // open()-time grid, which may have been measured before the dock un-hid) and
    // force-push the real size so the PTY/tmux window matches xterm exactly. A
    // couple of delayed re-fits catch any late reflow (display:none→flex settle).
    this.es.addEventListener('id', e => {
      this.ptyId = String(e.data);
      // re-fit once at the now-laid-out box and push the REAL size, so the PTY/tmux
      // window matches xterm exactly. (No speculative delayed re-fits — they raced
      // and pushed a size xterm then settled away from → window≠grid. The
      // ResizeObserver in attach() handles any genuine later reflow.)
      this._fitAndPush(true);
    });
    // output: raw PTY bytes (base64) → xterm. xterm owns scrollback/cursor/reflow.
    // After writing, pin to the BOTTOM unless the user has scrolled up to read — a
    // fresh `tmux attach` dumps the screen and xterm can land mid-scrollback, hiding
    // the live prompt/input bar (the symptom Felix saw). We only auto-scroll when
    // already near the bottom (within 2 rows) so we never yank the user out of
    // scrollback they're actively reading.
    this.es.addEventListener('data', e => {
      const nearBottom = (this.term.buffer.active.viewportY >= this.term.buffer.active.baseY - 2);
      this.term.write(dec(e.data), () => { if (nearBottom) this.term.scrollToBottom(); });
    });
    this.es.onerror = () => { /* EventSource auto-reconnects on transient errors */ };
    return this;
  }

  // push the current grid to the PTY (TIOCSWINSZ → tmux window follows).
  _pushResize() {
    if (this.ptyId && this._cols > 0 && this._rows > 0) {
      api.ptyResize(this.ptyId, this._cols, this._rows);
    }
  }

  // fit the grid to the box, then push the (post-fit) size to the PTY. We ALWAYS
  // read this.term.cols/rows AFTER fitAddon.fit() so the size we push == the size
  // xterm actually settled at (pushing a pre-fit guess made window≠grid). `force`
  // pushes even when unchanged (the id-event needs an authoritative first push).
  _fitAndPush(force) {
    this._fitFont();
    const changed = (this.term.cols !== this._cols || this.term.rows !== this._rows);
    this._cols = this.term.cols; this._rows = this.term.rows;
    if (changed || force) this._pushResize();
  }

  // _fitFont(): size the xterm GRID to fill the host box at baseFont, via the
  // fit-addon. In the PTY model the grid is DRIVEN BY THE BOX (we then push it to
  // the PTY → tmux resizes the window to match), the opposite of the old passive
  // model. The fit-addon recomputes cols/rows for the current box + font and calls
  // term.resize(); _fitAndPush() then forwards the new grid to the PTY.
  _fitFont() {
    if (!this.host || this.host.clientWidth < 2 || this.host.clientHeight < 2) return;
    try { this.fitAddon.fit(); } catch (e) { /* not laid out yet */ }
  }

  // setLive(on): toggle "live" (keystrokes flow to the pane). focus/blur drives
  // where keystrokes go (xterm.onData fires only while focused); the .live class
  // lets CSS show the focused-pane affordance (green border overlay).
  setLive(on) {
    if (this.host) this.host.classList.toggle('live', on);
    if (on) this.term.focus();
    else this.term.blur();
    return this;
  }

  // fit(): re-fit the grid to the box and push the new size to the PTY (dock drag-
  // resize calls this). The PTY → tmux resizes the window to match.
  fit() { this._fitAndPush(); return this; }

  // dispose(): tear everything down. Close the stream (dropping the SSE socket;
  // the server's heartbeat write then raises → ptyio.close() kills the grouped
  // view session), stop observing resize, and destroy the xterm instance + its
  // DOM. After this the pane is dead; construct a new one to reuse the slot.
  dispose() {
    this._closeStream();
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this._fitT) { clearTimeout(this._fitT); this._fitT = null; }
    this.term.dispose();
    this.host = null;
  }

  // Close the current EventSource if any. Dropping the socket is what the server
  // detects (its next heartbeat write raises) → it kills the grouped view session.
  _closeStream() {
    if (this.es) { this.es.close(); this.es = null; }
  }
}
