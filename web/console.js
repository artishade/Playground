/**
 * Agent_Linux console — Cherry Studio-style client.
 *
 * Views: chats · terminal · live browser · paintings · settings, behind an
 * icon rail and a context list column. Chats are topics (named, persisted in
 * localStorage) with assistants (personas → per-request system prompt),
 * a composer model/provider picker and attachments. The terminal keeps real
 * PTY tabs over SSE; the browser view streams a shared Chromium; paintings
 * call the agent's image endpoint and file library.
 *
 * No framework, no build step, one file.
 */
(function (global) {
  'use strict';

  const API = '/terminal/pty';
  const AGENT = '/agent';
  const EXT = '/agent/extensions';
  const BROWSER = '/agent/browser';
  const CRED = '/agent';
  const VIEWS = ['chats', 'terminal', 'browser', 'paintings', 'settings'];
  const THEMES = ['cherry', 'cherry-light', 'obsidian', 'plasma', 'matrix', 'glacier', 'ember'];
  const TERM_THEMES = {
    'cherry':       { background: '#0d0d12', foreground: '#cdd2e0', cursor: '#eb5757', selectionBackground: '#eb575740' },
    'cherry-light': { background: '#ffffff', foreground: '#2a2c36', cursor: '#eb5757', selectionBackground: '#eb575733' },
    obsidian:       { background: '#05060c', foreground: '#cbd5e1', cursor: '#7c5cff', selectionBackground: '#7c5cff40' },
    plasma:         { background: '#0a0510', foreground: '#e6d9f5', cursor: '#ff3ea5', selectionBackground: '#ff3ea540' },
    matrix:         { background: '#040a07', foreground: '#c8f7d6', cursor: '#3ee07f', selectionBackground: '#3ee07f40' },
    glacier:        { background: '#040810', foreground: '#cfe3ff', cursor: '#38bdf8', selectionBackground: '#38bdf840' },
    ember:          { background: '#0c0705', foreground: '#f6ddd0', cursor: '#fb923c', selectionBackground: '#fb923c40' },
  };
  const DEFAULT_ASSISTANTS = [
    { id: 'agentbox', name: 'Agentbox', emoji: '📦', prompt: '' },
    { id: 'devops', name: 'DevOps', emoji: '🛠️',
      prompt: 'You are a pragmatic DevOps engineer. Prefer the smallest safe command, explain what you are checking and why, and never run destructive commands without saying so first.' },
    { id: 'coder', name: 'Pair Coder', emoji: '👨‍💻',
      prompt: 'You are a senior pair programmer. Read before you write, keep changes minimal, and show the diff-style summary of what you changed and why.' },
    { id: 'writer', name: 'Tech Writer', emoji: '✍️',
      prompt: 'You are a precise technical writer. Prefer concrete facts from the workspace over generic advice, and format answers with short headed sections.' },
  ];
  const QUICK_LINKS = ['example.com', 'news.ycombinator.com', 'github.com', 'wikipedia.org'];

  const state = {
    token: '',
    view: 'chats',
    theme: 'cherry',
    // terminal
    sessions: [], active: null, offset: 0, stream: null, cols: 120, rows: 32,
    max: 8, retry: 0,
    // agent
    providers: [], provider: '', models: [], model: 'auto',
    modelByProvider: {},
    // chats
    topics: [], activeTopic: null,
    assistants: [], activeAssistant: 'agentbox',
    attachments: [],
    // paintings
    paintings: [],
    // browser
    browser: { stream: null, running: false, available: true, url: '', title: '',
               viewport: { width: 1280, height: 800 }, frames: 0, lastFrameAt: 0, fps: 0 },
    online: null,
    historyTurns: 12,
  };

  // ---- tiny helpers -----------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const on = (elx, name, fn, opts) => elx && elx.addEventListener(name, fn, opts);
  const store = {
    get(key, fallback) {
      try { const v = global.localStorage.getItem(key); return v == null ? fallback : v; }
      catch (e) { return fallback; }
    },
    getJSON(key, fallback) {
      try { const v = global.localStorage.getItem(key); return v ? JSON.parse(v) : fallback; }
      catch (e) { return fallback; }
    },
    set(key, value) { try { global.localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
    setJSON(key, value) { try { global.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* */ } },
    del(key) { try { global.localStorage.removeItem(key); } catch (e) { /* */ } },
  };
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);

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
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function toast(text, cls) {
    const box = $('toasts');
    if (!box) return;
    const node = el('div', 'toast ' + (cls || ''), String(text == null ? '' : text));
    box.appendChild(node);
    setTimeout(() => node.remove(), cls === 'err' ? 7000 : 3600);
  }
  function fileUrl(path) {
    return `${AGENT}/files/${String(path).replace(/^\/+/, '')}` +
      (state.token ? `?token=${encodeURIComponent(state.token)}` : '');
  }

  // ---- transport ---------------------------------------------------------------

  function headers(extra) {
    const out = Object.assign({ 'content-type': 'application/json' }, extra || {});
    if (state.token) out['X-Nova-Terminal-Token'] = state.token;
    return out;
  }
  async function api(path, options) {
    const opts = Object.assign({}, options || {});
    opts.headers = headers(opts.headers);
    let res = await fetch(path, opts);
    if (res.status === 401 && !state.pendingToken) {
      const token = askToken();
      if (token) { state.token = token; opts.headers = headers(); res = await fetch(path, opts); }
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
    state.pendingToken = true;
    const token = prompt('This terminal host requires its shared secret\n(AGENT_LINUX_TERMINAL_TOKEN):', state.token || '');
    state.pendingToken = false;
    if (token) store.set('agent_linux_token', token);
    return token || '';
  }
  function rememberToken() { state.token = store.get('agent_linux_token', ''); }

  // ---- link status ---------------------------------------------------------------

  function setLink(ok, text) {
    const dot = $('dot'), label = $('linkText');
    if (dot) {
      dot.classList.toggle('bad', ok === false);
      dot.classList.toggle('warn', ok === null);
      dot.classList.toggle('pulse', ok !== true);
    }
    if (label && text) label.textContent = text;
  }

  async function ping() {
    const started = (global.performance && performance.now) ? performance.now() : Date.now();
    try {
      const res = await fetch('/health', { cache: 'no-store' });
      if (!res.ok) throw new Error('http ' + res.status);
      const body = await res.json().catch(() => ({}));
      const ms = Math.round(((global.performance && performance.now) ? performance.now() : Date.now()) - started);
      if ($('latency')) $('latency').textContent = ms + ' ms';
      setLink(true, 'live');
      state.online = true;
    } catch (err) {
      state.online = false;
      if ($('latency')) $('latency').textContent = 'offline';
      setLink(false, 'offline');
    }
  }

  // ---- theme ----------------------------------------------------------------------

  function applyTheme(name) {
    state.theme = THEMES.indexOf(name) >= 0 ? name : 'cherry';
    document.documentElement.setAttribute('data-theme', state.theme);
    const pick = $('theme');
    if (pick) pick.value = state.theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', TERM_THEMES[state.theme].background);
    if (screen.term && screen.term.options) {
      screen.term.options.theme = TERM_THEMES[state.theme];
      if (screen.term.refresh) screen.term.refresh(0, state.rows);
    }
    store.set('agent_linux_theme', state.theme);
  }

  // ---- views -------------------------------------------------------------------------

  const LIST_TITLES = { chats: 'Chats', terminal: 'Shells', browser: 'Quick links',
                        paintings: 'Paintings', settings: 'Settings' };

  function showView(name) {
    state.view = VIEWS.indexOf(name) >= 0 ? name : 'chats';
    document.querySelectorAll('.rbtn[data-view]').forEach((b) => {
      b.classList.toggle('active', b.dataset.view === state.view);
    });
    VIEWS.forEach((v) => { const node = $(v + 'View'); if (node) node.hidden = v !== state.view; });
    const title = $('listTitle');
    if (title) title.textContent = LIST_TITLES[state.view] || 'Agent_Linux';
    const action = $('listAction');
    if (action) {
      action.hidden = state.view === 'settings';
      action.textContent = state.view === 'paintings' ? '🎨' : '＋';
      action.title = state.view === 'terminal' ? 'new shell'
        : state.view === 'paintings' ? 'go to the prompt' : 'new chat';
    }
    renderList();
    if (state.view === 'terminal') { screen.fit(); screen.focus(); postResize(); }
    if (state.view === 'browser') ensureBrowserStream();
    if (state.view === 'paintings') renderPaintings();
    store.set('agent_linux_view', state.view);
  }

  /**
   * The list column re-renders per view: topics under Chats, live shells under
   * Terminal, quick links under Browser, the gallery under Paintings.
   */
  function renderList() {
    const box = $('listScroll');
    if (!box) return;
    box.textContent = '';
    const q = (($('listSearch') || {}).value || '').trim().toLowerCase();

    if (state.view === 'chats') {
      const groups = { today: [], earlier: [] };
      const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
      state.topics.forEach((t) => {
        if (q && !(t.title || '').toLowerCase().includes(q)) return;
        (t.updatedAt >= dayStart.getTime() ? groups.today : groups.earlier).push(t);
      });
      const paint = (list, label) => {
        if (!list.length) return;
        box.append(el('div', 'lgroup', label));
        list.forEach((t) => {
          const item = el('button', 'litem' + (t.id === state.activeTopic ? ' active' : ''));
          item.type = 'button';
          const asst = state.assistants.find((a) => a.id === t.assistant);
          item.append(el('span', 'ic', (asst && asst.emoji) || '💬'));
          const wrap = el('span', 't');
          wrap.append(el('span', '', t.title || 'untitled'));
          wrap.append(el('span', 'sub', new Date(t.updatedAt).toLocaleString()));
          item.append(wrap);
          const del = el('span', 'del', '×');
          del.setAttribute('role', 'button');
          del.onclick = (e) => { e.stopPropagation(); deleteTopic(t.id); };
          item.append(del);
          item.onclick = () => { selectTopic(t.id); if (global.innerWidth <= 900) document.body.classList.remove('list-open'); };
          box.append(item);
        });
      };
      paint(groups.today, 'today');
      paint(groups.earlier, 'earlier');
      if (!state.topics.length) box.append(el('div', 'lgroup', 'no chats yet — press ＋'));
    }

    if (state.view === 'terminal') {
      state.sessions.forEach((s) => {
        if (q && !(`${s.label} ${s.cwd}`).toLowerCase().includes(q)) return;
        const item = el('button', 'litem' + (s.id === state.active ? ' active' : ''));
        item.type = 'button';
        item.append(el('span', 'ic', s.closed ? '○' : '⌨'));
        const wrap = el('span', 't');
        wrap.append(el('span', '', s.label || s.id));
        wrap.append(el('span', 'sub', s.cwd || (s.closed ? 'ended' : 'shell')));
        item.append(wrap);
        item.onclick = () => { if (!s.closed) select(s.id); };
        box.append(item);
      });
      if (!state.sessions.filter((s) => !s.closed).length) {
        box.append(el('div', 'lgroup', 'no shells — press ＋ or + shell'));
      }
    }

    if (state.view === 'browser') {
      box.append(el('div', 'lgroup', 'jump to'));
      QUICK_LINKS.forEach((url) => {
        const item = el('button', 'litem');
        item.type = 'button';
        item.append(el('span', 'ic', '🌐'));
        item.append(el('span', 't', url));
        item.onclick = () => { showView('browser'); browserGo(url); };
        box.append(item);
      });
    }

    if (state.view === 'paintings') {
      const list = state.paintings.filter((p) => !q || (p.prompt || '').toLowerCase().includes(q));
      box.append(el('div', 'lgroup', `${list.length} image${list.length === 1 ? '' : 's'}`));
      list.forEach((p) => {
        const item = el('button', 'litem');
        item.type = 'button';
        item.append(el('span', 'ic', '🖼'));
        const wrap = el('span', 't');
        wrap.append(el('span', '', p.prompt || 'untitled'));
        wrap.append(el('span', 'sub', p.model || p.path || ''));
        item.append(wrap);
        item.onclick = () => openLightbox(p.url || fileUrl(p.path));
        box.append(item);
      });
    }

    if (state.view === 'settings') {
      box.append(el('div', 'lgroup', 'appearance · providers · agent · data'));
    }
  }

  // ---- topics (chats) --------------------------------------------------------------------

  function loadChats() {
    state.topics = store.getJSON('agent_linux_topics', []);
    state.activeTopic = store.get('agent_linux_topic', '');
    if (!state.topics.find((t) => t.id === state.activeTopic)) state.activeTopic = null;
    if (!state.topics.length) newTopic(true);
  }
  function saveChats() {
    store.setJSON('agent_linux_topics', state.topics.slice(0, 80));
    store.set('agent_linux_topic', state.activeTopic || '');
  }
  function currentTopic() {
    let t = state.topics.find((x) => x.id === state.activeTopic);
    if (!t) t = newTopic(true);
    return t;
  }
  function newTopic(quiet) {
    const t = { id: uid(), title: 'New chat', createdAt: Date.now(), updatedAt: Date.now(),
                assistant: state.activeAssistant, messages: [] };
    state.topics.unshift(t);
    state.activeTopic = t.id;
    saveChats();
    renderList();
    renderChat();
    if (!quiet) { showView('chats'); const input = $('input'); if (input) input.focus(); }
    return t;
  }
  function deleteTopic(id) {
    state.topics = state.topics.filter((t) => t.id !== id);
    if (state.activeTopic === id) state.activeTopic = state.topics[0] ? state.topics[0].id : null;
    if (!state.topics.length) newTopic(true);
    saveChats();
    renderList();
    renderChat();
    toast('chat deleted');
  }
  function selectTopic(id) {
    state.activeTopic = id;
    const t = currentTopic();
    if (t.assistant) state.activeAssistant = t.assistant;
    saveChats();
    renderList();
    renderChat();
    renderAssistantPick();
  }
  function addMsg(topic, role, content, extra) {
    const msg = Object.assign({ role, content, ts: Date.now() }, extra || {});
    topic.messages.push(msg);
    topic.updatedAt = Date.now();
    if (role === 'user' && (topic.title === 'New chat' || !topic.title)) {
      topic.title = content.slice(0, 48) + (content.length > 48 ? '…' : '');
    }
    saveChats();
    return msg;
  }

  // ---- chat rendering -----------------------------------------------------------------------

  function renderLite(text) {
    const raw = String(text == null ? '' : text);
    const parts = raw.split(/```/);
    let html = '';
    parts.forEach((chunk, i) => {
      if (i % 2 === 1) {
        const code = escapeHtml(chunk.replace(/^\w*\n/, ''));
        html += '<pre class="codeblock">' + code +
          '<button type="button" class="code-copy">copy</button></pre>';
      } else {
        html += escapeHtml(chunk).replace(/`([^`\n]+)`/g, '<code>$1</code>');
      }
    });
    return html;
  }

  function msgNode(msg) {
    const node = el('div', 'msg ' + (msg.role === 'user' ? 'you' : 'bot'));
    const who = el('span', 'who', (msg.role === 'user' ? 'YOU' : 'AGENTBOX') +
      ' · ' + new Date(msg.ts || Date.now()).toLocaleTimeString());
    node.append(who);
    const body = el('span', 'body');
    body.innerHTML = renderLite(msg.content);
    node.append(body);
    if (msg.imgs && msg.imgs.length) {
      const imgs = el('div', 'imgs');
      msg.imgs.forEach((src) => {
        const img = el('img');
        img.src = src; img.alt = 'generated image'; img.loading = 'lazy';
        img.onclick = () => openLightbox(src);
        imgs.append(img);
      });
      node.append(imgs);
    }
    if (msg.role === 'assistant') {
      const regen = el('button', 'regen', '↻');
      regen.title = 'regenerate';
      regen.onclick = () => regenerate();
      node.append(regen);
    }
    const copy = el('button', 'copy', 'copy');
    copy.onclick = () => {
      if (global.navigator && navigator.clipboard) navigator.clipboard.writeText(msg.content).then(() => toast('copied', 'ok'));
    };
    node.append(copy);
    return node;
  }

  function renderChat() {
    const inner = $('chatInner');
    if (!inner) return;
    inner.textContent = '';
    const topic = currentTopic();
    topic.messages.forEach((m) => inner.append(msgNode(m)));
    if (!topic.messages.length) {
      const hello = el('div', 'msg bot');
      hello.append(el('span', 'who', 'AGENTBOX'));
      const body = el('span', 'body');
      body.innerHTML = renderLite(
        'New chat ready. I run in a real root shell — ask me to inspect, build, deploy or fix something, and every command lands in a terminal tab you can watch.\n\nTry:\n• `what is running here?`\n• `set up a python venv and install requests`\n• `check disk and memory, then tail the newest log`');
      hello.append(body);
      inner.append(hello);
    }
    const box = $('chatMessages');
    if (box) box.scrollTop = box.scrollHeight;
  }

  /** A visible reasoning card with a live tool trace and a timer. */
  function sayReasoning(label) {
    const inner = $('chatInner');
    if (!inner) return { el: null, remove() {}, step() {}, done() {} };
    const card = el('div', 'msg reasoning');
    card.append(el('span', 'who', 'AGENTBOX · working'));
    const status = el('div', 'think-status');
    const line = el('span', 'think-line', label || 'analysing the task…');
    const timer = el('span', 'tagx', '0.0s');
    const t0 = Date.now();
    const tick = setInterval(() => { timer.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's'; }, 100);
    status.append(line, timer);
    const trace = el('div', 'think-trace');
    card.append(status, trace);
    inner.append(card);
    const box = $('chatMessages');
    if (box) box.scrollTop = box.scrollHeight;
    return {
      el: card,
      step(stepObj) {
        trace.append(stepCard(stepObj));
        line.textContent = stepObj.tool === 'run_command' && stepObj.args && stepObj.args.command
          ? `ran: ${String(stepObj.args.command).slice(0, 80)}`
          : `called ${stepObj.tool || 'a tool'}`;
        if (box) box.scrollTop = box.scrollHeight;
      },
      done(ok) {
        clearInterval(tick);
        card.classList.add('done');
        line.textContent = ok === false ? 'ran into a problem — see below' : 'done — assembling the answer';
      },
      remove() { clearInterval(tick); card.remove(); },
    };
  }

  function stepCard(step) {
    const details = el('details', 'step');
    details.setAttribute('data-tool', String(step.tool || ''));
    const summary = el('summary');
    const cmd = step.args && step.args.command;
    summary.textContent = step.tool === 'run_command' && cmd ? `$ ${cmd}` : String(step.tool || 'step');
    const pre = el('pre');
    const result = step.result && step.result.output != null ? step.result.output : JSON.stringify(step.result);
    pre.textContent = String(result == null ? '' : result).slice(0, 4000);
    details.append(summary, pre);
    return details;
  }

  // ---- composer: state, attachments, assistants, models -----------------------------------

  function setComposerState(mode) {
    const tag = $('cmState');
    const send = $('sendBtn');
    const input = $('input');
    if (tag) {
      tag.textContent = mode === 'working' ? 'thinking…' : 'ready';
      tag.classList.toggle('acc', mode === 'working');
    }
    if (send) send.disabled = mode === 'working';
    if (mode === 'working' && input) input.placeholder = 'the agent is working — you can keep typing…';
    else if (input) input.placeholder = 'describe the task — the agent plans, runs commands and reports back…';
  }
  function autosize(node) {
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = Math.min(node.scrollHeight, 200) + 'px';
  }
  function renderAttachments() {
    const box = $('attPreview');
    if (!box) return;
    box.textContent = '';
    state.attachments.forEach((a, i) => {
      const chip = el('span', 'chip');
      chip.append(el('span', '', (a.name || a.path) + (a.bytes ? ' · ' + Math.round(a.bytes / 1024) + 'kB' : '')));
      const x = el('button', '', '×');
      x.onclick = () => { state.attachments.splice(i, 1); renderAttachments(); };
      chip.append(x);
      box.append(chip);
    });
  }
  async function uploadAttachment(file) {
    if (!file) return;
    const form = new FormData();
    form.append('file', file, file.name);
    const res = await fetch(`${AGENT}/files/upload`, { method: 'POST', headers: headers(), body: form });
    const body = await res.json().catch(() => ({}));
    if (res.status !== 200 || !body.ok) { toast(body.error || `upload failed (HTTP ${res.status})`, 'err'); return; }
    state.attachments.push(body);
    renderAttachments();
    toast(`attached ${body.name}`, 'ok');
  }

  function renderAssistantPick() {
    const pick = $('assistantPick');
    if (!pick) return;
    pick.textContent = '';
    state.assistants.forEach((a) => {
      const opt = el('option', '', `${a.emoji || '🤖'} ${a.name}`);
      opt.value = a.id;
      if (a.id === state.activeAssistant) opt.selected = true;
      pick.append(opt);
    });
    const t = state.topics.find((x) => x.id === state.activeTopic);
    if (t) { t.assistant = state.activeAssistant; saveChats(); }
  }
  function activeAssistant() {
    return state.assistants.find((a) => a.id === state.activeAssistant) || state.assistants[0];
  }

  function renderAssistantEditor() {
    const box = $('asstList');
    if (!box) return;
    box.textContent = '';
    state.assistants.forEach((a) => {
      const card = el('div', 'item' + (a.id === state.activeAssistant ? ' on' : ''));
      const top = el('div', 'top');
      top.append(el('b', '', `${a.emoji || '🤖'} ${a.name}`));
      top.append(el('span', 'grow'));
      top.append(btn(a.id === state.activeAssistant ? 'active' : 'use', () => {
        state.activeAssistant = a.id;
        store.set('agent_linux_assistant', a.id);
        renderAssistantPick(); renderAssistantEditor(); renderList();
      }, a.id === state.activeAssistant ? '' : 'primary'));
      card.append(top);
      card.append(el('div', 'meta', a.prompt ? a.prompt.slice(0, 180) + (a.prompt.length > 180 ? '…' : '') : 'no persona — uses the built-in prompt'));
      card.append(btn('edit', () => {
        $('as_name').value = a.name;
        $('as_emoji').value = a.emoji || '';
        $('as_prompt').value = a.prompt || '';
        $('asstForm').dataset.editing = a.id;
        toast(`editing ${a.name}`);
      }));
      box.append(card);
    });
  }
  function saveAssistantForm(form) {
    const name = $('as_name').value.trim();
    if (!name) { toast('a name is required', 'err'); return; }
    const emoji = $('as_emoji').value.trim();
    const promptText = $('as_prompt').value;
    const editing = form.dataset.editing || '';
    let rec = state.assistants.find((a) => a.id === editing) ||
              state.assistants.find((a) => a.name === name);
    if (rec) {
      rec.name = name; rec.emoji = emoji; rec.prompt = promptText;
    } else {
      rec = { id: 'as-' + uid(), name, emoji, prompt: promptText };
      state.assistants.push(rec);
    }
    form.dataset.editing = '';
    store.setJSON('agent_linux_assistants', state.assistants);
    renderAssistantEditor();
    renderAssistantPick();
    toast(`assistant ${name} saved`, 'ok');
  }

  // ---- providers + models ---------------------------------------------------------------------

  async function loadProviders() {
    const { status, body } = await json(`${AGENT}/providers`);
    if (status === 401 || !body || !body.providers) return;
    state.providers = body.providers;
    state.provider = body.active || (state.providers[0] && state.providers[0].id) || '';
    state.model = state.modelByProvider[state.provider] || 'auto';
    const pick = $('providerPick');
    if (pick) {
      pick.textContent = '';
      state.providers.forEach((p) => {
        const opt = el('option', '', p.label || p.id);
        opt.value = p.id;
        if (p.id === state.provider) opt.selected = true;
        pick.append(opt);
      });
    }
    renderProviders();
    loadLocalEngines();
    loadModels();
    renderList();
  }

  function renderProviders() {
    const box = $('provList');
    if (!box) return;
    box.textContent = '';
    state.providers.forEach((p) => {
      const row = el('div', 'prov' + (p.id === state.provider ? ' active' : ''));
      row.append(el('b', '', p.id));
      row.append(el('span', 'u', `${p.base_url}${p.model ? ' · ' + p.model : ''}${p.has_key ? ' · ' + p.api_key : ''}`));
      row.append(btn(p.id === state.provider ? 'active' : 'use', () => {
        state.provider = p.id;
        state.model = state.modelByProvider[p.id] || 'auto';
        const pick = $('providerPick');
        if (pick) pick.value = p.id;
        renderProviders();
        loadModels();
        toast(`provider → ${p.id}`, 'ok');
      }));
      row.append(btn('×', async () => {
        await api(`${AGENT}/providers/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
        loadProviders();
      }));
      box.append(row);
    });
  }

  async function loadModels() {
    const pick = $('modelPick');
    if (!pick) return;
    pick.textContent = '';
    const auto = el('option', '', 'model: auto');
    auto.value = 'auto';
    pick.append(auto);
    if (!state.provider) return;
    const { status, body } = await json(`${AGENT}/models?provider=${encodeURIComponent(state.provider)}`);
    state.models = (status === 200 && body && body.models) || [];
    state.models.slice(0, 60).forEach((m) => {
      const opt = el('option', '', m);
      opt.value = m;
      pick.append(opt);
    });
    pick.value = state.model && state.models.indexOf(state.model) >= 0 ? state.model : 'auto';
  }

  async function saveProviderForm() {
    const payload = {
      id: $('p_id').value.trim(),
      base_url: $('p_url').value.trim(),
      model: $('p_model').value.trim(),
      api_key: $('p_key').value,
    };
    const { status, body } = await json(`${AGENT}/providers`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    $('p_key').value = '';
    state.provider = body.provider.id;
    loadProviders();
    toast(`provider ${body.provider.id} saved`, 'ok');
  }

  async function loadLocalEngines() {
    const box = $('localEngines');
    if (!box) return;
    const { body } = await json(`${AGENT}/local-engines`);
    if (!body || !body.ok) return;
    box.textContent = '';
    (body.engines || []).forEach((e) => {
      const row = el('div', 'engine');
      row.append(el('b', '', e.engine));
      const count = (e.models || []).length;
      row.append(el('span', 'meta', e.configured ? 'already configured'
        : `${count} model${count === 1 ? '' : 's'} · ${e.models && e.models[0] ? e.models[0] : ''}`));
      if (!e.configured) {
        row.append(btn('add', async () => {
          const { status, body: saved } = await json(`${AGENT}/local-engines/add`, {
            method: 'POST',
            body: JSON.stringify({ engine: e.engine, base_url: e.base_url, model: (e.models || [])[0] || '' }),
          });
          if (status !== 200 || !saved.ok) { toast((saved && saved.error) || 'could not add', 'err'); return; }
          toast(`${e.engine} added — prompts stay on this machine`, 'ok');
          loadProviders();
        }));
      }
      box.append(row);
    });
  }

  // ---- the ask flow -----------------------------------------------------------------------------

  function historyForApi() {
    const topic = currentTopic();
    return topic.messages
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .slice(-state.historyTurns * 2)
      .map((m) => ({ role: m.role, content: m.content }));
  }

  async function ask(message, opts) {
    const options = opts || {};
    const topic = currentTopic();
    if (!options.skipUser) addMsg(topic, 'user', message);
    renderChat();
    setComposerState('working');
    const think = sayReasoning(options.skipUser ? 'rethinking the last answer…' : 'analysing the task…');

    const payload = { message, history: historyForApi(), provider: state.provider || undefined };
    if (state.model && state.model !== 'auto') payload.model = state.model;
    const asst = activeAssistant();
    if (asst && asst.prompt && asst.prompt.trim()) payload.system = asst.prompt.trim();
    if (state.attachments.length) {
      const mention = state.attachments
        .map((a) => `[attached ${a.name || 'file'}: ${a.path}]`)
        .join('\n');
      payload.message = `${message}\n\n${mention}`;
      state.attachments = [];
      renderAttachments();
    }

    const { status, body } = await json(`${AGENT}/chat`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    if (status !== 200 || !body.ok) {
      think.done(false);
      const text = body.error || `HTTP ${status}`;
      addMsg(topic, 'assistant', '⚠ ' + text);
      renderChat();
      toast(text, 'err');
      setComposerState('ready');
      return;
    }
    (body.steps || []).forEach((s) => think.step(s));
    think.done(true);
    // Screenshots the agent took land as viewable images on the reply.
    const imgs = (body.steps || [])
      .map((s) => s.result && s.result.path)
      .filter((p) => typeof p === 'string' && /\.(png|jpe?g)$/i.test(p))
      .map((p) => fileUrl(p));
    addMsg(topic, 'assistant', body.reply || '(no reply)', { imgs: imgs.length ? imgs : undefined });
    renderChat();
    setComposerState('ready');
    loadSessions();
    renderList();
  }

  function regenerate() {
    const topic = currentTopic();
    while (topic.messages.length && topic.messages[topic.messages.length - 1].role === 'assistant') {
      topic.messages.pop();
    }
    const lastUser = [].concat(topic.messages).reverse().find((m) => m.role === 'user');
    if (!lastUser) { toast('nothing to regenerate', 'err'); return; }
    saveChats();
    ask(lastUser.content, { skipUser: true });
  }

  // ---- paintings -----------------------------------------------------------------------------------

  function loadPaintings() {
    state.paintings = store.getJSON('agent_linux_paintings', []);
  }
  function savePaintings() {
    store.setJSON('agent_linux_paintings', state.paintings.slice(0, 200));
  }
  function renderPaintings() {
    const grid = $('paintingsGrid');
    const empty = $('paintEmpty');
    if (!grid) return;
    grid.textContent = '';
    if (empty) empty.hidden = state.paintings.length > 0;
    state.paintings.forEach((p) => {
      const card = el('div', 'pcard');
      const img = el('img');
      img.loading = 'lazy';
      img.src = p.url || fileUrl(p.path);
      img.alt = p.prompt || 'painting';
      img.onclick = () => openLightbox(img.src);
      card.append(img);
      const meta = el('div', 'pmeta');
      meta.title = p.prompt || '';
      meta.append(el('span', '', `${p.model || 'image'} · ${new Date(p.ts || Date.now()).toLocaleDateString()}`));
      meta.append(el('span', 'grow'));
      const copyPath = btn('⧉', () => {
        const text = p.path || p.url || '';
        if (global.navigator && navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('path copied', 'ok'));
      });
      copyPath.title = 'copy workspace path';
      meta.append(copyPath);
      card.append(meta);
      grid.append(card);
    });
    const prov = $('paintProv');
    if (prov) prov.textContent = 'provider: ' + (state.provider || '—');
  }
  async function generatePainting() {
    const input = $('paintPrompt');
    const prompt = ((input && input.value) || '').trim();
    if (!prompt) { toast('describe the image first', 'err'); return; }
    const go = $('paintGo');
    if (go) { go.disabled = true; go.textContent = 'painting…'; }
    const { status, body } = await json(`${AGENT}/images/generate`, {
      method: 'POST',
      body: JSON.stringify({ prompt, size: ($('paintSize') || {}).value || '1024x1024', provider: state.provider || undefined }),
    });
    if (go) { go.disabled = false; go.textContent = 'generate'; }
    if (status !== 200 || !body.ok) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    (body.images || []).forEach((img) => {
      state.paintings.unshift({ path: img.path, url: img.url, prompt: body.revised_prompt || prompt,
                                model: body.model, ts: Date.now() });
    });
    savePaintings();
    renderPaintings();
    renderList();
    toast(`${(body.images || []).length} image(s) ready`, 'ok');
  }

  // ---- lightbox ---------------------------------------------------------------------------------------

  function openLightbox(src) {
    const box = $('lightbox'), img = $('lightboxImg');
    if (!box || !img) return;
    img.src = src;
    box.hidden = false;
  }
  function closeLightbox() {
    const box = $('lightbox');
    if (box) box.hidden = true;
  }

  // ---- terminal -------------------------------------------------------------------------------

  const screen = {
    term: null,
    plain: null,
    write(data) {
      if (this.term) { this.term.write(data); return; }
      if (this.plain) { this.plain.textContent += data; this.plain.scrollTop = this.plain.scrollHeight; }
    },
    resize(cols, rows) {
      state.cols = cols; state.rows = rows;
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
        convertEol: false, cursorBlink: true, cursorStyle: 'bar',
        fontSize: 13, lineHeight: 1.25,
        fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        scrollback: 5000, allowProposedApi: true,
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
      const note = el('div', 'screen-note',
        'xterm.js unavailable — plain viewer (no colour, no full-screen apps)');
      host.append(note);
      const pre = el('pre', 'plain');
      pre.setAttribute('aria-label', 'terminal output');
      host.append(pre);
      screen.plain = pre;
      host.setAttribute('tabindex', '0');
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
    renderList();  // the terminal list mirrors the tabs
    const live = state.sessions.filter((s) => !s.closed);
    if (!state.active || !live.some((s) => s.id === state.active)) {
      const preferred = live.find((s) => s.active) || live[0];
      if (preferred) select(preferred.id);
    }
  }

  function renderTabs() {
    const box = $('tabs');
    if (!box) return;
    box.textContent = '';
    state.sessions.forEach((s) => {
      const tab = el('button', 'tab' + (s.id === state.active ? ' active' : '') + (s.closed ? ' closed' : ''));
      tab.type = 'button';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(s.id === state.active));
      tab.append(el('span', '', s.label || s.id));
      tab.append(el('em', '', s.cwd || ''));
      const x = el('span', 'x', '×');
      x.onclick = (e) => { e.stopPropagation(); closeSession(s.id); };
      tab.append(x);
      tab.onclick = () => {
        if (s.closed) { toast(`"${s.label || s.id}" already ended`, 'err'); return; }
        api(`${API}/activate`, { method: 'POST', body: JSON.stringify({ session: s.id }) }).catch(() => {});
        select(s.id);
      };
      tab.ondblclick = () => renameSession(s);
      box.append(tab);
    });
    const count = $('count');
    if (count) count.textContent = `${state.sessions.length}/${state.max || 8}`;
    const newBtn = $('new');
    if (newBtn) newBtn.disabled = state.sessions.length >= (state.max || 8);
  }

  function select(id) {
    if (!id) return;
    state.active = id;
    state.offset = 0;
    screen.clear();
    renderTabs();
    renderList();
    attach();
    screen.focus();
    const here = state.sessions.find((s) => s.id === id);
    if ($('where')) $('where').textContent = here ? (here.cwd || '') : '';
  }

  function attach() {
    if (state.stream && state.stream.close) state.stream.close();
    state.stream = null;
    if (!state.active) return;
    const token = state.token ? `&token=${encodeURIComponent(state.token)}` : '';
    const source = new EventSource(`${API}/stream?session=${encodeURIComponent(state.active)}&offset=${state.offset}${token}`);
    state.stream = source;
    source.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch (err) { return; }
      onChunk(data);
    };
    source.onerror = () => {
      source.close();
      state.stream = null;
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
    if (data.error) { toast(data.error, 'err'); }
    if (data.cwd) {
      if ($('where')) $('where').textContent = data.cwd;
      const here = state.sessions.find((s) => s.id === (data.session || state.active));
      if (here) { here.cwd = data.cwd; renderTabs(); }
    }
    if (data.done) {
      toast(`session ended (exit ${data.exit == null ? 0 : data.exit})`);
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
      method: 'POST', body: JSON.stringify({ session: session.id, label }),
    }).then(loadSessions).catch(() => {});
  }
  function renameActive() {
    const s = state.sessions.find((x) => x.id === state.active && !x.closed);
    if (s) renameSession(s); else toast('no focused shell', 'err');
  }

  // ---- live browser ---------------------------------------------------------------------------

  function setBrowserStatus(text, cls) {
    const tag = $('bStatus');
    if (tag) { tag.textContent = text; tag.className = 'tagx' + (cls ? ' ' + cls : ''); }
  }
  async function browserState() {
    const { status, body } = await json(BROWSER);
    if (status !== 200) return null;
    const info = body.browser || {};
    state.browser.running = !!info.running;
    state.browser.available = info.available !== false;
    state.browser.viewport = info.viewport || state.browser.viewport;
    state.browser.url = info.url || '';
    state.browser.title = info.title || '';
    const urlBox = $('bUrl');
    if (urlBox && document.activeElement !== urlBox) urlBox.value = state.browser.url || '';
    if ($('bTitle')) $('bTitle').textContent = state.browser.title || '';
    setBrowserStatus(state.browser.running ? 'live' : (state.browser.available ? 'idle' : 'unavailable'),
      state.browser.running ? 'acc' : '');
    const frame = $('bFrame'), empty = $('bEmpty');
    if (frame) frame.hidden = !state.browser.running;
    if (empty) empty.hidden = state.browser.running;
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
    if (state.browser.stream) { state.browser.stream.close(); state.browser.stream = null; }
    await browserState();
    setBrowserStatus('idle');
    toast('browser closed');
  }
  async function browserGo(url) {
    const target = (url || ($('bUrl') && $('bUrl').value) || '').trim();
    if (!target) return;
    if (!state.browser.running) await browserStart();
    setBrowserStatus('loading…');
    const { status, body } = await json(`${BROWSER}/navigate`, {
      method: 'POST', body: JSON.stringify({ url: target }),
    });
    if (status !== 200) { toast(body.error || 'navigation failed', 'err'); setBrowserStatus('error'); return; }
    state.browser.url = body.browser.url || target;
    if ($('bUrl')) $('bUrl').value = state.browser.url;
    ensureBrowserStream();
  }
  async function browserAction(payload) {
    if (!state.browser.running) return;
    const { status, body } = await json(`${BROWSER}/action`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    if (status !== 200) { toast(body.error || 'action failed', 'err'); return; }
    const info = body.browser || {};
    state.browser.url = info.url || state.browser.url;
    if (info.title && $('bTitle')) $('bTitle').textContent = info.title;
  }

  /** SSE frame stream — base64 JPEG on a `frame` event; idle pages cost nothing. */
  function ensureBrowserStream() {
    if (state.browser.stream || state.view !== 'browser') return;
    const token = state.token ? `&token=${encodeURIComponent(state.token)}` : '';
    const source = new EventSource(`${BROWSER}/stream?force=1${token}`);
    state.browser.stream = source;
    source.addEventListener('hello', () => setBrowserStatus(state.browser.running ? 'live' : 'idle',
      state.browser.running ? 'acc' : ''));
    source.addEventListener('frame', (e) => {
      let payload;
      try { payload = JSON.parse(e.data); } catch (err) { return; }
      const frame = $('bFrame');
      if (!frame || !payload.jpeg) return;
      frame.src = `data:image/jpeg;base64,${payload.jpeg}`;
      frame.hidden = false;
      const empty = $('bEmpty');
      if (empty) empty.hidden = true;
      state.browser.frames += 1;
      const now = Date.now();
      if (state.browser.lastFrameAt && now - state.browser.lastFrameAt < 4000) {
        state.browser.fps = Math.round(1000 / (now - state.browser.lastFrameAt) * 10) / 10;
      }
      state.browser.lastFrameAt = now;
      if ($('bFps')) $('bFps').textContent = `${state.browser.frames} frames`;
    });
    source.addEventListener('state', (e) => {
      let info;
      try { info = JSON.parse(e.data); } catch (err) { return; }
      state.browser.running = !!info.running;
      state.browser.url = info.url || state.browser.url;
      state.browser.title = info.title || state.browser.title;
      const urlBox = $('bUrl');
      if (urlBox && document.activeElement !== urlBox && info.url) urlBox.value = info.url;
      if ($('bTitle')) $('bTitle').textContent = info.title || '';
      setBrowserStatus(info.running ? 'live' : 'idle', info.running ? 'acc' : '');
      const frame = $('bFrame'), empty = $('bEmpty');
      if (frame) frame.hidden = !info.running;
      if (empty) empty.hidden = !!info.running;
    });
    source.onerror = () => {
      source.close();
      state.browser.stream = null;
      if (state.view === 'browser') setTimeout(ensureBrowserStream, 2500);
    };
  }

  /** Click on the frame, mapped from the displayed image to the viewport. */
  async function frameClick(event) {
    const frame = $('bFrame');
    if (!frame || !state.browser.running) return;
    const rect = frame.getBoundingClientRect();
    const natural = state.browser.viewport;
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
    if (!state.browser.running) return;
    event.preventDefault();
    browserAction({ action: 'scroll', direction: event.deltaY < 0 ? 'up' : 'down',
                    amount: Math.min(1200, Math.abs(Math.round(event.deltaY)) || 400) });
  }

// ---- extensions -----------------------------------------------------------------------------

  const ext = { loaded: false, mcp: [], skills: [], plugins: [], store: null, catalogue: null };

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
    } catch (err) { /* clipboard blocked — fall through */ }
    toast('clipboard blocked — the value is in the console log', 'err');
    if (global.console) global.console.log(text);
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

  async function loadExtensions(force) {
    if (ext.loaded && !force) return;
    try {
      const { status, body } = await json(EXT);
      if (status !== 200) { toast(body.error || 'extensions unavailable', 'err'); return; }
      ext.store = body.store;
      ext.loaded = true;
      const count = (body.mcp.enabled || 0) + (body.skills.enabled || 0) + (body.plugins.enabled || 0);
      const badge = $('extCount');
      if (badge) { badge.textContent = String(count); badge.hidden = count === 0; }
      const dot = $('extDot');
      if (dot) dot.classList.toggle('bad', count === 0);
      if ($('storeText')) {
        $('storeText').textContent = 'store: ' + (body.store.backend || '?') +
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

  // ---- MCP ------------------------------------------------------------------------------------

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
    loadExtensions(true);
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

  // ---- skills ---------------------------------------------------------------------------------

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
    toast(name + ': ' + String(body.skill.body || '').slice(0, 120) + '…');
    if (global.console) global.console.log(`[${name}]`, body.skill.body);
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

  // ---- plugins ------------------------------------------------------------------------------------

  async function loadPlugins() {
    const box = $('pluginList');
    if (!box) return;
    const { body } = await json(`${EXT}/plugins`);
    ext.plugins = (body && body.plugins) || [];
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
    if (global.console) global.console.log(`[${name}]`, result);
    toast(result.error ? `${name}: ${result.error}` : `${name}: ok`, result.error ? 'err' : 'ok');
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
      parameters: { type: 'object', properties: props, required: Object.keys(props) },
      request: { method: $('p_method').value, url: $('p_url').value.trim(), body: bodySpec },
    };
    const { status, body } = await json(`${EXT}/plugins`, { method: 'POST', body: JSON.stringify(payload) });
    if (status !== 200) { toast(body.error || `HTTP ${status}`, 'err'); return; }
    form.reset();
    toast(`plugin ${body.plugin.name} added`, 'ok');
    await loadPlugins();
    loadExtensions(true);
  }

  // ---- database ---------------------------------------------------------------------------------------

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
        : 'Database backend: extensions survive a redeploy.'));
    const sql = el('pre', 'out', body.setup_sql || '');
    box.append(sql);
    box.append(btn('copy setup sql', () => copyText(body.setup_sql || '', 'sql copied')));
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

  // ---- catalogue -------------------------------------------------------------------------------------------

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

  // ---- credentials -------------------------------------------------------------------------------------------

  const cred = { loaded: false, keys: [], hosts: [], known: [], accounts: [], catalogue: [], providers: {}, ssh: null, vault: null };

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
    const count = cred.keys.length + cred.hosts.length + cred.accounts.length;
    if (badge) { badge.textContent = String(count); badge.hidden = count === 0; }
    const dot = $('credDot');
    if (dot) dot.classList.toggle('bad', !cred.ssh || cred.ssh.available === false);
    if ($('vaultText')) {
      const v = cred.vault || {};
      $('vaultText').textContent = 'vault: ' + (v.available ? (v.key_source || 'ready') : 'unavailable');
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
  }

  function renderKeys() {
    const box = $('keyList');
    if (!box) return;
    box.textContent = '';
    if (!cred.keys.length) {
      box.append(el('div', 'meta', 'no keys yet — generate one below, or import a private key you already have.'));
      return;
    }
    cred.keys.forEach((key) => {
      const tags = [
        el('span', 'tagx', key.type || 'key'),
        el('span', 'tagx' + (key.has_private ? ' acc' : ''), key.has_private ? 'private in vault' : 'public only'),
      ];
      const acts = [
        btn('copy public', () => copyText(key.public_key || '', 'public key copied')),
        btn('reveal', () => revealKey(key.name), 'primary'),
        btn('delete', () => deleteKey(key.name)),
      ];
      const meta = `${key.fingerprint || 'no fingerprint'}${key.comment ? ' · ' + key.comment : ''}`;
      box.append(itemCard(key.name, key.has_private, meta, tags, acts));
    });
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
      `Reveal the private key "${name}"?\n\nIt will be copied to the clipboard as a deliberate action.`)) return;
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

  function renderHosts() {
    const box = $('hostList');
    if (!box) return;
    box.textContent = '';
    if (!cred.hosts.length) {
      box.append(el('div', 'meta', 'no saved hosts yet — add one below and open it as a shell tab.'));
      return;
    }
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
    $('h_password').value = '';
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
    if (panel) panel.hidden = true;
    showView('terminal');
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
        input.placeholder = `${field.name} (set — blank keeps it)`;
      } else if (stored) {
        input.value = preset[field.name];
      }
      host.append(input);
    });
    host.append(el('span', 'hint', spec.note || ''));
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
      if (value) fields[input.dataset.field] = value;
    });
    const payload = {
      provider, name,
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

// ---- command palette ----------------------------------------------------------------------------------------------

  const palette = { open: false, index: 0, items: [] };

  function paletteCommands() {
    const cmds = [
      { icon: '💬', label: 'View: Chats', hint: 'Ctrl+1', run: () => showView('chats') },
      { icon: '⌨', label: 'View: Terminal', hint: 'Ctrl+2', run: () => showView('terminal') },
      { icon: '🌐', label: 'View: Live browser', hint: 'Ctrl+3', run: () => showView('browser') },
      { icon: '🖼', label: 'View: Paintings', hint: 'Ctrl+4', run: () => showView('paintings') },
      { icon: '⚙', label: 'View: Settings', hint: 'Ctrl+5', run: () => showView('settings') },
      { icon: '＋', label: 'New chat', run: () => newTopic() },
      { icon: '＋', label: 'New shell', hint: 'Alt+T', run: () => { showView('terminal'); newSession(); } },
      { icon: '✎', label: 'Assistants', hint: 'personas', run: () => { const p = $('asstPanel'); p.hidden = false; renderAssistantEditor(); } },
      { icon: '⧉', label: 'Extensions drawer', hint: 'MCP · skills · plugins', run: () => { const p = $('extPanel'); p.hidden = !p.hidden; if (!p.hidden) loadExtensions(); } },
      { icon: '⚿', label: 'Credentials drawer', hint: 'SSH · accounts', run: () => { const p = $('credPanel'); p.hidden = !p.hidden; if (!p.hidden) loadCredentials(); } },
      { icon: '⏻', label: 'Launch browser engine', run: () => { showView('browser'); browserStart(); } },
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
          const pick = $('providerPick');
          if (pick) pick.value = p.id;
          renderProviders();
          loadModels();
          toast(`provider → ${p.id}`, 'ok');
        },
      });
    });
    state.assistants.forEach((a) => {
      cmds.push({
        icon: a.emoji || '🤖', label: `Assistant: ${a.name}`,
        hint: a.id === state.activeAssistant ? 'active' : '',
        run: () => {
          state.activeAssistant = a.id;
          store.set('agent_linux_assistant', a.id);
          renderAssistantPick();
          toast(`assistant → ${a.name}`, 'ok');
        },
      });
    });
    state.sessions.filter((s) => !s.closed).forEach((s) => {
      cmds.push({
        icon: '›_', label: `Focus shell: ${s.label || s.id}`,
        hint: s.cwd || '',
        run: () => { showView('terminal'); select(s.id); },
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
      const li = el('li');
      const node = el('button', 'pal-item' + (i === palette.index ? ' active' : ''));
      node.type = 'button';
      node.innerHTML = `<span class="pal-ic">${escapeHtml(c.icon)}</span>` +
        `<span class="pal-label">${escapeHtml(c.label)}</span>` +
        (c.hint ? `<span class="pal-hint">${escapeHtml(c.hint)}</span>` : '');
      node.onclick = () => { paletteClose(); c.run(); };
      li.append(node);
      list.append(li);
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
      if (active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
    }
  }

  // ---- settings wiring -----------------------------------------------------------------------------------------------

  function wireSettings() {
    on($('wipeChats'), 'click', () => {
      if (global.confirm && !global.confirm('Delete every chat on this device?')) return;
      state.topics = [];
      store.del('agent_linux_topics');
      store.del('agent_linux_topic');
      newTopic(true);
      renderList();
      renderChat();
      toast('all chats cleared');
    });
    on($('wipeAll'), 'click', () => {
      if (global.confirm && !global.confirm('Reset everything — chats, assistants, paintings and settings?')) return;
      ['agent_linux_topics', 'agent_linux_topic', 'agent_linux_assistants', 'agent_linux_assistant',
        'agent_linux_paintings', 'agent_linux_theme', 'agent_linux_view', 'agent_linux_history'].forEach(store.del);
      state.assistants = [].concat(DEFAULT_ASSISTANTS);
      state.activeAssistant = 'agentbox';
      state.paintings = [];
      loadChats();
      renderAssistantPick();
      renderAssistantEditor();
      renderPaintings();
      renderList();
      toast('reset done');
    });
    const hist = $('setHistory');
    if (hist) {
      hist.value = String(store.get('agent_linux_history', '12'));
      on(hist, 'change', () => {
        const n = Math.max(0, Math.min(50, parseInt(hist.value, 10) || 12));
        state.historyTurns = n;
        store.set('agent_linux_history', String(n));
        toast(`history: ${n} turns`, 'ok');
      });
    }
  }

  // ---- wiring -----------------------------------------------------------------------------------------------------------

  function wire() {
    // rail views
    document.querySelectorAll('.rbtn[data-view]').forEach((node) => {
      on(node, 'click', () => {
        showView(node.dataset.view);
        if (global.innerWidth <= 900 && node.dataset.view === 'chats') document.body.classList.add('list-open');
      });
    });
    // list
    on($('listAction'), 'click', () => {
      if (state.view === 'chats') newTopic();
      else if (state.view === 'terminal') { showView('terminal'); newSession(); }
      else if (state.view === 'paintings') { const input = $('paintPrompt'); if (input) input.focus(); }
    });
    on($('listSearch'), 'input', () => renderList());
    // composer
    on($('composer'), 'submit', (e) => {
      e.preventDefault();
      const box = $('input');
      const message = (box.value || '').trim();
      if (!message) return;
      box.value = '';
      autosize(box);
      if ($('cmChars')) $('cmChars').textContent = '0';
      ask(message);
    });
    on($('input'), 'input', (e) => {
      autosize(e.target);
      if ($('cmChars')) $('cmChars').textContent = String(e.target.value.length);
    });
    on($('input'), 'keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        $('composer').requestSubmit();
      }
    });
    on($('attachBtn'), 'click', () => { const f = $('attachFile'); if (f) f.click(); });
    on($('attachFile'), 'change', (e) => {
      const file = e.target.files && e.target.files[0];
      uploadAttachment(file);
      e.target.value = '';
    });
    on($('assistantPick'), 'change', (e) => {
      state.activeAssistant = e.target.value;
      store.set('agent_linux_assistant', state.activeAssistant);
      const t = state.topics.find((x) => x.id === state.activeTopic);
      if (t) { t.assistant = state.activeAssistant; saveChats(); }
      renderList();
    });
    on($('editAssistant'), 'click', () => {
      const p = $('asstPanel');
      p.hidden = false;
      renderAssistantEditor();
    });
    on($('modelPick'), 'change', (e) => {
      state.model = e.target.value;
      state.modelByProvider[state.provider] = state.model;
    });
    on($('providerPick'), 'change', (e) => {
      state.provider = e.target.value;
      state.model = state.modelByProvider[state.provider] || 'auto';
      renderProviders();
      loadModels();
      toast(`provider → ${state.provider}`, 'ok');
    });
    // terminal
    on($('new'), 'click', newSession);
    on($('rename'), 'click', renameActive);
    on($('kill'), 'click', () => {
      if (state.active) closeSession(state.active);
      else toast('no focused shell', 'err');
    });
    // theme
    on($('theme'), 'change', (e) => applyTheme(e.target.value));
    // providers
    on($('provForm'), 'submit', (e) => { e.preventDefault(); saveProviderForm(); });
    // paintings
    on($('paintGo'), 'click', generatePainting);
    on($('paintPrompt'), 'keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); generatePainting(); } });
    // browser
    on($('bGo'), 'click', () => browserGo());
    on($('bUrl'), 'keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); browserGo(); } });
    on($('bStart'), 'click', browserStart);
    on($('bLaunch'), 'click', browserStart);
    on($('bStop'), 'click', browserStop);
    on($('bBack'), 'click', () => browserAction({ action: 'back' }));
    on($('bReload'), 'click', () => browserGo(state.browser.url || ($('bUrl') && $('bUrl').value)));
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
    // drawers
    on($('extBtn'), 'click', () => {
      const p = $('extPanel');
      p.hidden = !p.hidden;
      if (!p.hidden) loadExtensions();
    });
    on($('extClose'), 'click', () => { const p = $('extPanel'); if (p) p.hidden = true; });
    document.querySelectorAll('#extPanel .exttab').forEach((tab) => {
      on(tab, 'click', () => showPane(tab.dataset.pane));
    });
    on($('credBtn'), 'click', () => {
      const p = $('credPanel');
      p.hidden = !p.hidden;
      if (!p.hidden) loadCredentials();
    });
    on($('credClose'), 'click', () => { const p = $('credPanel'); if (p) p.hidden = true; });
    document.querySelectorAll('#credPanel .exttab').forEach((tab) => {
      on(tab, 'click', () => showCredPane(tab.dataset.pane));
    });
    // assistants drawer
    on($('asstClose'), 'click', () => { const p = $('asstPanel'); if (p) p.hidden = true; });
    on($('asstForm'), 'submit', (e) => { e.preventDefault(); saveAssistantForm(e.target); });
    on($('asstDelete'), 'click', () => {
      const id = $('asstForm').dataset.editing || '';
      const rec = state.assistants.find((a) => a.id === id);
      if (!rec) { toast('select "edit" on an assistant first', 'err'); return; }
      if (rec.id === 'agentbox') { toast('the default assistant stays', 'err'); return; }
      state.assistants = state.assistants.filter((a) => a.id !== id);
      store.setJSON('agent_linux_assistants', state.assistants);
      $('asstForm').dataset.editing = '';
      renderAssistantEditor();
      renderAssistantPick();
      toast(`assistant ${rec.name} deleted`);
    });
    // extensions forms
    on($('mcpForm'), 'submit', (e) => { e.preventDefault(); saveMcp(e.target); });
    on($('skillForm'), 'submit', (e) => { e.preventDefault(); saveSkill(e.target); });
    on($('pluginForm'), 'submit', (e) => { e.preventDefault(); savePlugin(e.target); });
    on($('dbRun'), 'click', runDbQuery);
    // skill drop zone
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
    // credentials forms
    on($('keyForm'), 'submit', (e) => { e.preventDefault(); saveKey(e.target); });
    on($('keyImportForm'), 'submit', (e) => { e.preventDefault(); importKey(e.target); });
    on($('hostForm'), 'submit', (e) => { e.preventDefault(); saveHostForm(e.target); });
    on($('acctForm'), 'submit', (e) => { e.preventDefault(); saveAccount(e.target); });
    on($('a_provider'), 'change', () => renderAccountFields());
    // lightbox
    on($('lightbox'), 'click', closeLightbox);
    // palette
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
    // keyboard
    on(global, 'keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && ['1', '2', '3', '4', '5'].indexOf(e.key) >= 0) {
        const views = { 1: 'chats', 2: 'terminal', 3: 'browser', 4: 'paintings', 5: 'settings' };
        e.preventDefault();
        showView(views[e.key]);
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !e.altKey) {
        e.preventDefault();
        palette.open ? paletteClose() : paletteOpen();
        return;
      }
      if (e.key === 'Escape') {
        if (palette.open) { paletteClose(); return; }
        const lb = $('lightbox');
        if (lb && !lb.hidden) { closeLightbox(); return; }
        ['extPanel', 'credPanel', 'asstPanel'].forEach((id) => { const p = $(id); if (p) p.hidden = true; });
        return;
      }
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const key = e.key.toLowerCase();
      if (key === 't') { e.preventDefault(); showView('terminal'); newSession(); }
      else if (key === 'a') { e.preventDefault(); document.body.classList.toggle('list-open'); }
      else if (key === 'p') { e.preventDefault(); palette.open ? paletteClose() : paletteOpen(); }
      else if (key === 'b') { e.preventDefault(); showView('browser'); }
      else if (key === 'r') { e.preventDefault(); renameActive(); }
      else if (key === 'w') { e.preventDefault(); if (state.active) closeSession(state.active); }
    });
    on(global, 'resize', () => { screen.fit(); postResize(); });
    wireSettings();
  }

  // ---- boot -------------------------------------------------------------------------------------------------------------

  async function boot() {
    rememberToken();
    // assistants
    state.assistants = store.getJSON('agent_linux_assistants', null) || [].concat(DEFAULT_ASSISTANTS);
    state.activeAssistant = store.get('agent_linux_assistant', 'agentbox');
    if (!state.assistants.find((a) => a.id === state.activeAssistant)) state.activeAssistant = 'agentbox';
    loadChats();
    loadPaintings();
    state.historyTurns = Math.max(0, Math.min(50, parseInt(store.get('agent_linux_history', '12'), 10) || 12));
    applyTheme(store.get('agent_linux_theme', 'cherry'));
    wire();
    buildTerminal();
    setLink(null, 'linking');
    renderAssistantPick();
    renderAssistantEditor();
    showView(store.get('agent_linux_view', 'chats'));
    try {
      await loadSessions();
      if (!state.sessions.some((s) => !s.closed)) await newSession();
    } catch (err) {
      setLink(false, 'offline');
    }
    loadProviders();
    ping();
    global.setInterval(loadSessions, 20000);
    global.setInterval(ping, 45000);
    renderChat();
    renderPaintings();
  }

  // ---- exports (debug/testing) ------------------------------------------------------------------------------------------

  const AgentLinuxConsole = {
    state, screen, api, json, toast, applyTheme, showView, renderList,
    loadSessions, newSession, closeSession, select,
    ask, regenerate, newTopic, deleteTopic, selectTopic, currentTopic,
    loadProviders, renderProviders, loadModels, saveProviderForm,
    generatePainting, renderPaintings, openLightbox, closeLightbox,
    paletteOpen, paletteClose, paletteRender, paletteCommands,
    loadExtensions, loadCredentials, boot, ping, uploadAttachment,
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