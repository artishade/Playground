/**
 * NovaRouter terminal console — the standalone workspace client.
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
 * The UI layer (theme, toasts, latency, shortcuts, mobile pane) is deliberately
 * dependency-free: no framework, no build step, one file.
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
        html += '<pre style="margin:8px 0;overflow:auto;white-space:pre-wrap">' + escapeHtml(chunk.replace(/^\w*\n/, '')) + '</pre>';
      } else {
        html += escapeHtml(chunk).replace(/`([^`\n]+)`/g, '<code style="opacity:.85">$1</code>');
      }
    });
    return html;
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
    if (token) store.set('nova_terminal_token', token);
    return token || '';
  }

  function rememberToken() {
    state.token = store.get('nova_terminal_token', '');
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
    store.set('nova_terminal_theme', state.theme);
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
        fontFamily: 'ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace',
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
      const hint = $('agentHint');
      if (hint && body && body.agentbox) {
        hint.textContent = body.agentbox.configured
          ? 'Every command the agent runs appears in a shell tab you can watch.'
          : 'No model provider configured yet — open ⚙ to add one (any OpenAI-compatible endpoint).';
      }
    } catch (err) {
      const el = $('latency');
      if (el) el.textContent = 'offline';
      setLink(false, 'offline');
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
    const dot = $('adot');
    if (dot) dot.classList.toggle('bad', !state.providers.length);
    renderChips();
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
    const pending = say('AGENTBOX', 'thinking…', 'bot');
    if (pending) pending.querySelector('.who').textContent = 'AGENTBOX · working';
    const { status, body } = await json(`${AGENT}/chat`, {
      method: 'POST',
      body: JSON.stringify({ message, history: state.history, provider: state.provider || undefined }),
    });
    if (pending) pending.remove();
    if (status !== 200 || !body.ok) {
      const text = body.error || `HTTP ${status}`;
      say('AGENTBOX', text, 'err');
      toast(text, 'err');
      return;
    }
    const box = $('log');
    (body.steps || []).forEach((step) => {
      if (box) { box.appendChild(stepCard(step)); box.scrollTop = box.scrollHeight; }
    });
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
    const bv = $('browserView');
    if (bv) bv.hidden = browser.view !== 'browser';
    document.querySelectorAll('.segbtn').forEach((btn) => {
      const active = btn.dataset.view === browser.view;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    });
    if (browser.view === 'browser') {
      screen.fit();                 // the xterm was hidden; re-fit on the way back
      ensureBrowserStream();
    } else {
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
    let headers, env;
    try {
      headers = jsonField($('m_headers').value, 'headers');
      env = jsonField($('m_env').value, 'env');
    } catch (err) { toast(err.message, 'err'); return; }
    const payload = {
      id: $('m_id').value.trim(),
      transport: $('m_transport').value,
      url: $('m_url').value.trim(),
      command: $('m_command').value.trim(),
      args: $('m_args').value.trim(),
      headers, env,
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

  function showPane(name) {
    const panel = $('extPanel');
    if (panel) panel.hidden = false;
    document.querySelectorAll('.exttab').forEach((tab) => {
      const active = tab.dataset.pane === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    document.querySelectorAll('.extpane').forEach((pane) => {
      pane.classList.toggle('active', pane.id === 'pane-' + name);
    });
  }

  function autosize(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
  }

  function wire() {
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
    document.querySelectorAll('.exttab').forEach((tab) => {
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

    // Shortcuts — Alt+… never collides with a shell running in the xterm.
    on(global, 'keydown', (e) => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const key = e.key.toLowerCase();
      if (key === 'k') { e.preventDefault(); screen.clear(); }
      else if (key === 'r') { e.preventDefault(); renameActive(); }
      else if (key === 'w') { e.preventDefault(); if (state.active) closeSession(state.active); }
      else if (key === 't') { e.preventDefault(); newSession(); }
      else if (key === 'b') { e.preventDefault(); showView(browser.view === 'browser' ? 'shell' : 'browser'); }
      else if (key === 'a') { e.preventDefault(); document.body.classList.toggle('agent-open'); }
      else if (key === '1' || key === '2' || key === '3' || key === '4' || key === '5' || key === '6' || key === '7' || key === '8') {
        const idx = Number(key) - 1;
        const live = state.sessions.filter((s) => !s.closed);
        if (live[idx]) { e.preventDefault(); select(live[idx].id); }
      }
    });
  }

  async function boot() {
    rememberToken();
    applyTheme(store.get('nova_terminal_theme', 'obsidian'));
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
  }

  const NovaTerminalConsole = {
    state, screen, api, json, askToken, headers, toast, applyTheme, ping,
    loadSessions, renderTabs, select, attach, onChunk,
    newSession, closeSession, renameSession, renameActive, send, postResize, buildTerminal,
    loadProviders, renderProviders, saveProvider, ask, boot, say,
    ext, loadExtensions, loadMcp, loadSkills, loadPlugins, loadDb, loadCatalogue,
    showPane, uploadSkillFile, runDbQuery,
    browser, showView, browserState, browserStart, browserStop, browserGo, browserAction,
  };

  global.NovaTerminalConsole = NovaTerminalConsole;
  if (global.document && global.document.addEventListener) {
    global.document.addEventListener('DOMContentLoaded', boot);
  }
})(typeof window !== 'undefined' ? window : globalThis);