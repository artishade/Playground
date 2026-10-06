/**
 * Agent_Linux terminal console — the standalone workspace client · skin v2.
 *
 * One page for a terminal that may be hosted with nothing else: the shell tabs
 * on the left, Agentbox on the right, providers manageable in place. It talks
 * to the same contract the dashboard uses (`terminal/api.py`) under the host's
 * own `/terminal/pty` prefix, plus `/agent` for the AI side.
 *
 * Two rendering paths on purpose: xterm.js when the CDN is reachable (real
 * emulation — colour, cursor, vim, top), and a plain append-only viewer when it
 * is not, because a terminal that renders nothing the moment jsdelivr is
 * blocked is not a terminal. Both accept the same keystrokes.
 *
 * v2 additions: boot splash, command palette (Ctrl+K), draggable agent-pane
 * resizer with a persisted width, statusbar, typing indicator, richer agent
 * messages and code blocks. The UI layer stays dependency-free: no framework,
 * no build step, one file.
 */
(function (global) {
  'use strict';

  const API = '/terminal/pty';
  const AGENT = '/agent';
  const THEMES = ['obsidian', 'plasma', 'matrix', 'glacier', 'ember'];
  // xterm cannot read CSS variables, so the terminal palette is mirrored here.
  const TERM_THEMES = {
    obsidian: { background: '#05060c', foreground: '#cbd5e1', cursor: '#7c5cff', selectionBackground: '#7c5cff40' },
    plasma:   { background: '#0a0510', foreground: '#e6d9f5', cursor: '#ff3ea5', selectionBackground: '#ff3ea540' },
    matrix:   { background: '#040a07', foreground: '#c8f7d6', cursor: '#3ee07f', selectionBackground: '#3ee07f40' },
    glacier:  { background: '#040810', foreground: '#cfe3ff', cursor: '#38bdf8', selectionBackground: '#38bdf840' },
    ember:    { background: '#0c0705', foreground: '#f6ddd0', cursor: '#fb923c', selectionBackground: '#fb923c40' },
  };

  const state = {
    token: '',
    sessions: [],
    active: null,
    offset: 0,
    stream: null,
    cols: 120,
    rows: 32,
    history: [],
    providers: [],
    provider: '',
    pending: null,
    max: 8,
    theme: 'obsidian',
    retry: 0,
    online: null,
  };

  // ---- tiny DOM helper -----------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const on = (el, name, fn) => el && el.addEventListener(name, fn);
  const store = {
    get(key, fallback) {
      try { return global.localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
    },
    set(key, value) {
      try { global.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    },
  };

  // ---- toasts --------------------------------------------------------------

  function toast(text, cls) {
    const box = $('toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = 'toast ' + (cls || '');
    el.textContent = String(text == null ? '' : text);
    box.appendChild(el);
    setTimeout(() => el.remove(), cls === 'err' ? 7000 : 3800);
  }

  function setLink(ok, text) {
    const dot = $('dot');
    const label = $('linkText');
    if (dot) {
      dot.classList.toggle('bad', ok === false);
      dot.classList.toggle('warn', ok === null);
      dot.classList.toggle('pulse', ok !== true);
    }
    if (label && text) label.textContent = text;
  }

  function setAgentOrb(ok) {
    const orb = $('adot');
    if (!orb) return;
    orb.classList.toggle('bad', ok === false);   // harmless if unsupported
    orb.classList.toggle('on', ok === true);
  }

  function say(who, text, cls) {
    const box = $('log');
    if (!box) return null;
    const el = document.createElement('div');
    el.className = 'msg ' + (cls || '');
    const label = document.createElement('span');
    label.className = 'who';
    label.textContent = who;
    el.appendChild(label);
    const body = document.createElement('span');
    body.className = 'body';
    body.innerHTML = cls === 'bot' || cls === 'you' ? renderLite(text) : escapeHtml(text);
    el.appendChild(body);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return el;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /**
   * Markdown-lite: fenced code blocks and inline `code`, everything else
   * escaped verbatim. A full parser would be a dependency; this is the 95%
   * that actually shows up in agent replies.
   */
  function renderLite(text) {
    const raw = String(text == null ? '' : text);
    const parts = raw.split(/```/);
    let html = '';
    parts.forEach((chunk, i) => {
      if (i % 2 === 1) {
        html += '<pre class="codeblock">' + escapeHtml(chunk.replace(/^\w*\n/, '')) + '</pre>';
      } else {
        html += escapeHtml(chunk).replace(/`([^`\n]+)`/g, '<code>$1</code>');
      }
    });
    return html;
  }

  /** A live "working" bubble with breathing dots; returns a remove() fn. */
  function sayThinking() {
    const box = $('log');
    if (!box) return () => {};
    const el = document.createElement('div');
    el.className = 'msg bot';
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = 'AGENTBOX · working';
    const body = document.createElement('span');
    body.className = 'body';
    body.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
    el.append(who, body);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return () => el.remove();
  }

  /**
   * A visible reasoning card: what the agent is doing right now, a live tool
   * trace that grows as steps land, and a timer. Returns handles to steer it.
   */
  function sayReasoning(label) {
    const box = $('log');
    if (!box) return { el: null, remove() {}, step() {}, done() {} };
    const el = document.createElement('div');
    el.className = 'msg reasoning';
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = 'AGENTBOX · reasoning';
    const status = document.createElement('div');
    status.className = 'think-status';
    const line = document.createElement('span');
    line.className = 'think-line';
    line.textContent = label || 'analysing the task…';
    const timer = document.createElement('span');
    timer.className = 'tagx';
    timer.textContent = '0.0s';
    const t0 = Date.now();
    const tick = setInterval(() => { timer.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's'; }, 100);
    status.append(line, timer);
    const trace = document.createElement('div');
    trace.className = 'think-trace';
    el.append(who, status, trace);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return {
      el,
      /** Log one tool step into the live trace. */
      step(stepObj) {
        trace.appendChild(stepCard(stepObj));
        line.textContent = stepObj.tool === 'run_command' && stepObj.args && stepObj.args.command
          ? `ran: ${String(stepObj.args.command).slice(0, 80)}`
          : `called ${stepObj.tool || 'a tool'}`;
        box.scrollTop = box.scrollHeight;
      },
      /** Freeze the timer and mark the card resolved. */
      done(ok) {
        clearInterval(tick);
        el.classList.add('done');
        line.textContent = ok === false ? 'ran into a problem — see below' : 'done — assembling the answer';
        box.scrollTop = box.scrollHeight;
      },
      remove() {
        clearInterval(tick);
        el.remove();
      },
    };
  }

  // ---- transport -----------------------------------------------------------

  function headers(extra) {
    const out = Object.assign({ 'content-type': 'application/json' }, extra || {});
    if (state.token) out['X-Nova-Terminal-Token'] = state.token;
    return out;
  }

  async function api(path, options) {
    const opts = Object.assign({}, options || {});
    opts.headers = headers(opts.headers);
    let res = await fetch(path, opts);
    if (res.status === 401 && !state.pending) {
      const token = askToken();                 // the host gates root shells
      if (token) {
        state.token = token;
        opts.headers = headers();
        res = await fetch(path, opts);
      }
    }
    return res;
  }

  async function json(path, options) {
    const res = await api(path, options);
    const body = await res.json().catch(() => ({}));
    return { status: res.status, body };
  }

  function askToken() {
    if (typeof prompt !== 'function') return '';
    const token = prompt('This terminal host requires its shared secret\n(NOVA_TERMINAL_TOKEN):', state.token || '');
    if (token) store.set('agent_linux_token', token);
    return token || '';
  }

  function rememberToken() {
    state.token = store.get('agent_linux_token', '');
  }

  // ---- theme ---------------------------------------------------------------

  function applyTheme(name) {
    state.theme = THEMES.indexOf(name) >= 0 ? name : 'obsidian';
    document.documentElement.setAttribute('data-theme', state.theme);
    const pick = $('theme');
    if (pick) pick.value = state.theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', TERM_THEMES[state.theme].background);
    if (screen.term && screen.term.options) {
      screen.term.options.theme = TERM_THEMES[state.theme];
      screen.term.refresh && screen.term.refresh(0, state.rows);
    }
    store.set('agent_linux_theme', state.theme);
  }

  // ---- the shell screen ----------------------------------------------------

  const screen = {
    term: null,
    plain: null,
    write(data) {
      if (this.term) { this.term.write(data); return; }
      if (this.plain) { this.plain.textContent += data; this.plain.scrollTop = this.plain.scrollHeight; }
    },
    resize(cols, rows) {
      state.cols = cols;
      state.rows = rows;
      if (this.term && this.term.resize) this.term.resize(cols, rows);
    },
    clear() {
      if (this.term && this.term.clear) this.term.clear();
      if (this.plain) this.plain.textContent = '';
    },
    focus() { if (this.term && this.term.focus) this.term.focus(); },
    fit() { if (this.term && this.term._fit) this.term._fit.fit(); },
  };

  function buildTerminal() {
    const host = $('screen');
    if (!host) return;
    const TerminalCtor = global.Terminal;
    if (TerminalCtor) {
      screen.term = new TerminalCtor({
        convertEol: false,
        cursorBlink: true,
        cursorStyle: 'bar',
        fontSize: 13,
        lineHeight: 1.25,
        fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        scrollback: 5000,
        allowProposedApi: true,
        theme: TERM_THEMES[state.theme],
      });
      screen.term.open(host);
      if (global.FitAddon) {
        screen.term._fit = new global.FitAddon.FitAddon();
        screen.term.loadAddon(screen.term._fit);
        screen.term._fit.fit();
      }
      screen.term.onData((data) => send(data));
    } else {
      // No CDN: still a usable terminal, just without emulation.
      const note = document.createElement('div');
      note.className = 'screen-note';
      note.textContent = 'xterm.js unavailable — plain viewer (no colour, no full-screen apps)';
      host.appendChild(note);
      const pre = document.createElement('pre');
      pre.className = 'plain';
      pre.setAttribute('aria-label', 'terminal output');
      host.appendChild(pre);
      screen.plain = pre;
      host.setAttribute('tabindex', '0');
      // Named keys first: `Enter`.length is 5, so a printable-char test would
      // silently drop the most important key on the keyboard.
      const NAMED = {
        Enter: '\r', Backspace: '\x7f', Tab: '\t', Escape: '\x1b',
        ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D',
        Home: '\x1b[H', End: '\x1b[F', PageUp: '\x1b[5~', PageDown: '\x1b[6~',
      };
      const capture = (e) => {
        let data = null;
        if (NAMED[e.key]) data = NAMED[e.key];
        else if (e.ctrlKey && e.key.length === 1) data = String.fromCharCode(e.key.toUpperCase().charCodeAt(0) - 64);
        else if (e.key.length === 1) data = e.key;
        if (data === null) return;
        e.preventDefault();
        send(data);
      };
      on(host, 'keydown', capture);
    }
    postResize();
  }

  function throttle(fn, wait) {
    let timer = null;
    return function () {
      if (timer) return;
      timer = setTimeout(() => { timer = null; fn(); }, wait || 120);
    };
  }

  const postResize = throttle(() => {
    if (!state.active) return;
    api(`${API}/resize`, {
      method: 'POST',
      body: JSON.stringify({ session: state.active, cols: state.cols, rows: state.rows }),
    }).catch(() => {});
  }, 150);

  function send(data) {
    if (!state.active) return;
    api(`${API}/input`, { method: 'POST', body: JSON.stringify({ session: state.active, data }) })
      .catch(() => {});
  }

  // ---- sessions ------------------------------------------------------------

  async function loadSessions() {
    let status, body;
    try {
      ({ status, body } = await json(`${API}/sessions`));
    } catch (err) {
      setLink(false, 'offline');
      return;
    }
    if (status === 401) { setLink(false, 'locked'); return; }
    if (status !== 200) { setLink(false, `http ${status}`); return; }
    setLink(true, 'live');
    state.retry = 0;
    state.sessions = (body && body.sessions) || [];
    state.max = (body && body.max_sessions) || state.max;
    renderTabs();
    const live = state.sessions.filter((s) => !s.closed);
    if (!state.active || !live.some((s) => s.id === state.active)) {
      const preferred = live.find((s) => s.active) || live[0];
      if (preferred) select(preferred.id);
    } else {
      const focused = live.find((s) => s.active);
      if (focused && focused.id !== state.active) select(focused.id);
    }
  }

  function renderTabs() {
    const box = $('tabs');
    if (!box) return;
    box.textContent = '';
    state.sessions.forEach((s) => {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'tab' + (s.id === state.active ? ' active' : '') + (s.closed ? ' closed' : '');
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(s.id === state.active));

      const name = document.createElement('span');
      name.textContent = s.label || s.id;
      tab.appendChild(name);

      const cwd = document.createElement('em');
      cwd.textContent = s.cwd || '';
      tab.appendChild(cwd);

      const close = document.createElement('span');
      close.className = 'x';
      close.textContent = '×';
      close.setAttribute('role', 'button');
      close.setAttribute('aria-label', 'close ' + (s.label || s.id));
      close.onclick = (e) => { e.stopPropagation(); closeSession(s.id); };
      tab.appendChild(close);

      tab.onclick = () => {
        if (s.closed) { toast(`“${s.label || s.id}” already ended`, 'err'); return; }
        api(`${API}/activate`, { method: 'POST', body: JSON.stringify({ session: s.id }) }).catch(() => {});
        select(s.id);
      };
      tab.ondblclick = () => renameSession(s);
      box.appendChild(tab);
    });
    const count = $('count');
    if (count) count.textContent = `${state.sessions.length}/${state.max || 8}`;
    // Disable rather than warn: a cap you cannot cross is clearer than a toast
    // that repeats on every poll.
    const newBtn = $('new');
    if (newBtn) newBtn.disabled = state.sessions.length >= (state.max || 8);
  }

  function updateStatusbar() {
    const here = state.sessions.find((s) => s.id === state.active);
    const left = $('sbSession');
    if (left) {
      left.textContent = here
        ? `${here.label || here.id}${here.cwd ? ' · ' + here.cwd : ''}`
        : 'no shell focused';
    }
    const prov = $('sbProv');
    if (prov) prov.textContent = state.provider ? 'agent: ' + state.provider : 'agent: —';
    const lat = $('sbLat');
    const top = $('latency');
    if (lat && top) lat.textContent = top.textContent;
  }

  function select(id) {
    if (!id) return;
    state.active = id;
    state.offset = 0;
    screen.clear();
    renderTabs();
    attach();
    screen.focus();
    const here = state.sessions.find((s) => s.id === id);
    const hint = $('where');
    if (hint) hint.textContent = here ? (here.cwd || '') : '';
    renderEmptyState();
    updateStatusbar();
  }

  function renderEmptyState() {
    const host = $('screen');
    if (!host) return;
    const has = state.sessions.some((s) => !s.closed);
    let box = host.querySelector('.empty');
    if (has) { if (box) box.remove(); return; }
    if (box) return;
    box = document.createElement('div');
    box.className = 'empty';
    box.innerHTML = '<div class="box"><h2>NO SHELL OPEN</h2>' +
      '<p>Open a root shell to start working — it stays alive while you are away, and every agent command runs in one you can watch.</p>' +
      '<button type="button" class="primary" id="emptyNew">+ open a shell</button></div>';
    host.appendChild(box);
    on($('emptyNew'), 'click', newSession);
  }

  function attach() {
    if (state.stream && state.stream.close) state.stream.close();
    state.stream = null;
    if (!state.active) return;
    const source = new EventSource(`${API}/stream?session=${encodeURIComponent(state.active)}&offset=${state.offset}`);
    state.stream = source;
    source.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch (err) { return; }
      onChunk(data);
    };
    source.onerror = () => {
      source.close();
      if (!state.active) return;
      // The host restarts shells and the network blips: back off instead of
      // hammering a socket that is not there.
      state.retry = Math.min((state.retry || 0) + 1, 6);
      const wait = Math.min(1000 * Math.pow(1.6, state.retry), 12000);
      setLink(null, 'reconnecting');
      setTimeout(() => { if (state.active) attach(); }, wait);
    };
  }

  function onChunk(data) {
    if (data.o) {
      state.offset += data.o.length;
      screen.write(data.o);
    }
    if (data.error) { say('TERMINAL', data.error, 'err'); toast(data.error, 'err'); }
    if (data.cwd) {
      const hint = $('where');
      if (hint) hint.textContent = data.cwd;
      const here = state.sessions.find((s) => s.id === (data.session || state.active));
      if (here) { here.cwd = data.cwd; renderTabs(); }
      updateStatusbar();
    }
    if (data.done) {
      say('TERMINAL', `session ended (exit ${data.exit == null ? 0 : data.exit})`);
      loadSessions();
    }
  }

  async function newSession() {
    const { body } = await json(`${API}/sessions`, {
      method: 'POST',
      body: JSON.stringify({ cols: state.cols, rows: state.rows }),
    });
    if (!body.session) { toast(body.error || 'could not open a session', 'err'); return; }
    select(body.session);
    await loadSessions();
    toast(`shell ${body.session} open`, 'ok');
  }

  async function closeSession(id) {
    const { body } = await json(`${API}/stop`, { method: 'POST', body: JSON.stringify({ session: id }) });
    if (state.active === id) { state.active = null; state.offset = 0; screen.clear(); }
    await loadSessions();
    if (body && body.ok) toast('shell closed');
    updateStatusbar();
  }

  function renameSession(session) {
    const label = global.prompt ? prompt('tab name', session.label || '') : null;
    if (!label) return;
    api(`${API}/rename`, {
      method: 'POST',
      body: JSON.stringify({ session: session.id, label }),
    }).then(loadSessions).catch(() => {});
  }

  function renameActive() {
    const s = state.sessions.find((x) => x.id === state.active && !x.closed);
    if (s) renameSession(s); else toast('no focused shell', 'err');
  }

  // ---- health / latency ----------------------------------------------------

  async function ping() {
    const started = (global.performance && performance.now) ? performance.now() : Date.now();
    try {
      const res = await fetch('/health', { cache: 'no-store' });
      if (!res.ok) throw new Error('http ' + res.status);
      const body = await res.json().catch(() => ({}));
      const ms = Math.round(((global.performance && performance.now) ? performance.now() : Date.now()) - started);
      const el = $('latency');
      if (el) el.textContent = ms + ' ms';
      const lat = $('sbLat');
      if (lat) lat.textContent = ms + ' ms';
      const hint = $('agentHint');
      if (hint && body && body.agentbox) {
        hint.textContent = body.agentbox.configured
          ? 'Every command the agent runs appears in a shell tab you can watch.'
          : 'No model provider configured yet — open ⚙ to add one (any OpenAI-compatible endpoint).';
      }
      setAgentOrb(!!(body.agentbox && body.agentbox.configured));
    } catch (err) {
      const el = $('latency');
      if (el) el.textContent = 'offline';
      setLink(false, 'offline');
      setAgentOrb(false);
    }
  }

  // ---- Agentbox ------------------------------------------------------------

async function loadProviders() {
    const { status, body } = await json(`${AGENT}/providers`);
    if (status === 401 || !body || !body.providers) return;
    state.providers = body.providers;
    state.provider = body.active || (state.providers[0] && state.providers[0].id) || '';
    const pick = $('provider');
    if (pick) {
      pick.textContent = '';
      state.providers.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.label || p.id;
        if (p.id === state.provider) opt.selected = true;
        pick.appendChild(opt);
      });
      pick.hidden = state.providers.length < 2;
    }
    renderProviders(body);
    loadLocalEngines();
    const dot = $('adot');
    if (dot) dot.classList.toggle('bad', !state.providers.length);
    renderChips();
    updateStatusbar();
  }

  // ---- local engines (privacy mode helpers) --------------------------------
  async function loadLocalEngines() {
    const box = $('localEngines');
    if (!box) return;
    const { body } = await json(`${AGENT}/local-engines`);
    if (!body || !body.ok) return;
    box.textContent = '';
    (body.engines || []).forEach((e) => {
      const row = document.createElement('div');
      row.className = 'engine';
      const name = document.createElement('b');
      name.textContent = e.engine;
      const meta = document.createElement('span');
      meta.className = 'meta';
      const count = (e.models || []).length;
      meta.textContent = e.configured
        ? 'already configured'
        : `${count} model${count === 1 ? '' : 's'} · ${e.models && e.models[0] ? e.models[0] : ''}`;
      row.append(name, meta);
      if (!e.configured) {
        const add = document.createElement('button');
        add.type = 'button';
        add.textContent = 'add';
        add.onclick = async () => {
          const { status, body: saved } = await json(`${AGENT}/local-engines/add`, {
            method: 'POST',
            body: JSON.stringify({ engine: e.engine, base_url: e.base_url, model: (e.models || [])[0] || '' }),
          });
          if (status !== 200 || !saved.ok) {
            toast((saved && saved.error) || 'could not add', 'err');
            return;
          }
          toast(`${e.engine} added as a provider — prompts now stay on this machine`, 'ok');
          loadProviders();
        };
        row.appendChild(add);
      } else {
        const done = document.createElement('span');
        done.textContent = '✓';
        row.appendChild(done);
      }
      box.appendChild(row);
    });
  }

  function renderProviders(body) {
    const box = $('provList');
    if (!box) return;
    box.textContent = '';
    (body.providers || []).forEach((p) => {
      const row = document.createElement('div');
      row.className = 'prov' + (p.id === state.provider ? ' active' : '');

      const name = document.createElement('b');
      name.textContent = p.id;
      const url = document.createElement('span');
      url.className = 'u';
      url.textContent = `${p.base_url}${p.model ? ' · ' + p.model : ''}${p.has_key ? ' · ' + p.api_key : ''}`;
      const del = document.createElement('button');
      del.type = 'button';
      del.textContent = '×';
      del.setAttribute('aria-label', 'remove ' + p.id);
      del.onclick = async () => {
        await api(`${AGENT}/providers/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
        loadProviders();
      };
      row.append(name, url, del);
      box.appendChild(row);
    });
    const where = $('registry');
    if (where) where.textContent = body.registry || '';
  }

  function renderChips() {
    const box = $('chips');
    if (!box) return;
    box.textContent = '';
    const suggestions = state.providers.length
      ? ['what is running here?', 'install ffmpeg and check the version', 'show disk and memory usage', 'set up a python venv and install requests']
      : ['add a provider to enable the agent'];
    suggestions.forEach((text) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.textContent = text;
      chip.onclick = () => {
        if (!state.providers.length) { const p = $('providerBox'); if (p) { p.hidden = false; loadProviders(); } return; }
        const input = $('input');
        if (input) { input.value = text; input.focus(); }
      };
      box.appendChild(chip);
    });
  }

  function stepCard(step) {
    const details = document.createElement('details');
    details.className = 'step';
    details.setAttribute('data-tool', String(step.tool || ''));
    const summary = document.createElement('summary');
    const cmd = step.args && step.args.command;
    summary.textContent = step.tool === 'run_command' && cmd ? `$ ${cmd}` : String(step.tool || 'step');
    const pre = document.createElement('pre');
    const result = step.result && step.result.output != null ? step.result.output : JSON.stringify(step.result);
    pre.textContent = String(result == null ? '' : result).slice(0, 4000);
    details.append(summary, pre);
    return details;
  }

  async function ask(message) {
    say('YOU', message, 'you');
    setComposerState('working');
    const think = sayReasoning('analysing the task…');
    const { status, body } = await json(`${AGENT}/chat`, {
      method: 'POST',
      body: JSON.stringify({ message, history: state.history, provider: state.provider || undefined }),
    });
    if (status !== 200 || !body.ok) {
      think.done(false);
      const text = body.error || `HTTP ${status}`;
      say('AGENTBOX', text, 'err');
      toast(text, 'err');
      setComposerState('ready');
      return;
    }
    // Steps stream into the reasoning trace as they arrive — the thinking is
    // visible, not hidden behind a spinner.
    (body.steps || []).forEach((step) => { think.step(step); });
    think.done(true);
    const reply = say('AGENTBOX', body.reply || '(no reply)', 'bot');
    if (reply) {
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'copy';
      copy.textContent = 'copy';
      copy.onclick = () => {
        const text = body.reply || '';
        if (global.navigator && navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('reply copied', 'ok'));
      };
      reply.appendChild(copy);
    }
    state.history.push({ role: 'user', content: message }, { role: 'assistant', content: body.reply || '' });
    setComposerState('ready');
    loadSessions();   // its commands opened tabs — show them
  }

  async function saveProvider(form) {
    const payload = {
      id: $('p_id').value.trim(),
      base_url: $('p_url').value.trim(),
      model: $('p_model').value.trim(),
      api_key: $('p_key').value,
    };
    const { status, body } = await json(`${AGENT}/providers`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) {
      const text = body.error || `HTTP ${status}`;
      say('AGENTBOX', text, 'err');
      toast(text, 'err');
      return;
    }
    $('p_key').value = '';
    state.provider = body.provider.id;
    loadProviders();
    say('AGENTBOX', `provider ${body.provider.id} saved — using it for the next message`, 'bot');
    toast(`provider ${body.provider.id} saved`, 'ok');
  }

  // ---- command palette -----------------------------------------------------

  const palette = {
    open: false,
    index: 0,
    items: [],
  };

  function paletteCommands() {
    const cmds = [
      { icon: '＋', label: 'New shell', hint: 'Alt+T', run: () => newSession() },
      { icon: '⟳', label: 'Clear terminal view', hint: 'Alt+K', run: () => screen.clear() },
      { icon: '⌨', label: 'Rename focused shell', hint: 'Alt+R', run: () => renameActive() },
      { icon: '✕', label: 'Kill focused shell', hint: 'Alt+W', run: () => state.active ? closeSession(state.active) : toast('no focused shell', 'err') },
      { icon: '◐', label: 'Toggle agent pane', hint: 'Alt+A', run: () => document.body.classList.toggle('agent-open') },
      { icon: '⧉', label: 'Extensions drawer', hint: 'MCP · skills · plugins', run: () => { const p = $('extPanel'); p.hidden = !p.hidden; if (!p.hidden) loadExtensions(); } },
      { icon: '⚿', label: 'Credentials drawer', hint: 'SSH · accounts', run: () => { const p = $('credPanel'); p.hidden = !p.hidden; if (!p.hidden) loadCredentials(); } },
      { icon: '⚙', label: 'Manage providers', hint: 'agent endpoints', run: () => { const box = $('providerBox'); box.hidden = !box.hidden; if (!box.hidden) loadProviders(); } },
      { icon: '⌁', label: 'Open live browser', hint: 'Alt+B', run: () => showView(browser.view === 'browser' ? 'shell' : 'browser') },
      { icon: '◍', label: 'Launch browser engine', run: () => { showView('browser'); browserStart(); } },
    ];
    THEMES.forEach((t) => {
      cmds.push({
        icon: '◑', label: `Theme: ${t}`,
        hint: t === state.theme ? 'current' : '',
        run: () => { applyTheme(t); toast(`theme: ${t}`, 'ok'); },
      });
    });
    state.providers.forEach((p) => {
      cmds.push({
        icon: '◈', label: `Provider: ${p.label || p.id}`,
        hint: p.id === state.provider ? 'active' : (p.model || ''),
        run: () => {
          state.provider = p.id;
          const pick = $('provider');
          if (pick) pick.value = p.id;
          renderProviders({ providers: state.providers, registry: ($('registry') || {}).textContent });
          updateStatusbar();
          toast(`provider → ${p.id}`, 'ok');
        },
      });
    });
    state.sessions.filter((s) => !s.closed).forEach((s) => {
      cmds.push({
        icon: '›_', label: `Focus shell: ${s.label || s.id}`,
        hint: s.cwd || '',
        run: () => select(s.id),
      });
    });
    return cmds;
  }

  function paletteRender(filter) {
    const list = $('paletteList');
    if (!list) return;
    palette.items = paletteCommands();
    const q = (filter || '').trim().toLowerCase();
    if (q) {
      palette.items = palette.items.filter((c) =>
        c.label.toLowerCase().includes(q) || (c.hint || '').toLowerCase().includes(q));
    }
    list.textContent = '';
    palette.items.slice(0, 24).forEach((c, i) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pal-item' + (i === palette.index ? ' active' : '');
      btn.innerHTML = `<span class="pal-ic">${escapeHtml(c.icon)}</span>` +
        `<span class="pal-label">${escapeHtml(c.label)}</span>` +
        (c.hint ? `<span class="pal-hint">${escapeHtml(c.hint)}</span>` : '');
      btn.onclick = () => { paletteClose(); c.run(); };
      li.appendChild(btn);
      list.appendChild(li);
    });
    palette.index = Math.min(palette.index, Math.max(0, palette.items.length - 1));
  }

  function paletteOpen() {
    const wrap = $('palette');
    if (!wrap) return;
    wrap.hidden = false;
    palette.open = true;
    palette.index = 0;
    const input = $('paletteInput');
    if (input) { input.value = ''; input.focus(); }
    paletteRender('');
  }

  function paletteClose() {
    const wrap = $('palette');
    if (!wrap) return;
    wrap.hidden = true;
    palette.open = false;
  }

  function paletteMove(delta) {
    if (!palette.items.length) return;
    palette.index = (palette.index + delta + palette.items.length) % palette.items.length;
    const list = $('paletteList');
    const active = list && list.children[palette.index];
    if (active) {
      list.querySelectorAll('.pal-item').forEach((n, i) => n.classList.toggle('active', i === palette.index));
      active.scrollIntoView({ block: 'nearest' });
    }
  }

  // ---- agent pane resizer --------------------------------------------------

  function wireResizer() {
    const grip = $('resizer');
    if (!grip) return;
    let startX = 0;
    let startW = 0;
    const MIN = 300;
    const MAX = 720;
    const begin = (e) => {
      if (global.matchMedia && global.matchMedia('(max-width: 900px)').matches) return;
      startX = e.clientX;
      startW = document.querySelector('aside').getBoundingClientRect().width;
      document.body.classList.add('resizing');
      on(global, 'mousemove', move);
      on(global, 'mouseup', end);
      e.preventDefault();
    };
    const move = (e) => {
      const w = Math.min(MAX, Math.max(MIN, startW - (e.clientX - startX)));
      document.documentElement.style.setProperty('--agentw', w + 'px');
      screen.fit();
    };
    const end = () => {
      document.body.classList.remove('resizing');
      global.removeEventListener('mousemove', move);
      global.removeEventListener('mouseup', end);
      const w = getComputedStyle(document.documentElement).getPropertyValue('--agentw').trim();
      store.set('agent_linux_agentw', w);
      postResize();
    };
    grip.addEventListener('mousedown', begin);
    // Touch: same contract, one finger.
    grip.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      startX = e.touches[0].clientX;
      startW = document.querySelector('aside').getBoundingClientRect().width;
      document.body.classList.add('resizing');
    }, { passive: true });
    grip.addEventListener('touchmove', (e) => {
      if (!document.body.classList.contains('resizing') || e.touches.length !== 1) return;
      const w = Math.min(MAX, Math.max(MIN, startW - (e.touches[0].clientX - startX)));
      document.documentElement.style.setProperty('--agentw', w + 'px');
      screen.fit();
    }, { passive: true });
    grip.addEventListener('touchend', () => {
      if (!document.body.classList.contains('resizing')) return;
      document.body.classList.remove('resizing');
      store.set('agent_linux_agentw', getComputedStyle(document.documentElement).getPropertyValue('--agentw').trim());
      postResize();
    });
  }

  function restorePaneWidth() {
    const w = parseInt(store.get('agent_linux_agentw', ''), 10);
    if (w >= 300 && w <= 720) {
      document.documentElement.style.setProperty('--agentw', w + 'px');
    }
  }

  // ---- live browser --------------------------------------------------------

  const BROWSER = '/agent/browser';

  const browser = {
    stream: null,
    running: false,
    available: true,
    url: '',
    title: '',
    viewport: { width: 1280, height: 800 },
    frames: 0,
    lastFrameAt: 0,
    fps: 0,
    view: 'shell',
  };

  function showView(name) {
    browser.view = name === 'browser' ? 'browser' : 'shell';
    const shellish = ['screen', 'tabs'];
    shellish.forEach((id) => { const el = $(id); if (el) el.hidden = browser.view === 'browser'; });
    const cwdPill = $('cwdPill');
    if (cwdPill) cwdPill.hidden = browser.view === 'browser';
    const sb = document.querySelector('.statusbar');
    if (sb) sb.hidden = false;
    const bv = $('browserView');
    if (bv) bv.hidden = browser.view !== 'browser';
    document.querySelectorAll('.segbtn').forEach((btn) => {
      const active = btn.dataset.view === browser.view;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    });
    if (browser.view === 'browser') {
      ensureBrowserStream();
    } else {
      screen.fit();                 // the xterm was hidden; re-fit on the way back
      screen.focus();
      postResize();
    }
  }

  function setBrowserStatus(text, cls) {
    const el = $('bStatus');
    if (el) { el.textContent = text; el.className = 'tagx' + (cls ? ' ' + cls : ''); }
    const dot = $('bDot');
    if (dot) dot.classList.toggle('bad', !browser.running);
  }

  async function browserState() {
    const { status, body } = await json(BROWSER);
    if (status !== 200) { toast(body.error || 'browser unavailable', 'err'); return null; }
    const info = body.browser || {};
    browser.running = !!info.running;
    browser.available = info.available !== false;
    browser.viewport = info.viewport || browser.viewport;
    browser.url = info.url || '';
    browser.title = info.title || '';
    const urlBox = $('bUrl');
    if (urlBox && document.activeElement !== urlBox) urlBox.value = browser.url || '';
    const titleEl = $('bTitle');
    if (titleEl) titleEl.textContent = browser.title || '';
    setBrowserStatus(browser.running ? 'live' : (browser.available ? 'idle' : 'unavailable'),
      browser.running ? 'acc' : '');
    const frame = $('bFrame');
    const empty = $('bEmpty');
    if (frame) frame.hidden = !browser.running;
    if (empty) empty.hidden = browser.running;
    const hint = $('bHint');
    if (hint && !browser.available) {
      hint.textContent = 'Playwright is not installed on this host — see /health or the README.';
    }
    return info;
  }

  async function browserStart() {
    setBrowserStatus('launching…');
    const { status, body } = await json(`${BROWSER}/start`, { method: 'POST' });
    if (status !== 200) {
      toast(body.error || 'could not launch the browser', 'err');
      setBrowserStatus(body.code === 'browser_unavailable' ? 'unavailable' : 'failed');
      return;
    }
    toast('browser launched', 'ok');
    await browserState();
    ensureBrowserStream();
  }

  async function browserStop() {
    await json(`${BROWSER}/stop`, { method: 'POST' });
    if (browser.stream) { browser.stream.close(); browser.stream = null; }
    await browserState();
    setBrowserStatus('idle');
    toast('browser closed');
  }

  async function browserGo(url) {
    const target = (url || ($('bUrl') && $('bUrl').value) || '').trim();
    if (!target) return;
    if (!browser.running) await browserStart();
    setBrowserStatus('loading…');
    const { status, body } = await json(`${BROWSER}/navigate`, {
      method: 'POST', body: JSON.stringify({ url: target }),
    });
    if (status !== 200) { toast(body.error || 'navigation failed', 'err'); setBrowserStatus('error'); return; }
    browser.url = body.browser.url || target;
    if ($('bUrl')) $('bUrl').value = browser.url;
    ensureBrowserStream();
  }

  async function browserAction(payload) {
    if (!browser.running) return;
    const { status, body } = await json(`${BROWSER}/action`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    if (status !== 200) { toast(body.error || 'action failed', 'err'); return; }
    const info = body.browser || {};
    browser.url = info.url || browser.url;
    const titleEl = $('bTitle');
    if (titleEl && info.title) titleEl.textContent = info.title;
  }

  /**
   * The frame stream. Frames arrive as base64 JPEG on an SSE `frame` event, so
   * a page the user is not looking at costs nothing but the connection.
   */
  function ensureBrowserStream() {
    if (browser.stream || browser.view !== 'browser') return;
    const token = state.token ? `&token=${encodeURIComponent(state.token)}` : '';
    // EventSource cannot send a header, so this one route takes the token in the
    // query string. It is a read-only stream of a page the holder can already drive.
    const source = new EventSource(`${BROWSER}/stream?force=1${token}`);
    browser.stream = source;
    source.addEventListener('hello', () => setBrowserStatus(browser.running ? 'live' : 'idle', browser.running ? 'acc' : ''));
    source.addEventListener('frame', (e) => {
      let payload;
      try { payload = JSON.parse(e.data); } catch (err) { return; }
      const frame = $('bFrame');
      if (!frame || !payload.jpeg) return;
      frame.src = `data:image/jpeg;base64,${payload.jpeg}`;
      frame.hidden = false;
      const empty = $('bEmpty');
      if (empty) empty.hidden = true;
      browser.frames += 1;
      const now = Date.now();
      if (browser.lastFrameAt && now - browser.lastFrameAt < 4000) {
        browser.fps = Math.round(1000 / (now - browser.lastFrameAt) * 10) / 10;
      }
      browser.lastFrameAt = now;
      const fps = $('bFps');
      if (fps) fps.textContent = `${browser.frames} frames`;
    });
    source.addEventListener('state', (e) => {
      let info;
      try { info = JSON.parse(e.data); } catch (err) { return; }
      browser.running = !!info.running;
      browser.url = info.url || browser.url;
      browser.title = info.title || browser.title;
      const urlBox = $('bUrl');
      if (urlBox && document.activeElement !== urlBox && info.url) urlBox.value = info.url;
      const titleEl = $('bTitle');
      if (titleEl) titleEl.textContent = info.title || '';
      setBrowserStatus(info.running ? 'live' : 'idle', info.running ? 'acc' : '');
      const frame = $('bFrame');
      if (frame) frame.hidden = !info.running;
      const empty = $('bEmpty');
      if (empty) empty.hidden = !!info.running;
    });
    source.addEventListener('error', (e) => {
      try {
        const payload = JSON.parse(e.data || '{}');
        if (payload.error) toast(payload.error, 'err');
      } catch (err) { /* the connection itself dropped; handled below */ }
    });
    source.onerror = () => {
      source.close();
      browser.stream = null;
      if (browser.view === 'browser') setTimeout(ensureBrowserStream, 2500);
    };
  }

  /** A click on the frame, mapped from the displayed image to the viewport. */
  async function frameClick(event) {
    const frame = $('bFrame');
    if (!frame || !browser.running) return;
    const rect = frame.getBoundingClientRect();
    const natural = { width: browser.viewport.width, height: browser.viewport.height };
    // object-fit: contain letterboxes the image; undo that before scaling.
    const scale = Math.min(rect.width / natural.width, rect.height / natural.height);
    const drawnW = natural.width * scale;
    const drawnH = natural.height * scale;
    const offsetX = (rect.width - drawnW) / 2;
    const offsetY = (rect.height - drawnH) / 2;
    const x = (event.clientX - rect.left - offsetX) / scale;
    const y = (event.clientY - rect.top - offsetY) / scale;
    if (x < 0 || y < 0 || x > natural.width || y > natural.height) return;
    await browserAction({ action: 'click', x: Math.round(x), y: Math.round(y) });
  }

  function browserWheel(event) {
    if (!browser.running) return;
    event.preventDefault();
    browserAction({ action: 'scroll', direction: event.deltaY < 0 ? 'up' : 'down',
                    amount: Math.min(1200, Math.abs(Math.round(event.deltaY)) || 400) });
  }

  const EXT = '/agent/extensions';

  const ext = {
    loaded: false,
    mcp: [],
    skills: [],
    plugins: [],
    store: null,
    catalogue: null,
    codeAllowed: false,
  };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function btn(label, fn, cls) {
    const node = el('button', cls || '', label);
    node.type = 'button';
    node.onclick = fn;
    return node;
  }

  function itemCard(name, enabled, meta, tags, acts) {
    const card = el('div', 'item ' + (enabled ? 'on' : 'off'));
    const top = el('div', 'top');
    top.append(el('b', '', name));
    top.append(el('span', 'grow'));
    if (tags) top.append(...tags);
    card.append(top);
    if (meta) card.append(el('div', 'meta', meta));
    if (acts && acts.length) {
      const row = el('div', 'acts');
      row.append(...acts);
      card.append(row);
    }
    return card;
  }

  async function loadExtensions(force) {
    if (ext.loaded && !force) return;
    try {
      const { status, body } = await json(EXT);
      if (status !== 200) { toast(body.error || 'extensions unavailable', 'err'); return; }
      ext.store = body.store;
      ext.loaded = true;
      const count = (body.mcp.enabled || 0) + (body.skills.enabled || 0) + (body.plugins.enabled || 0);
      const badge = $('extCount');
      if (badge) badge.textContent = String(count);
      const dot = $('extDot');
      if (dot) dot.classList.toggle('bad', !body.mcp.enabled && !body.skills.enabled && !body.plugins.enabled);
      const storeText = $('storeText');
      if (storeText) {
        storeText.textContent = 'store: ' + (body.store.backend || '?') +
          (body.store.configured ? '' : ' (not configured)');
      }
      if (body.unavailable && body.unavailable.length) {
        body.unavailable.forEach((p) => toast(`extension unavailable — ${p.server || p.plugin}: ${p.error}`, 'err'));
      }
    } catch (err) {
      toast('cannot reach the extensions API', 'err');
    }
    await Promise.all([loadMcp(), loadSkills(), loadPlugins()]);
    loadCatalogue();
    loadDb();
  }

  // ---- MCP ----------------------------------------------------------------

  async function loadMcp() {
    const box = $('mcpList');
    if (!box) return;
    const { body } = await json(`${EXT}/mcp`);
    ext.mcp = (body && body.servers) || [];
    box.textContent = '';
    if (!ext.mcp.length) {
      box.append(el('div', 'meta', 'no MCP servers yet — add one below, or copy one from Examples.'));
      return;
    }
    ext.mcp.forEach((server) => {
      const where = server.transport === 'http'
        ? server.url
        : `${server.command} ${(server.args || []).join(' ')}`.trim();
      const tags = [el('span', 'tagx', server.transport)];
      if (server.source === 'env') tags.push(el('span', 'tagx', 'env'));
      if (server.tool_count) tags.push(el('span', 'tagx acc', `${server.tool_count} tools`));
      if (server.has_secrets) tags.push(el('span', 'tagx', 'secrets'));

      const acts = [
        btn('test', () => testMcp(server.id)),
        btn(server.enabled ? 'disable' : 'enable', () => toggleMcp(server.id, !server.enabled)),
        btn('remove', () => removeMcp(server.id)),
      ];
      const card = itemCard(server.label || server.id, server.enabled, where, tags, acts);
      if (server.notes) card.append(el('div', 'meta', server.notes));
      if ((server.tools || []).length) {
        const row = el('div', 'tools');
        server.tools.slice(0, 12).forEach((tool) => row.append(el('span', 'tagx', tool.name)));
        if (server.tools.length > 12) row.append(el('span', 'tagx', `+${server.tools.length - 12}`));
        card.append(row);
      }
      box.append(card);
    });
  }

  async function testMcp(id) {
    toast(`testing ${id}…`);
    const { status, body } = await json(`${EXT}/mcp/${encodeURIComponent(id)}/test`, { method: 'POST' });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    if (!body.ok) { toast(`${id}: ${body.error}`, 'err'); return; }
    toast(`${id}: ${body.tool_count} tools in ${body.latency_ms} ms`, 'ok');
    await loadMcp();
  }

  async function toggleMcp(id, enabled) {
    const { status, body } = await json(`${EXT}/mcp/${encodeURIComponent(id)}/toggle`, {
      method: 'POST', body: JSON.stringify({ enabled }),
    });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    await loadMcp();
  }

  async function removeMcp(id) {
    const { status, body } = await json(`${EXT}/mcp/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    toast(`removed ${id}`, 'ok');
    await loadMcp();
  }

  function jsonField(value, label) {
    const text = (value || '').trim();
    if (!text) return {};
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
    } catch (err) { /* reported below */ }
    throw new Error(`${label} must be a JSON object`);
  }

  async function saveMcp(form) {
    let hdrs, env;
    try {
      hdrs = jsonField($('m_headers').value, 'headers');
      env = jsonField($('m_env').value, 'env');
    } catch (err) { toast(err.message, 'err'); return; }
    const payload = {
      id: $('m_id').value.trim(),
      transport: $('m_transport').value,
      url: $('m_url').value.trim(),
      command: $('m_command').value.trim(),
      args: $('m_args').value.trim(),
      headers: hdrs, env,
    };
    const { status, body } = await json(`${EXT}/mcp`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    form.reset();
    toast(`server ${body.server.id} added — press test to list its tools`, 'ok');
    await loadMcp();
    loadExtensions(true);
  }

  // ---- skills -------------------------------------------------------------

  async function loadSkills() {
    const box = $('skillList');
    if (!box) return;
    const { body } = await json(`${EXT}/skills`);
    ext.skills = (body && body.skills) || [];
    box.textContent = '';
    if (!ext.skills.length) {
      box.append(el('div', 'meta', 'no skills yet — drop a .md or .zip above.'));
      return;
    }
    ext.skills.forEach((skill) => {
      const tags = [];
      if (skill.files.length) tags.push(el('span', 'tagx acc', `${skill.files.length} files`));
      if (skill.source) tags.push(el('span', 'tagx', skill.source));
      const acts = [
        btn('view', () => viewSkill(skill.name)),
        btn(skill.enabled ? 'disable' : 'enable', () => patchSkill(skill.name, { enabled: !skill.enabled })),
        btn('remove', () => removeSkill(skill.name)),
      ];
      const card = itemCard(skill.name, skill.enabled, skill.description, tags, acts);
      if (skill.when_to_use) card.append(el('div', 'meta', 'use when: ' + skill.when_to_use));
      if (skill.files.length) {
        const row = el('div', 'tools');
        skill.files.forEach((file) => row.append(el('span', 'tagx', file)));
        card.append(row);
      }
      box.append(card);
    });
  }

  async function uploadSkillFile(file) {
    if (!file) return;
    const form = new FormData();
    form.append('file', file, file.name);
    // No content-type header: the browser must set the multipart boundary.
    const res = await fetch(`${EXT}/skills/upload`, { method: 'POST', headers: headers(), body: form });
    const body = await res.json().catch(() => ({}));
    if (res.status !== 200) { toast(body.error || `HTTP ${res.status}`, 'err'); return; }
    toast(`skill ${body.skill.name} uploaded`, 'ok');
    await loadSkills();
    loadExtensions(true);
  }

  async function viewSkill(name) {
    const { status, body } = await json(`${EXT}/skills/${encodeURIComponent(name)}`);
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    say(`SKILL · ${name}`, body.skill.body || '(empty body)', 'tool');
    document.body.classList.add('agent-open');
  }

  async function patchSkill(name, patch) {
    const { status, body } = await json(`${EXT}/skills/${encodeURIComponent(name)}`, {
      method: 'PATCH', body: JSON.stringify(patch),
    });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    await loadSkills();
  }

  async function removeSkill(name) {
    const { status, body } = await json(`${EXT}/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    toast(`removed ${name}`, 'ok');
    await loadSkills();
    loadExtensions(true);
  }

  async function saveSkill(form) {
    const payload = {
      name: $('s_name').value.trim(),
      description: $('s_desc').value.trim(),
      body: $('s_body').value,
    };
    const { status, body } = await json(`${EXT}/skills`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    form.reset();
    toast(`skill ${body.skill.name} saved`, 'ok');
    await loadSkills();
    loadExtensions(true);
  }

  // ---- plugins ------------------------------------------------------------

  async function loadPlugins() {
    const box = $('pluginList');
    if (!box) return;
    const { body } = await json(`${EXT}/plugins`);
    ext.plugins = (body && body.plugins) || [];
    ext.codeAllowed = !!(body && body.code_allowed);
    box.textContent = '';
    if (!ext.plugins.length) {
      box.append(el('div', 'meta', 'no plugins yet — add one below.'));
      return;
    }
    ext.plugins.forEach((plugin) => {
      const where = plugin.kind === 'http'
        ? `${plugin.request.method} ${plugin.request.url}`
        : `python · ${plugin.code_bytes} bytes`;
      const tags = [el('span', 'tagx', plugin.kind)];
      const acts = [
        btn('test', () => testPlugin(plugin.name)),
        btn(plugin.enabled ? 'disable' : 'enable', () => patchPlugin(plugin.name, { enabled: !plugin.enabled })),
        btn('remove', () => removePlugin(plugin.name)),
      ];
      const card = itemCard(plugin.name, plugin.enabled, plugin.description, tags, acts);
      card.append(el('div', 'meta', where));
      const args = Object.keys((plugin.parameters && plugin.parameters.properties) || {});
      if (args.length) {
        const row = el('div', 'tools');
        args.forEach((arg) => row.append(el('span', 'tagx', arg)));
        card.append(row);
      }
      box.append(card);
    });
  }

  async function testPlugin(name) {
    const argsText = prompt(`arguments for ${name} (JSON)`, '{}');
    if (argsText == null) return;
    let args;
    try { args = JSON.parse(argsText || '{}'); } catch (err) { toast('arguments must be JSON', 'err'); return; }
    toast(`running ${name}…`);
    const { status, body } = await json(`${EXT}/plugins/${encodeURIComponent(name)}/test`, {
      method: 'POST', body: JSON.stringify({ args }),
    });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    const result = body.result || {};
    const text = result.output || result.error || JSON.stringify(result);
    say(`PLUGIN · ${name}`, String(text).slice(0, 4000), result.error ? 'err' : 'tool');
    toast(result.error ? `${name}: ${result.error}` : `${name}: HTTP ${result.status}`, result.error ? 'err' : 'ok');
    document.body.classList.add('agent-open');
  }

  async function patchPlugin(name, patch) {
    const { status, body } = await json(`${EXT}/plugins/${encodeURIComponent(name)}`, {
      method: 'PATCH', body: JSON.stringify(patch),
    });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    await loadPlugins();
  }

  async function removePlugin(name) {
    const { status, body } = await json(`${EXT}/plugins/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    toast(`removed ${name}`, 'ok');
    await loadPlugins();
    loadExtensions(true);
  }

  async function savePlugin(form) {
    let props = {}, bodySpec = null;
    try {
      props = jsonField($('p_props').value, 'arguments');
      const rawBody = ($('p_body').value || '').trim();
      bodySpec = rawBody ? JSON.parse(rawBody) : null;
    } catch (err) { toast(err.message || 'body must be valid JSON', 'err'); return; }
    const payload = {
      name: $('p_name').value.trim(),
      description: $('p_desc').value.trim(),
      kind: 'http',
      parameters: {
        type: 'object',
        properties: props,
        required: Object.keys(props),
      },
      request: {
        method: $('p_method').value,
        url: $('p_url').value.trim(),
        body: bodySpec,
      },
    };
    const { status, body } = await json(`${EXT}/plugins`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    form.reset();
    toast(`plugin ${body.plugin.name} added — press test to run it`, 'ok');
    await loadPlugins();
    loadExtensions(true);
  }

  // ---- database -----------------------------------------------------------

  async function loadDb() {
    const box = $('dbCard');
    if (!box) return;
    const { status, body } = await json(`${EXT}/db`);
    if (status !== 200) return;
    box.textContent = '';
    const s = body.store || {};
    const top = el('div', 'top');
    top.append(el('b', '', 'backend: ' + (s.backend || '?')));
    top.append(el('span', 'grow'));
    top.append(el('span', 'tagx' + (body.reachable ? ' acc' : ''), body.reachable ? 'reachable' : 'unreachable'));
    if (s.readonly) top.append(el('span', 'tagx', 'read-only'));
    box.append(top);
    box.append(el('div', 'meta', 'table: ' + (s.table || '?')));
    if (body.error) box.append(el('div', 'bad-line', body.error));
    box.append(el('div', 'meta',
      s.backend === 'file'
        ? 'File backend: everything works, but a redeploy wipes it.'
        : 'Database backend: skills, MCP servers and plugins survive a redeploy.'));
    const sql = el('pre', 'out', body.setup_sql || '');
    box.append(sql);
    const copy = btn('copy setup sql', () => {
      if (global.navigator && navigator.clipboard) navigator.clipboard.writeText(body.setup_sql || '').then(() => toast('sql copied', 'ok'));
    });
    box.append(copy);
  }

  async function runDbQuery() {
    const out = $('dbOut');
    const sql = $('dbSql').value.trim();
    if (!sql) return;
    const { status, body } = await json(`${EXT}/db/query`, { method: 'POST', body: JSON.stringify({ sql }) });
    if (out) {
      out.hidden = false;
      out.textContent = status === 200
        ? JSON.stringify(body.rows, null, 2).slice(0, 8000)
        : `${body.code || 'error'}: ${body.error}`;
    }
  }

  // ---- catalogue ----------------------------------------------------------

  async function loadCatalogue() {
    const box = $('catalogue');
    if (!box || ext.catalogue) return;
    const { status, body } = await json(`${EXT}/catalogue`);
    if (status !== 200) return;
    ext.catalogue = body;
    box.textContent = '';

    (body.mcp || []).forEach((server) => {
      const card = el('div', 'item');
      const top = el('div', 'top');
      top.append(el('b', '', server.label || server.id));
      top.append(el('span', 'grow'));
      top.append(el('span', 'tagx', server.transport));
      card.append(top);
      if (server.notes) card.append(el('div', 'meta', server.notes));
      const acts = el('div', 'acts');
      acts.append(btn('fill the MCP form', () => {
        $('m_id').value = server.id;
        $('m_transport').value = server.transport;
        $('m_url').value = server.url || '';
        $('m_command').value = server.command || '';
        $('m_args').value = (server.args || []).join(' ');
        $('m_headers').value = server.headers ? JSON.stringify(server.headers) : '';
        $('m_env').value = server.env ? JSON.stringify(server.env) : '';
        showPane('mcp');
        toast('form filled — check it, then connect');
      }, 'primary'));
      card.append(acts);
      box.append(card);
    });

    const skill = body.skill_example;
    if (skill) {
      const card = el('div', 'item');
      card.append(el('div', 'top')).append(el('b', '', 'skill: ' + skill.name));
      card.append(el('div', 'meta', skill.description));
      const acts = el('div', 'acts');
      acts.append(btn('fill the skill form', () => {
        $('s_name').value = skill.name;
        $('s_desc').value = skill.description;
        $('s_body').value = skill.body;
        showPane('skills');
      }, 'primary'));
      card.append(acts);
      box.append(card);
    }

    const plugin = body.plugin_example;
    if (plugin) {
      const card = el('div', 'item');
      card.append(el('div', 'top')).append(el('b', '', 'plugin: ' + plugin.name));
      card.append(el('div', 'meta', plugin.description));
      const acts = el('div', 'acts');
      acts.append(btn('fill the plugin form', () => {
        $('p_name').value = plugin.name;
        $('p_desc').value = plugin.description;
        $('p_url').value = plugin.request.url;
        $('p_method').value = plugin.request.method;
        $('p_props').value = JSON.stringify(plugin.parameters.properties);
        $('p_body').value = plugin.request.body ? JSON.stringify(plugin.request.body) : '';
        showPane('plugins');
      }, 'primary'));
      card.append(acts);
      box.append(card);
    }
  }

  // ---------------------------------------------------------------------------
  // Credentials — SSH keys, remote hosts and provider accounts.
  //
  // A second drawer, same furniture as extensions (`/agent/ssh*`,
  // `/agent/accounts*`). The host never returns a private key, a password or a
  // token unless a route is called to reveal it, so this client is written to
  // match: masked values are shown as masked, and blank secret fields on an
  // update mean "keep the stored one" — exactly what the API does.
  // ---------------------------------------------------------------------------

  const CRED = '/agent';

  const cred = {
    loaded: false,
    keys: [],
    hosts: [],
    known: [],
    accounts: [],
    catalogue: [],
    providers: {},
    ssh: null,
    vault: null,
  };

  function mkOption(value, label) {
    const node = document.createElement('option');
    node.value = value;
    node.textContent = label;
    return node;
  }

  async function copyText(text, okMessage) {
    if (!text) { toast('nothing to copy', 'err'); return; }
    try {
      if (global.navigator && global.navigator.clipboard) {
        await global.navigator.clipboard.writeText(text);
        toast(okMessage || 'copied', 'ok');
        return;
      }
    } catch (err) { /* clipboard blocked — fall through to the log */ }
    toast('clipboard blocked — the value is in the console log', 'err');
    if (global.console) global.console.log(text);
  }

  async function loadCredentials(force) {
    if (cred.loaded && !force) return;
    try {
      const [ov, keys, hosts, kh, accts, prov] = await Promise.all([
        json(`${CRED}/ssh`),
        json(`${CRED}/ssh/keys`),
        json(`${CRED}/ssh/hosts`),
        json(`${CRED}/ssh/known_hosts`),
        json(`${CRED}/accounts`),
        json(`${CRED}/accounts/providers`),
      ]);
      cred.ssh = (ov.body && ov.body.ssh) || {};
      cred.vault = (ov.body && ov.body.vault) || null;
      cred.keys = (keys.body && keys.body.keys) || [];
      cred.hosts = (hosts.body && hosts.body.hosts) || [];
      cred.known = (kh.body && kh.body.entries) || [];
      cred.accounts = (accts.body && accts.body.accounts) || [];
      if (accts.body && accts.body.vault) cred.vault = accts.body.vault;
      cred.catalogue = (prov.body && prov.body.providers) || [];
      cred.loaded = true;
    } catch (err) {
      toast('cannot reach the credentials API', 'err');
      return;
    }
    renderCredBadge();
    renderSshStatus();
    renderKeys();
    renderHosts();
    renderKnownHosts();
    buildProviderSelect();
    renderAccounts();
  }

  function renderCredBadge() {
    const badge = $('credCount');
    if (badge) {
      badge.textContent = String(cred.keys.length + cred.hosts.length + cred.accounts.length);
    }
    const dot = $('credDot');
    if (dot) dot.classList.toggle('bad', !cred.ssh || cred.ssh.available === false);
    const vaultText = $('vaultText');
    if (vaultText) {
      const v = cred.vault || {};
      vaultText.textContent = 'vault: ' + (v.available ? (v.key_source || 'ready') : 'unavailable');
    }
  }

  function renderSshStatus() {
    const box = $('sshStat');
    if (!box) return;
    const s = cred.ssh || {};
    box.textContent = [
      s.available ? 'ssh present' : 'ssh MISSING on this host',
      s.keygen ? 'ssh-keygen present' : 'ssh-keygen missing',
      `${s.keys || 0} keys · ${s.hosts || 0} hosts`,
    ].join(' · ');
    box.className = s.available ? 'hint' : 'bad-line';
  }

  // ---- SSH keys ------------------------------------------------------------

  function renderKeys() {
    const box = $('keyList');
    if (!box) return;
    box.textContent = '';
    if (!cred.keys.length) {
      box.append(el('div', 'meta', 'no keys yet — generate one below, or import a private key you already have.'));
    } else {
      cred.keys.forEach((key) => {
        const tags = [
          el('span', 'tagx', key.type || 'key'),
          el('span', 'tagx' + (key.has_private ? ' acc' : ''), key.has_private ? 'private in vault' : 'public only'),
        ];
        const acts = [
          btn('copy public', () => copyText(key.public_key || '', 'public key copied')),
          btn('reveal private', () => revealKey(key.name), 'primary'),
          btn('delete', () => deleteKey(key.name)),
        ];
        const meta = `${key.fingerprint || 'no fingerprint'}${key.comment ? ' · ' + key.comment : ''}`;
        box.append(itemCard(key.name, key.has_private, meta, tags, acts));
      });
    }
  }

  async function saveKey(form) {
    const payload = {
      name: $('k_name').value.trim(),
      type: $('k_type').value,
      comment: $('k_comment').value.trim(),
    };
    if (!payload.name) { toast('a key needs a name', 'err'); return; }
    const { status, body } = await json(`${CRED}/ssh/keys`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast((body && body.error) || 'key generation failed', 'err'); return; }
    form.reset();
    toast(`key "${payload.name}" generated`, 'ok');
    await loadCredentials(true);
  }

  async function importKey(form) {
    const payload = {
      name: $('k_import_name').value.trim(),
      private_key: $('k_import_private').value,
      public_key: $('k_import_public').value.trim(),
    };
    if (!payload.name || !payload.private_key.trim()) {
      toast('a name and a private key are required', 'err');
      return;
    }
    const { status, body } = await json(`${CRED}/ssh/keys/import`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast((body && body.error) || 'import failed', 'err'); return; }
    form.reset();
    toast(`key "${payload.name}" imported`, 'ok');
    await loadCredentials(true);
  }

  async function revealKey(name) {
    if (global.confirm && !global.confirm(
      `Reveal the private key "${name}"?\n\nIt will be copied to the clipboard and logged by the host as a deliberate action.`)) return;
    const { status, body } = await json(`${CRED}/ssh/keys/${encodeURIComponent(name)}/private`);
    if (status !== 200 || !body.key) { toast((body && body.error) || 'cannot reveal the key', 'err'); return; }
    await copyText(body.key.private_key || '', 'private key copied to the clipboard');
  }

  async function deleteKey(name) {
    if (global.confirm && !global.confirm(
      `Delete the key "${name}"? Every host using it stops authenticating.`)) return;
    const { status, body } = await json(`${CRED}/ssh/keys/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (status !== 200) { toast((body && body.error) || 'delete failed', 'err'); return; }
    toast(`key "${name}" deleted`, 'ok');
    await loadCredentials(true);
  }

  // ---- remote hosts --------------------------------------------------------

  function renderHosts() {
    const box = $('hostList');
    if (!box) return;
    box.textContent = '';
    if (!cred.hosts.length) {
      box.append(el('div', 'meta', 'no saved hosts yet — add one below and open it as a shell tab.'));
    } else {
      cred.hosts.forEach((host) => {
        const tags = [
          el('span', 'tagx', host.auth || 'key'),
          el('span', 'tagx', ':' + host.port),
        ];
        if (host.key) tags.push(el('span', 'tagx acc', host.key));
        if (host.has_password) tags.push(el('span', 'tagx acc', 'password set'));
        const acts = [
          btn('open shell', () => openHost(host.name), 'primary'),
          btn('probe', () => probeHost(host.name)),
          btn('fill form', () => fillHostForm(host)),
          btn('delete', () => deleteHost(host.name)),
        ];
        const meta = `${host.target}${host.notes ? ' · ' + host.notes : ''}`;
        box.append(itemCard(host.label || host.name, true, meta, tags, acts));
      });
    }
    // Keep the "key to use" picker in step with the key list.
    const pick = $('h_key');
    if (pick) {
      const current = pick.value;
      pick.textContent = '';
      pick.append(mkOption('', '— key (none) —'));
      cred.keys.forEach((k) => pick.append(mkOption(k.name, k.name)));
      pick.value = current;
    }
  }

  function fillHostForm(host) {
    $('h_name').value = host.name || '';
    $('h_label').value = host.label || '';
    $('h_hostname').value = host.hostname || '';
    $('h_user').value = host.user || 'root';
    $('h_port').value = host.port || 22;
    $('h_auth').value = host.auth || 'key';
    $('h_key').value = host.key || '';
    $('h_profile').value = host.profile || '';
    $('h_notes').value = host.notes || '';
    $('h_password').value = '';           // blank on an update keeps the stored one
    showCredPane('sshhosts');
    toast(`form filled from "${host.name}"`, 'ok');
  }

  async function saveHostForm(form) {
    const payload = {
      name: $('h_name').value.trim(),
      label: $('h_label').value.trim(),
      hostname: $('h_hostname').value.trim(),
      user: $('h_user').value.trim() || 'root',
      port: Number($('h_port').value || 22),
      auth: $('h_auth').value,
      key: $('h_key').value,
      profile: $('h_profile').value.trim(),
      password: $('h_password').value,
      notes: $('h_notes').value.trim(),
    };
    if (!payload.name || !payload.hostname) {
      toast('a host needs a name and a hostname', 'err');
      return;
    }
    const { status, body } = await json(`${CRED}/ssh/hosts`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast((body && body.error) || 'save failed', 'err'); return; }
    form.reset();
    $('h_user').value = 'root';
    $('h_port').value = '22';
    toast(`host "${payload.name}" saved`, 'ok');
    await loadCredentials(true);
  }

  async function openHost(name) {
    const { status, body } = await json(
      `${CRED}/ssh/hosts/${encodeURIComponent(name)}/open`, { method: 'POST', body: '{}' });
    if (status !== 200) { toast((body && body.error) || 'cannot open the host', 'err'); return; }
    toast(`opening ${body.label || name}…`, 'ok');
    const panel = $('credPanel');
    if (panel) panel.hidden = true;       // the ssh tab is a normal terminal session
    await loadSessions();
    if (body.session) select(body.session);
  }

  async function probeHost(name) {
    toast(`probing ${name}…`);
    const { status, body } = await json(
      `${CRED}/ssh/hosts/${encodeURIComponent(name)}/probe`, { method: 'POST', body: '{}' });
    if (status !== 200) { toast((body && body.error) || 'probe failed', 'err'); return; }
    if (body.ok) toast(`${name}: ok in ${body.latency_ms}ms`, 'ok');
    else toast(`${name}: ${body.error || 'no answer'}`, 'err');
  }

  async function deleteHost(name) {
    if (global.confirm && !global.confirm(
      `Delete the host "${name}"? The saved credentials go with it.`)) return;
    const { status, body } = await json(`${CRED}/ssh/hosts/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (status !== 200) { toast((body && body.error) || 'delete failed', 'err'); return; }
    toast(`host "${name}" deleted`, 'ok');
    await loadCredentials(true);
  }

  // ---- known hosts ---------------------------------------------------------

  function renderKnownHosts() {
    const pathBox = $('khPath');
    if (pathBox) {
      pathBox.textContent = cred.known.length
        ? `${cred.known.length} entries in the trust file`
        : 'nothing trusted yet — the first ssh connection adds an entry';
    }
    const box = $('khList');
    if (!box) return;
    box.textContent = '';
    if (!cred.known.length) {
      box.append(el('div', 'meta', 'known_hosts is empty.'));
      return;
    }
    cred.known.forEach((entry) => {
      const acts = [btn('forget', () => forgetHost(entry.hosts), 'primary')];
      box.append(itemCard(entry.hosts, true, `${entry.type} · ${entry.key}`, [], acts));
    });
  }

  async function forgetHost(hostname) {
    if (global.confirm && !global.confirm(
      `Forget the host key for "${hostname}"? The next connection trusts it again from scratch.`)) return;
    const { status, body } = await json(
      `${CRED}/ssh/known_hosts`, { method: 'DELETE', body: JSON.stringify({ hostname }) });
    if (status !== 200) { toast((body && body.error) || 'forget failed', 'err'); return; }
    toast(`forgot ${hostname} (${body.forgotten || 0} removed)`, 'ok');
    await loadCredentials(true);
  }

  // ---- accounts ------------------------------------------------------------

  function buildProviderSelect() {
    const pick = $('a_provider');
    if (!pick) return;
    const current = pick.value;
    pick.textContent = '';
    cred.providers = {};
    cred.catalogue.forEach((spec) => {
      cred.providers[spec.id] = spec;
      pick.append(mkOption(spec.id, spec.label || spec.id));
    });
    if (current && cred.providers[current]) pick.value = current;
    renderAccountFields();
  }

  function renderAccountFields(preset) {
    const host = $('aFields');
    if (!host) return;
    const spec = cred.providers[$('a_provider').value];
    host.textContent = '';
    if (!spec) return;
    spec.fields.forEach((field) => {
      const input = document.createElement('input');
      input.id = 'af_' + field.name;
      input.dataset.field = field.name;
      if (field.secret) input.type = 'password';
      input.setAttribute('aria-label', field.name);
      const required = field.required ? ' (required)' : '';
      input.placeholder = `${field.name}${required} — ${field.hint || ''}`;
      const stored = preset && preset[field.name] != null && preset[field.name] !== '';
      if (stored && field.secret) {
        // A masked secret must never be sent back: the API reads a blank secret
        // as "keep the stored one", so the field stays empty on purpose.
        input.placeholder = `${field.name} (set — blank keeps it)`;
      } else if (stored) {
        input.value = preset[field.name];
      }
      host.append(input);
    });
    const note = el('span', 'hint', spec.note || '');
    note.style.padding = '0';
    host.append(note);
  }

  function renderAccounts() {
    const box = $('acctList');
    if (!box) return;
    box.textContent = '';
    if (!cred.accounts.length) {
      box.append(el('div', 'meta', 'no accounts yet — the store keeps using the environment until one is active.'));
      return;
    }
    cred.accounts.forEach((acct) => {
      const tags = [];
      if (acct.active) tags.push(el('span', 'tagx acc', 'active'));
      (acct.secret_fields || []).forEach((field) => {
        if (acct.fields && acct.fields[field]) tags.push(el('span', 'tagx acc', field + ' set'));
      });
      const acts = [
        btn(acct.active ? 'deactivate' : 'activate', () => activateAccount(acct), acct.active ? '' : 'primary'),
        btn('env', () => showEnv(acct)),
        btn('fill form', () => fillAccountForm(acct)),
        btn('delete', () => deleteAccount(acct)),
      ];
      const meta = `${acct.provider}${acct.notes ? ' · ' + acct.notes : ''}`;
      box.append(itemCard(acct.label || acct.name, true, meta, tags, acts));
    });
  }

  function fillAccountForm(acct) {
    $('a_provider').value = acct.provider;
    renderAccountFields(acct.fields || {});
    $('a_name').value = acct.name || '';
    $('a_label').value = acct.label || '';
    $('a_active').checked = !!acct.active;
    $('a_notes').value = acct.notes || '';
    showCredPane('accounts');
    toast('secrets stay masked — leave a secret field blank to keep the stored value', 'ok');
  }

  async function saveAccount(form) {
    const provider = $('a_provider').value;
    const name = $('a_name').value.trim().toLowerCase();
    if (!provider || !name) { toast('pick a provider and name the account', 'err'); return; }
    const fields = {};
    document.querySelectorAll('#aFields input[data-field]').forEach((input) => {
      const value = input.value.trim();
      if (value) fields[input.dataset.field] = value;   // blank ⇒ keep stored
    });
    const payload = {
      provider,
      name,
      label: $('a_label').value.trim(),
      notes: $('a_notes').value.trim(),
      active: $('a_active').checked,
      fields,
    };
    const { status, body } = await json(`${CRED}/accounts`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast((body && body.error) || 'save failed', 'err'); return; }
    form.reset();
    renderAccountFields();
    toast(`account ${provider}/${name} saved`, 'ok');
    await loadCredentials(true);
  }

  async function activateAccount(acct) {
    const next = !acct.active;
    const url = `${CRED}/accounts/${encodeURIComponent(acct.provider)}/${encodeURIComponent(acct.name)}/activate`;
    const { status, body } = await json(url, { method: 'POST', body: JSON.stringify({ active: next }) });
    if (status !== 200) { toast((body && body.error) || 'cannot switch the account', 'err'); return; }
    if (body.store_backend) toast(`${acct.provider}/${acct.name} active — the store is now ${body.store_backend}`, 'ok');
    else toast(`${acct.provider}/${acct.name} ${next ? 'active' : 'inactive'}`, 'ok');
    await loadCredentials(true);
  }

  async function showEnv(acct) {
    const url = `${CRED}/accounts/${encodeURIComponent(acct.provider)}/${encodeURIComponent(acct.name)}/env`;
    const { status, body } = await json(url);
    if (status !== 200) { toast((body && body.error) || 'cannot read the environment', 'err'); return; }
    const names = Object.keys(body.variables || {});
    if (!names.length) { toast('this account exports nothing yet', 'err'); return; }
    if (global.console) global.console.log(`[${acct.provider}/${acct.name}]`, body.variables);
    toast(`${names.join(', ')} — values (masked) in the console log`, 'ok');
  }

  async function deleteAccount(acct) {
    if (global.confirm && !global.confirm(
      `Delete ${acct.provider}/${acct.name}? The stored credentials go with it.`)) return;
    const url = `${CRED}/accounts/${encodeURIComponent(acct.provider)}/${encodeURIComponent(acct.name)}`;
    const { status, body } = await json(url, { method: 'DELETE' });
    if (status !== 200) { toast((body && body.error) || 'delete failed', 'err'); return; }
    toast(`${acct.provider}/${acct.name} deleted`, 'ok');
    await loadCredentials(true);
  }

  function showCredPane(name) {
    const panel = $('credPanel');
    if (panel) panel.hidden = false;
    const scope = panel || document;
    scope.querySelectorAll('.exttab').forEach((tab) => {
      const active = tab.dataset.pane === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    scope.querySelectorAll('.extpane').forEach((pane) => {
      pane.classList.toggle('active', pane.id === 'pane-' + name);
    });
  }

  function showPane(name) {
    const panel = $('extPanel');
    if (panel) panel.hidden = false;
    // Scoped to this drawer: the credentials drawer reuses .exttab/.extpane for
    // the same look, and an unscoped querySelectorAll would deactivate its tabs.
    const scope = panel || document;
    scope.querySelectorAll('.exttab').forEach((tab) => {
      const active = tab.dataset.pane === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    scope.querySelectorAll('.extpane').forEach((pane) => {
      pane.classList.toggle('active', pane.id === 'pane-' + name);
    });
  }

  function autosize(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 240) + 'px';
  }

  /** Composer meta row: busy state + char count. */
  function setComposerState(mode) {
    const tag = $('cmState');
    const input = $('input');
    const send = document.querySelector('#composer button[type="submit"]');
    if (tag) {
      tag.textContent = mode === 'working' ? 'thinking…' : 'ready';
      tag.classList.toggle('acc', mode === 'working');
    }
    if (send) send.disabled = mode === 'working';
    if (mode === 'working' && input) input.placeholder = 'the agent is working — you can keep typing…';
    else if (input) input.placeholder = 'describe the task — the agent plans, runs commands and reports back…';
  }

  function wireComposerMeta() {
    const input = $('input');
    if (!input) return;
    const chars = $('cmChars');
    on(input, 'input', () => { if (chars) chars.textContent = String(input.value.length); });
  }

  function wire() {
    wireComposerMeta();
    on($('new'), 'click', newSession);
    on($('rename'), 'click', renameActive);
    on($('kill'), 'click', () => {
      if (state.active) closeSession(state.active);
      else toast('no focused shell', 'err');
    });
    on($('manage'), 'click', () => {
      const box = $('providerBox');
      box.hidden = !box.hidden;
      if (!box.hidden) loadProviders();
    });
    on($('provider'), 'change', (e) => {
      state.provider = e.target.value;
      renderProviders({ providers: state.providers, registry: ($('registry') || {}).textContent });
      updateStatusbar();
    });
    on($('theme'), 'change', (e) => applyTheme(e.target.value));
    on($('provForm'), 'submit', (e) => { e.preventDefault(); saveProvider(); });
    on($('composer'), 'submit', (e) => {
      e.preventDefault();
      const box = $('input');
      const message = box.value.trim();
      if (!message) return;
      box.value = '';
      autosize(box);
      ask(message);
    });
    on($('input'), 'input', (e) => autosize(e.target));
    on($('input'), 'keydown', (e) => {
      // Enter sends; Shift+Enter (and Ctrl/⌘+Enter) keep working.
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        $('composer').requestSubmit();
      }
    });
    on($('clear'), 'click', () => screen.clear());
    on($('paneToggle'), 'click', () => document.body.classList.toggle('agent-open'));
    on($('closePane'), 'click', () => document.body.classList.remove('agent-open'));
    on(global, 'resize', () => { screen.fit(); postResize(); });

    // ---- command palette ---------------------------------------------------
    on($('paletteBtn'), 'click', paletteOpen);
    on($('paletteBackdrop'), 'click', paletteClose);
    on($('paletteInput'), 'input', (e) => { palette.index = 0; paletteRender(e.target.value); });
    on($('paletteInput'), 'keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); paletteMove(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); paletteMove(-1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        const item = palette.items[palette.index];
        if (item) { paletteClose(); item.run(); }
      }
    });

    // ---- agent pane resizer ------------------------------------------------
    wireResizer();

    // ---- live browser controls --------------------------------------------
    document.querySelectorAll('.segbtn').forEach((btn) => {
      on(btn, 'click', () => showView(btn.dataset.view));
    });
    on($('bGo'), 'click', () => browserGo());
    on($('bUrl'), 'keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); browserGo(); } });
    on($('bStart'), 'click', browserStart);
    on($('bLaunch'), 'click', browserStart);
    on($('bStop'), 'click', browserStop);
    on($('bBack'), 'click', () => browserAction({ action: 'back' }));
    on($('bReload'), 'click', () => browserGo(browser.url || ($('bUrl') && $('bUrl').value)));
    on($('bUp'), 'click', () => browserAction({ action: 'scroll', direction: 'up', amount: 700 }));
    on($('bDown'), 'click', () => browserAction({ action: 'scroll', direction: 'down', amount: 700 }));
    on($('bFrame'), 'click', frameClick);
    on($('bFrame'), 'wheel', browserWheel, { passive: false });
    on($('bType'), 'keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const box = $('bType');
      const text = box.value;
      if (!text) return;
      box.value = '';
      browserAction({ action: 'type', text, submit: true });
    });

    // ---- extensions drawer ------------------------------------------------
    on($('ext'), 'click', () => {
      const panel = $('extPanel');
      panel.hidden = !panel.hidden;
      if (!panel.hidden) loadExtensions();
    });
    on($('extClose'), 'click', () => { const p = $('extPanel'); if (p) p.hidden = true; });
    document.querySelectorAll('#extPanel .exttab').forEach((tab) => {
      on(tab, 'click', () => showPane(tab.dataset.pane));
    });
    on($('mcpForm'), 'submit', (e) => { e.preventDefault(); saveMcp(e.target); });
    on($('skillForm'), 'submit', (e) => { e.preventDefault(); saveSkill(e.target); });
    on($('pluginForm'), 'submit', (e) => { e.preventDefault(); savePlugin(e.target); });
    on($('dbRun'), 'click', runDbQuery);

    // The drop zone is a real file input: click, drop and keyboard all work.
    const drop = $('skillDrop');
    const fileInput = $('skillFile');
    if (drop && fileInput) {
      on(drop, 'click', () => fileInput.click());
      on(fileInput, 'change', () => { uploadSkillFile(fileInput.files && fileInput.files[0]); fileInput.value = ''; });
      ['dragenter', 'dragover'].forEach((name) => on(drop, name, (e) => {
        e.preventDefault(); drop.classList.add('over');
      }));
      ['dragleave', 'drop'].forEach((name) => on(drop, name, (e) => {
        e.preventDefault(); drop.classList.remove('over');
      }));
      on(drop, 'drop', (e) => {
        const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        uploadSkillFile(file);
      });
    }

    // ---- credentials drawer -----------------------------------------------
    const credPanel = $('credPanel');
    on($('cred'), 'click', () => {
      if (!credPanel) return;
      credPanel.hidden = !credPanel.hidden;
      if (!credPanel.hidden) loadCredentials();
    });
    on($('credClose'), 'click', () => { if (credPanel) credPanel.hidden = true; });
    // Scoped on purpose — an unscoped `.exttab` selector would also hit the
    // extensions drawer's tabs and switch both drawers at once.
    if (credPanel) {
      credPanel.querySelectorAll('.exttab').forEach((tab) => {
        on(tab, 'click', () => showCredPane(tab.dataset.pane));
      });
    }
    on($('keyForm'), 'submit', (e) => { e.preventDefault(); saveKey(e.target); });
    on($('keyImportForm'), 'submit', (e) => { e.preventDefault(); importKey(e.target); });
    on($('hostForm'), 'submit', (e) => { e.preventDefault(); saveHostForm(e.target); });
    on($('acctForm'), 'submit', (e) => { e.preventDefault(); saveAccount(e.target); });
    on($('a_provider'), 'change', () => renderAccountFields());

    // Shortcuts — Alt+… never collides with a shell running in the xterm.
    on(global, 'keydown', (e) => {
      if (palette.open) {
        if (e.key === 'Escape') { paletteClose(); return; }
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !e.altKey) {
        e.preventDefault();
        palette.open ? paletteClose() : paletteOpen();
        return;
      }
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const key = e.key.toLowerCase();
      if (key === 'k') { e.preventDefault(); screen.clear(); }
      else if (key === 'r') { e.preventDefault(); renameActive(); }
      else if (key === 'w') { e.preventDefault(); if (state.active) closeSession(state.active); }
      else if (key === 't') { e.preventDefault(); newSession(); }
      else if (key === 'b') { e.preventDefault(); showView(browser.view === 'browser' ? 'shell' : 'browser'); }
      else if (key === 'a') { e.preventDefault(); document.body.classList.toggle('agent-open'); }
      else if (key === 'p') { e.preventDefault(); palette.open ? paletteClose() : paletteOpen(); }
      else if (key === 'c') {
        e.preventDefault();
        if (!credPanel) return;
        credPanel.hidden = !credPanel.hidden;
        if (!credPanel.hidden) loadCredentials();
      }
      else if (key === '1' || key === '2' || key === '3' || key === '4' || key === '5' || key === '6' || key === '7' || key === '8') {
        const idx = Number(key) - 1;
        const live = state.sessions.filter((s) => !s.closed);
        if (live[idx]) { e.preventDefault(); select(live[idx].id); }
      }
    });
    on(global, 'keydown', (e) => {
      if (e.key === 'Escape' && palette.open) paletteClose();
    });
  }

  async function boot() {
    rememberToken();
    applyTheme(store.get('agent_linux_theme', 'obsidian'));
    restorePaneWidth();
    wire();
    buildTerminal();
    setLink(null, 'linking');
    try {
      await loadSessions();
      if (!state.sessions.some((s) => !s.closed)) await newSession();
    } catch (err) {
      setLink(false, 'offline');
      say('TERMINAL', `cannot reach the terminal host (${err && err.message})`, 'err');
    }
    renderEmptyState();
    loadProviders();
    ping();
    global.setInterval(loadSessions, 20000);
    global.setInterval(ping, 45000);
    // Boot splash out — never trap the user behind it.
    const splash = $('boot');
    if (splash) {
      splash.classList.add('out');
      setTimeout(() => splash.remove(), 600);
    }
    updateStatusbar();
  }

  const AgentLinuxConsole = {
    state, screen, api, json, askToken, headers, toast, applyTheme, ping,
    loadSessions, renderTabs, select, attach, onChunk,
    newSession, closeSession, renameSession, renameActive, send, postResize, buildTerminal,
    loadProviders, renderProviders, saveProvider, ask, boot, say, sayThinking, sayReasoning, renderLite,
    paletteOpen, paletteClose, paletteRender, paletteCommands,
    setComposerState, wireComposerMeta,
    ext, loadExtensions, loadMcp, loadSkills, loadPlugins, loadDb, loadCatalogue,
    showPane, uploadSkillFile, runDbQuery,
    cred, loadCredentials, showCredPane,
    browser, showView, browserState, browserStart, browserStop, browserGo, browserAction,
    ensureBrowserStream,
  };

  global.AgentLinuxConsole = AgentLinuxConsole;
  if (global.document && global.document.addEventListener) {
    if (global.document.readyState === 'loading') {
      global.document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);