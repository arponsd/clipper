/* Auto Clipper - local web UI (vanilla ES2020, no build step, no CDN).
 * CSP: script-src 'self'; style-src 'self'. So: no inline handlers, no innerHTML for server data,
 * styles only through classes or the CSSOM (el.style.x = ...).
 * Contract: docs/CONTRACT_WEB.md (routes section 4, errors section 3, UI section 6, settings section 8).
 */
'use strict';

(() => {
  // ------------------------------------------------------------------ basics
  const TOKEN = (() => {
    const m = document.querySelector('meta[name="autoclipper-token"]');
    return m ? m.getAttribute('content') || '' : '';
  })();
  const $main = document.getElementById('main');
  const $banner = document.getElementById('banner');
  const $toasts = document.getElementById('toasts');

  const JOB_ID_RE = '[A-Za-z0-9][A-Za-z0-9_-]{0,63}';
  const LANG_NAMES = { en: 'English', hi: 'हिन्दी (Hindi)' };
  const LANG_SHORT = { en: 'EN', hi: 'HI' };
  const JOB_STAGES = ['ingest', 'transcribe', 'signals', 'moments', 'cut'];
  const NOTES_MAX = 1000; // options.notes (server: stages.source_info.NOTES_MAX)
  const CLIP_STAGES = ['script', 'voice', 'captions', 'render'];
  const DEFAULT_EXTS = ['.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.ogv'];
  const DEFAULT_MAX_MB = 4096;
  const DEFAULT_WPS = 3.0;
  const TITLE_SOFT_MAX = 70;
  const MAX_HASHTAGS = 8; // = autoclipper.stages.script.MAX_HASHTAGS (server enforces it too)

  const PROP_KEYS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'htmlFor',
    'indeterminate', 'readOnly', 'multiple', 'open']);

  /** Create an element. Text always goes through textContent / text nodes (never innerHTML). */
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = String(v);
        else if (k === 'on') for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
        else if (PROP_KEYS.has(k)) n[k] = v;
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    append(n, kids);
    return n;
  }
  function append(parent, ...kids) {
    for (const k of kids) {
      if (k === undefined || k === null || k === false) continue;
      if (Array.isArray(k)) append(parent, ...k);
      else if (k instanceof Node) parent.appendChild(k);
      else parent.appendChild(document.createTextNode(String(k)));
    }
    return parent;
  }
  /** Replace children, keeping keyboard focus on the element with the same data-fk key. */
  function swap(container, ...nodes) {
    const a = document.activeElement;
    const key = a && container.contains(a) ? a.getAttribute('data-fk') : null;
    container.replaceChildren();
    append(container, nodes);
    if (key) {
      const n = container.querySelector('[data-fk="' + cssEscape(key) + '"]');
      if (n) n.focus({ preventScroll: true });
    }
  }
  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&');
  }
  function uuid() {
    if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  const pad2 = (n) => String(n).padStart(2, '0');
  function fmtClock(sec) {
    if (typeof sec !== 'number' || !isFinite(sec)) return '?';
    const m = Math.floor(sec / 60);
    const s = sec - m * 60;
    return `${m}:${s < 10 ? '0' : ''}${s.toFixed(1)}`;
  }
  function fmtSecs(sec) {
    return (typeof sec === 'number' && isFinite(sec)) ? `${sec.toFixed(1)} s` : '?';
  }
  function fmtDate(epoch) {
    if (typeof epoch !== 'number') return '';
    try { return new Date(epoch * 1000).toLocaleString(); } catch (e) { return ''; }
  }
  function fmtBytes(n) {
    if (typeof n !== 'number') return '';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  function pct(p) {
    const v = typeof p === 'number' && isFinite(p) ? Math.max(0, Math.min(1, p)) : 0;
    return Math.round(v * 100);
  }
  function humanize(key) {
    const s = String(key).replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  function deepClone(o) { return o === undefined ? undefined : JSON.parse(JSON.stringify(o)); }
  function getPath(obj, path) {
    let cur = obj;
    for (const k of path) { if (cur == null || typeof cur !== 'object') return undefined; cur = cur[k]; }
    return cur;
  }
  function setPath(obj, path, value) {
    let cur = obj;
    for (let i = 0; i < path.length - 1; i++) {
      if (cur[path[i]] == null || typeof cur[path[i]] !== 'object') cur[path[i]] = {};
      cur = cur[path[i]];
    }
    cur[path[path.length - 1]] = value;
  }
  function langAttr(lang) { return lang === 'hi' ? 'hi' : (lang === 'en' ? 'en' : null); }
  function countWords(text) {
    const t = String(text || '').trim();
    if (!t) return 0;
    return t.split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)).length;
  }
  function parseTags(text) {
    return String(text || '').split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  }
  // G5 keywords typed as "word emoji, word, two words 🔥" -> [{word, emoji}]
  const EMOJI_TAIL_RE = /^(.*?[\p{L}\p{M}\p{N}])\s*(\p{Extended_Pictographic}[\p{Extended_Pictographic}\u200d\ufe0f\u{1F3FB}-\u{1F3FF}]*)$/u;
  function parseKeywords(text) {
    return String(text || '').split(/[,\n]+/).map((x) => x.trim()).filter(Boolean).map((part) => {
      const m = EMOJI_TAIL_RE.exec(part);
      return m ? { word: m[1].trim(), emoji: m[2] } : { word: part.replace(/\s+/g, ' '), emoji: '' };
    });
  }
  function formatKeywords(list) {
    return (Array.isArray(list) ? list : []).map((k) => (k.emoji ? `${k.word} ${k.emoji}` : k.word)).join(', ');
  }
  const HOOK_MAX_CHARS = 100; // = autoclipper.stages.caption_style.HOOK_MAX_CHARS
  const HOOK_MAX_WORDS = 12;

  // ------------------------------------------------------------------ banner + toasts
  const Banner = {
    kind: null,
    show(kind, text, reload) {
      if (this.kind === 'auth' && kind !== 'auth') return; // auth wins
      this.kind = kind;
      swap($banner, el('span', { text }),
        reload ? el('button', { type: 'button', class: 'btn btn-small', text: 'Reload page',
          on: { click: () => location.reload() } }) : null);
      $banner.hidden = false;
    },
    clear(kind) {
      if (this.kind !== kind) return;
      this.kind = null;
      $banner.hidden = true;
      $banner.replaceChildren();
    },
  };
  function toast(text, kind) {
    const t = el('div', { class: 'toast' + (kind ? ' ' + kind : ''), role: kind === 'error' ? 'alert' : 'status', text });
    $toasts.appendChild(t);
    setTimeout(() => t.remove(), kind === 'error' ? 8000 : 4000);
  }

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(status, message, errors, data) {
      super(message);
      this.status = status;
      this.errors = errors && typeof errors === 'object' && !Array.isArray(errors) ? errors : {};
      this.data = data;
    }
  }
  function authFailed(status, msg) {
    Banner.show('auth', `The server rejected this page's access token (HTTP ${status}: ${msg}). ` +
      'Reload the page to pick up the current token.', true);
  }
  function errorFromResponse(status, text) {
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    const msg = data && typeof data.message === 'string' && data.message
      ? data.message : `Request failed (HTTP ${status}).`;
    if (status === 401 || status === 403) authFailed(status, msg);
    return new ApiError(status, msg, data && data.errors, data);
  }

  async function api(method, path, opts) {
    const o = opts || {};
    const headers = Object.assign({ 'X-Auth-Token': TOKEN, Accept: 'application/json' }, o.headers || {});
    let body;
    if (o.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(o.body);
    }
    let res;
    try {
      res = await fetch(path, { method, headers, body, signal: o.signal, cache: 'no-store',
        credentials: 'same-origin' });
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      Banner.show('offline', 'Cannot reach the Auto Clipper server. Is `autoclipper serve` still running?');
      throw new ApiError(0, 'Cannot reach the Auto Clipper server.', {});
    }
    Banner.clear('offline');
    const text = await res.text();
    if (!res.ok) throw errorFromResponse(res.status, text);
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    return { status: res.status, data };
  }
  const apiGet = (p, o) => api('GET', p, o).then((r) => r.data);

  /** Raw-body upload with progress (XMLHttpRequest, so we get upload events). */
  function uploadFile(file, onProgress, handle) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/uploads?filename=' + encodeURIComponent(file.name));
      xhr.setRequestHeader('X-Auth-Token', TOKEN);
      xhr.setRequestHeader('Accept', 'application/json');
      const type = file.type && file.type.startsWith('video/') ? file.type : 'application/octet-stream';
      xhr.setRequestHeader('Content-Type', type);
      xhr.upload.addEventListener('progress', (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); });
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); } catch (e) {
            reject(new ApiError(xhr.status, 'The server sent an unreadable upload response.', {}));
          }
        } else reject(errorFromResponse(xhr.status, xhr.responseText));
      });
      xhr.addEventListener('error', () => reject(new ApiError(0, 'Upload failed: the connection to the server was lost.', {})));
      xhr.addEventListener('abort', () => reject(new ApiError(-1, 'Upload cancelled.', {})));
      if (handle) handle.abort = () => xhr.abort();
      xhr.send(file);
    });
  }

  /** Field error display: register fields by dotted path, then show(ApiError | {message, errors}). */
  class FormErrors {
    constructor(summaryEl) {
      this.summary = summaryEl;
      this.fields = new Map();
    }
    register(path, container, errEl) { this.fields.set(path, { container, errEl }); }
    clear() {
      for (const f of this.fields.values()) {
        f.container.classList.remove('invalid');
        f.errEl.textContent = '';
        const ctl = f.container.querySelector('input, select, textarea');
        if (ctl) ctl.removeAttribute('aria-invalid');
      }
      this.summary.replaceChildren();
      this.summary.hidden = true;
    }
    lookup(path) {
      const parts = String(path).split('.');
      for (let i = parts.length; i > 0; i--) {
        const f = this.fields.get(parts.slice(0, i).join('.'));
        if (f) return f;
      }
      return null;
    }
    show(err) {
      this.clear();
      const unmatched = [];
      let first = null;
      for (const [path, msgs] of Object.entries(err.errors || {})) {
        const list = Array.isArray(msgs) ? msgs.map(String) : [String(msgs)];
        const f = this.lookup(path);
        if (f) {
          f.container.classList.add('invalid');
          const ctl = f.container.querySelector('input, select, textarea');
          if (ctl) ctl.setAttribute('aria-invalid', 'true');
          f.errEl.textContent = (f.errEl.textContent ? f.errEl.textContent + ' ' : '') + list.join(' ');
          if (!first) first = f.container;
        } else unmatched.push(`${path || 'request'}: ${list.join(' ')}`);
      }
      append(this.summary, el('strong', { text: err.message || 'Something went wrong.' }));
      if (unmatched.length) append(this.summary, el('ul', null, unmatched.map((t) => el('li', { text: t }))));
      this.summary.hidden = false;
      const focusable = first && first.querySelector('input, select, textarea');
      if (focusable) focusable.focus();
    }
  }
  function errorNotice() { return el('div', { class: 'notice notice-error', role: 'alert', hidden: true }); }

  // ------------------------------------------------------------------ theme
  const THEME_KEY = 'autoclipper.theme';
  function storedTheme() {
    try { const v = localStorage.getItem(THEME_KEY); return ['light', 'dark'].includes(v) ? v : 'auto'; } catch (e) { return 'auto'; }
  }
  function applyTheme(t) {
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
    const btn = document.getElementById('theme-toggle');
    if (btn) {
      const label = t === 'auto' ? 'Auto' : (t === 'light' ? 'Light' : 'Dark');
      btn.setAttribute('aria-label', `Theme: ${label} (click to change)`);
      const l = btn.querySelector('.theme-label');
      if (l) l.textContent = label;
    }
  }
  function initTheme() {
    applyTheme(storedTheme());
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', () => {
      const order = ['auto', 'light', 'dark'];
      const next = order[(order.indexOf(storedTheme()) + 1) % order.length];
      try { if (next === 'auto') localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, next); } catch (e) { /* ignore */ }
      applyTheme(next);
    });
  }

  // ------------------------------------------------------------------ polling
  /** Repeats fn every ms while the view lives. fn returns false to pause (nothing active).
   *  Paused when the tab is hidden; kick() runs it now and un-pauses. */
  function makePoller(ctx, ms, fn) {
    let timer = null; let busy = false; let wanted = true; let stopped = false; let again = false;
    async function tick() {
      timer = null;
      if (stopped || !ctx.alive || document.hidden) return;
      busy = true;
      let keep = true;
      try { keep = await fn(); } catch (e) { keep = !(e && (e.status === 401 || e.status === 403 || e.status === 404)); }
      busy = false;
      if (stopped || !ctx.alive) return;
      if (again) { again = false; wanted = true; schedule(0); return; }
      if (keep === false) { wanted = false; return; }
      schedule(ms);
    }
    function schedule(d) { if (timer) clearTimeout(timer); timer = setTimeout(tick, d); }
    const onVis = () => { if (!document.hidden && wanted && !busy) schedule(0); };
    document.addEventListener('visibilitychange', onVis);
    const p = {
      kick() { wanted = true; if (busy) again = true; else schedule(0); },
      stop() { stopped = true; if (timer) clearTimeout(timer); document.removeEventListener('visibilitychange', onVis); },
      get active() { return wanted; },
    };
    ctx.onCleanup(() => p.stop());
    schedule(0);
    return p;
  }

  /** Global queue state: GET /api/queue every 2 s while something is queued/running. */
  const Queue = {
    snap: null,
    subs: new Set(),
    timer: null,
    busy: false,
    wanted: true,
    dead: false,
    isBusy(s) { const q = s || this.snap; return !!(q && (q.running || (q.queued && q.queued.length))); },
    async tick() {
      this.timer = null;
      if (this.dead || document.hidden) return;
      this.busy = true;
      try {
        const s = await apiGet('/api/queue');
        this.snap = s;
        this.render();
        for (const fn of this.subs) { try { fn(s); } catch (e) { console.error(e); } }
        this.wanted = this.isBusy(s);
      } catch (e) {
        if (e.status === 401 || e.status === 403) this.dead = true;
      }
      this.busy = false;
      if (this.wanted && !this.dead) this.schedule(2000);
    },
    schedule(d) { if (this.timer) clearTimeout(this.timer); this.timer = setTimeout(() => this.tick(), d); },
    kick() { this.wanted = true; if (!this.busy) this.schedule(0); else this.schedule(400); },
    subscribe(ctx, fn) {
      this.subs.add(fn);
      ctx.onCleanup(() => this.subs.delete(fn));
      if (this.snap) fn(this.snap);
    },
    render() {
      const pill = document.getElementById('queue-pill');
      if (!pill) return;
      const s = this.snap;
      const waiting = s && s.queued ? s.queued.length : 0;
      let text = 'Queue idle';
      if (s && s.running) text = waiting ? `1 running · ${waiting} waiting` : '1 running';
      else if (waiting) text = `${waiting} waiting`;
      pill.textContent = text;
      pill.classList.toggle('busy', this.isBusy());
    },
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden && Queue.wanted) Queue.kick(); });

  function taskLabel(t) {
    if (!t) return '';
    if (t.state === 'running') return 'running';
    if (t.state === 'queued') return typeof t.position === 'number' ? `queued · #${t.position} in line` : 'queued';
    return t.state;
  }
  function taskDesc(t) {
    if (t.kind === 'rerender') return `Re-render clip ${pad2(t.clip)} / ${LANG_SHORT[t.lang] || t.lang}`;
    if (t.kind === 'publish') return `Publish clip ${pad2(t.clip)} / ${LANG_SHORT[t.lang] || t.lang}`;
    return 'Process job';
  }
  function chip(status, text) {
    const s = String(status || 'pending').toLowerCase().replace(/[^a-z]/g, '');
    return el('span', { class: 'chip st-' + s, text: text || status });
  }
  function bar(status, progress, label) {
    const s = String(status || 'pending').toLowerCase().replace(/[^a-z]/g, '');
    const v = s === 'done' ? 100 : pct(progress);
    const fill = el('span');
    fill.style.width = v + '%';
    return el('div', { class: 'bar st-' + s, role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100,
      'aria-valuenow': v, 'aria-label': label || 'progress' }, fill);
  }

  async function cancelTask(task, after) {
    if (task.state === 'running' && !confirm('Stop the running task? The job is marked "interrupted" and can be resumed later.')) return;
    try {
      const r = await api('POST', `/api/tasks/${encodeURIComponent(task.id)}/cancel`);
      toast(r.status === 202 ? 'Stop requested; the task stops at the next progress step.' : 'Task cancelled.', 'ok');
    } catch (e) {
      toast(e.message, 'error');
    }
    Queue.kick();
    if (after) after();
  }

  // ------------------------------------------------------------------ settings cache
  let settingsCache = null;
  async function getSettings(force) {
    if (!force && settingsCache) return settingsCache;
    settingsCache = await apiGet('/api/settings');
    return settingsCache;
  }

  // ------------------------------------------------------------------ router
  let current = null;
  let lastHash = '';
  const routes = [
    [/^#\/new$/, viewNew, 'new'],
    [/^#\/jobs$/, viewJobs, 'jobs'],
    [new RegExp(`^#/jobs/(${JOB_ID_RE})$`), viewJob, 'jobs'],
    [new RegExp(`^#/jobs/(${JOB_ID_RE})/clips/(\\d{1,3})/(en|hi)$`), viewClip, 'jobs'],
    [/^#\/settings$/, viewSettings, 'settings'],
    [/^#\/doctor$/, viewDoctor, 'doctor'],
    [/^#\/accounts$/, (ctx) => window.ACPublish.viewAccounts(ctx), 'accounts'], // step 4 (publish.js)
    [/^#\/schedule$/, (ctx) => window.ACSchedule.viewSchedules(ctx), 'schedule'], // G6 (schedule.js)
  ];

  function route(initial) {
    if (current) { current.alive = false; for (const fn of current.cleanups) { try { fn(); } catch (e) { console.error(e); } } }
    const hash = location.hash || '';
    if (!hash || hash === '#' || hash === '#/' || hash === '#main') { history.replaceState(null, '', '#/jobs'); return route(initial); }
    lastHash = hash;
    const ctx = { alive: true, cleanups: [], guard: null };
    ctx.onCleanup = (fn) => ctx.cleanups.push(fn);
    current = ctx;
    $main.replaceChildren();
    let nav = null;
    let matched = false;
    for (const [re, view, n] of routes) {
      const m = hash.match(re);
      if (m) {
        matched = true;
        nav = n;
        try { view(ctx, ...m.slice(1)); } catch (e) { console.error(e); append($main, el('p', { class: 'notice notice-error', text: 'This page failed to load: ' + e.message })); }
        break;
      }
    }
    if (!matched) {
      append($main, el('div', { class: 'card empty' },
        el('h1', { text: 'Page not found' }),
        el('p', { text: 'There is nothing at this address.' }),
        el('a', { class: 'btn', href: '#/jobs', text: 'Go to jobs' })));
    }
    for (const a of document.querySelectorAll('.nav a')) {
      if (a.getAttribute('data-nav') === nav) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    Queue.kick();
    if (!initial) { $main.focus({ preventScroll: true }); window.scrollTo(0, 0); }
  }
  window.addEventListener('hashchange', () => {
    if (location.hash === '#main') { history.replaceState(null, '', lastHash || '#/jobs'); return; }
    if (current && current.guard) {
      const msg = current.guard();
      if (msg && !confirm(msg)) { history.replaceState(null, '', lastHash); return; }
    }
    route(false);
  });
  for (const a of document.querySelectorAll('.skip-link')) {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const m = document.getElementById('main');
      if (m) m.focus();
    });
  }
  window.addEventListener('beforeunload', (e) => {
    if (current && current.guard && current.guard()) { e.preventDefault(); e.returnValue = ''; }
  });

  function pageHead(title, crumbs, ...actions) {
    return el('div', { class: 'page-head' },
      el('div', { class: 'wrap-any' },
        crumbs ? el('div', { class: 'crumbs' }, crumbs) : null,
        el('h1', { text: title })),
      actions.length ? el('div', { class: 'row' }, actions) : null);
  }
  function loading(text) { return el('p', { class: 'loading' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), ' ', text || 'Loading…'); }
  function failBox(e, retry) {
    return el('div', { class: 'notice notice-error', role: 'alert' },
      el('strong', { text: e.message || String(e) }),
      retry ? el('div', null, el('button', { type: 'button', class: 'btn btn-small', text: 'Try again', on: { click: retry } })) : null);
  }

  // ================================================================== NEW JOB
  function viewNew(ctx) {
    let mode = 'file';
    let file = null;
    let cachedUpload = null; // {key, upload_id}: reuse when "Start" is retried with the same file
    let uploading = null;    // {abort}
    let inFlight = false;
    let web = { allowed_extensions: DEFAULT_EXTS, max_upload_mb: DEFAULT_MAX_MB };

    const summary = errorNotice();
    const errs = new FormErrors(summary);

    function fieldWrap(path, label, control, hint, id) {
      const err = el('div', { class: 'field-error', id: id + '-err' });
      const wrap = el('div', { class: 'field' },
        label ? el('label', { for: id, text: label }) : null, control,
        hint ? el('div', { class: 'hint', id: id + '-hint', text: hint }) : null, err);
      control.setAttribute('aria-describedby', [hint ? id + '-hint' : null, id + '-err'].filter(Boolean).join(' '));
      errs.register(path, wrap, err);
      return wrap;
    }

    // --- mode switch
    const btnFile = el('button', { type: 'button', 'aria-pressed': 'true', text: 'Upload a file' });
    const btnUrl = el('button', { type: 'button', 'aria-pressed': 'false', text: 'Paste a URL' });
    const seg = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Source type' }, btnFile, btnUrl);

    // --- file panel
    const fileInput = el('input', { type: 'file', id: 'nj-file', class: 'sr-only', tabindex: '-1', accept: DEFAULT_EXTS.join(',') });
    const fileName = el('div', { class: 'file-name', text: 'No file chosen' });
    const fileInfo = el('div', { class: 'small muted', text: '' });
    const chooseBtn = el('button', { type: 'button', class: 'btn', text: 'Choose file…', on: { click: (e) => { e.stopPropagation(); fileInput.click(); } } });
    const drop = el('div', { class: 'dropzone', tabindex: '0', role: 'button', 'aria-label': 'Choose a video file or drop it here',
      on: {
        click: () => fileInput.click(),
        keydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } },
      } },
    el('p', { text: 'Drop a video file here, or' }), chooseBtn, el('div', { class: 'stack' }, fileName, fileInfo));
    const fileField = fieldWrap('filename', 'Video file', drop, '', 'nj-file');
    fileField.querySelector('label').setAttribute('for', 'nj-file');
    append(fileField, fileInput);
    errs.register('body', fileField, fileField.querySelector('.field-error'));
    errs.register('upload_id', fileField, fileField.querySelector('.field-error'));
    const filePanel = el('div', null, fileField);

    // --- url panel
    const urlInput = el('input', { type: 'url', id: 'nj-url', placeholder: 'https://www.youtube.com/watch?v=…',
      maxlength: '2048', autocomplete: 'off', spellcheck: 'false' });
    const urlPanel = el('div', { hidden: true }, fieldWrap('url', 'Video URL', urlInput, 'http(s) links only: YouTube or any site yt-dlp supports.', 'nj-url'));

    // --- options
    const clipsIn = el('input', { type: 'number', id: 'nj-clips', min: '1', max: '30', step: '1', inputmode: 'numeric' });
    const minIn = el('input', { type: 'number', id: 'nj-min', min: '5', max: '180', step: 'any', inputmode: 'decimal' });
    const maxIn = el('input', { type: 'number', id: 'nj-max', min: '10', max: '180', step: 'any', inputmode: 'decimal' });
    const langEn = el('input', { type: 'checkbox', id: 'nj-lang-en', value: 'en' });
    const langHi = el('input', { type: 'checkbox', id: 'nj-lang-hi', value: 'hi' });
    const langErr = el('div', { class: 'field-error', id: 'nj-langs-err' });
    const langsBox = el('div', { class: 'checks', role: 'group', 'aria-labelledby': 'nj-langs-label', 'aria-describedby': 'nj-langs-err' },
      el('label', { class: 'check' }, langEn, 'English'),
      el('label', { class: 'check', lang: 'hi' }, langHi, 'हिन्दी (Hindi)'));
    const langsField = el('div', { class: 'field' }, el('span', { class: 'field-label', id: 'nj-langs-label', text: 'Languages' }), langsBox, langErr);
    errs.register('options.langs', langsField, langErr);
    // Voice-over: starts at the setting; always sent, and fixed for the job (Settings changes apply to new jobs).
    let voiceoverDefault = true;
    const voIn = el('input', { type: 'checkbox', id: 'nj-voiceover', checked: true });
    const voErr = el('div', { class: 'field-error', id: 'nj-voiceover-err' });
    const voHint = el('div', { class: 'hint', id: 'nj-voiceover-hint', text: 'Off = captions only: no narrator voice, the original audio is kept and the captions show what is said.' });
    voIn.setAttribute('aria-describedby', 'nj-voiceover-hint nj-voiceover-err');
    const voField = el('div', { class: 'field' }, el('label', { class: 'check' }, voIn, 'Voice-over narration'), voHint, voErr);
    errs.register('options.voiceover', voField, voErr);
    // Context / notes: what the video is and who made it; Claude uses it (with the page info of a URL) for
    // the moments, titles, descriptions and hashtags.
    const notesIn = el('textarea', { id: 'nj-notes', rows: '2', maxlength: String(NOTES_MAX),
      placeholder: 'e.g. GTA 6 gameplay by Nemsk: robbing every bank without stopping' });
    const optsFs = el('fieldset', null, el('legend', { text: 'Options' }),
      el('div', { class: 'fields' },
        fieldWrap('options.clips', 'Number of clips', clipsIn, '1–30', 'nj-clips'),
        fieldWrap('options.min_seconds', 'Min length (s)', minIn, '5–180 seconds', 'nj-min'),
        fieldWrap('options.max_seconds', 'Max length (s)', maxIn, '10–180, at least the minimum', 'nj-max'),
        langsField, voField),
      fieldWrap('options.notes', 'Context / notes (optional)', notesIn,
        `What the video is and who made it, up to ${NOTES_MAX} characters. Used with the video page's title and channel for the titles, descriptions and hashtags.`, 'nj-notes'));

    // --- progress + actions
    const upBar = el('div', { class: 'bar upload', role: 'progressbar', 'aria-label': 'Upload progress', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': 0 }, el('span'));
    const upText = el('div', { class: 'small muted', 'aria-live': 'polite' });
    const progress = el('div', { class: 'stack', hidden: true }, upText, upBar);
    const submit = el('button', { type: 'submit', class: 'btn btn-primary', text: 'Start job' });
    const cancelUp = el('button', { type: 'button', class: 'btn', text: 'Cancel upload', hidden: true,
      on: { click: () => { if (uploading) uploading.abort(); } } });
    const settingsNote = el('p', { class: 'small muted' });

    const form = el('form', { class: 'card stack', novalidate: true, 'aria-label': 'New job' },
      el('div', { class: 'row row-between' }, el('h2', { text: 'Source' }), seg),
      filePanel, urlPanel, optsFs, summary, progress,
      el('div', { class: 'row' }, submit, cancelUp), settingsNote);

    append($main, pageHead('New job', null), form);

    function setMode(m) {
      mode = m;
      btnFile.setAttribute('aria-pressed', String(m === 'file'));
      btnUrl.setAttribute('aria-pressed', String(m === 'url'));
      filePanel.hidden = m !== 'file';
      urlPanel.hidden = m !== 'url';
      errs.clear();
    }
    btnFile.addEventListener('click', () => setMode('file'));
    btnUrl.addEventListener('click', () => { setMode('url'); urlInput.focus(); });

    function checkFile(f) {
      const name = f.name || '';
      const dot = name.lastIndexOf('.');
      const ext = dot >= 0 ? name.slice(dot).toLowerCase() : '';
      const exts = (web.allowed_extensions || DEFAULT_EXTS).map((x) => String(x).toLowerCase());
      if (!exts.includes(ext)) return `“${ext || 'no extension'}” files are not allowed. Allowed: ${exts.join(', ')}.`;
      const max = (web.max_upload_mb || DEFAULT_MAX_MB) * 1024 * 1024;
      if (f.size > max) return `This file is ${fmtBytes(f.size)}; the limit is ${web.max_upload_mb || DEFAULT_MAX_MB} MiB (Settings → Web uploads).`;
      if (f.size === 0) return 'This file is empty.';
      return null;
    }
    function setFile(f) {
      file = f || null;
      errs.clear();
      fileName.textContent = file ? file.name : 'No file chosen';
      fileInfo.textContent = file ? fmtBytes(file.size) : '';
      if (file) {
        const problem = checkFile(file);
        if (problem) errs.show({ message: 'This file cannot be used.', errors: { filename: [problem] } });
      }
    }
    fileInput.addEventListener('change', () => setFile(fileInput.files && fileInput.files[0]));

    // drag & drop anywhere on the page while this view is open
    let dragDepth = 0;
    const onDragEnter = (e) => { if (hasFiles(e)) { dragDepth++; document.body.classList.add('dragging'); } };
    const onDragLeave = () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) document.body.classList.remove('dragging'); };
    const onDragOver = (e) => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } };
    const onDrop = (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth = 0;
      document.body.classList.remove('dragging');
      if (inFlight) return;
      const f = e.dataTransfer.files[0];
      if (f) { setMode('file'); setFile(f); }
    };
    function hasFiles(e) { return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files'); }
    document.addEventListener('dragenter', onDragEnter);
    document.addEventListener('dragleave', onDragLeave);
    document.addEventListener('dragover', onDragOver);
    document.addEventListener('drop', onDrop);
    ctx.onCleanup(() => {
      document.removeEventListener('dragenter', onDragEnter);
      document.removeEventListener('dragleave', onDragLeave);
      document.removeEventListener('dragover', onDragOver);
      document.removeEventListener('drop', onDrop);
      document.body.classList.remove('dragging');
      if (uploading) uploading.abort();
    });
    ctx.guard = () => (uploading ? 'An upload is in progress. Leave and cancel it?' : null);

    // defaults from settings
    getSettings(true).then((s) => {
      if (!ctx.alive) return;
      const c = s.clips || {};
      if (c.count != null) clipsIn.value = c.count;
      if (c.min_seconds != null) minIn.value = c.min_seconds;
      if (c.max_seconds != null) maxIn.value = c.max_seconds;
      const langs = Array.isArray(s.languages) ? s.languages : ['en', 'hi'];
      langEn.checked = langs.includes('en');
      langHi.checked = langs.includes('hi');
      voiceoverDefault = s.voiceover !== false;
      voIn.checked = voiceoverDefault;
      if (s.web) web = Object.assign({}, web, s.web);
      fileInput.setAttribute('accept', (web.allowed_extensions || DEFAULT_EXTS).join(','));
      settingsNote.textContent = `Defaults come from Settings. Uploads: ${(web.allowed_extensions || DEFAULT_EXTS).join(' ')}, up to ${web.max_upload_mb} MiB.`;
    }).catch((e) => {
      if (!ctx.alive) return;
      clipsIn.value = 5; minIn.value = 20; maxIn.value = 60; langEn.checked = true; langHi.checked = true;
      settingsNote.textContent = 'Could not load settings (' + e.message + '); using built-in defaults.';
    });

    function collectOptions(clientErrors) {
      const o = {};
      const num = (input, key, integer) => {
        const raw = input.value.trim();
        if (raw === '') return;
        const v = Number(raw);
        if (!isFinite(v)) { clientErrors['options.' + key] = ['Enter a number.']; return; }
        if (integer && !Number.isInteger(v)) { clientErrors['options.' + key] = ['Enter a whole number.']; return; }
        o[key] = v;
      };
      num(clipsIn, 'clips', true);
      num(minIn, 'min_seconds', false);
      num(maxIn, 'max_seconds', false);
      if (o.min_seconds != null && o.max_seconds != null && o.max_seconds < o.min_seconds) {
        clientErrors['options.max_seconds'] = ['Must be at least the minimum length.'];
      }
      const langs = [];
      if (langEn.checked) langs.push('en');
      if (langHi.checked) langs.push('hi');
      if (!langs.length) clientErrors['options.langs'] = ['Pick at least one language.'];
      o.langs = langs;
      o.voiceover = voIn.checked; // always sent: the job keeps the mode shown on the form
      const notes = notesIn.value.trim();
      if (notes.length > NOTES_MAX) clientErrors['options.notes'] = [`At most ${NOTES_MAX} characters.`];
      else if (notes) o.notes = notes;
      return o;
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (inFlight) return;
      errs.clear();
      const clientErrors = {};
      const options = collectOptions(clientErrors);
      let url = null;
      if (mode === 'file') {
        if (!file) clientErrors.filename = ['Choose a video file first.'];
        else { const p = checkFile(file); if (p) clientErrors.filename = [p]; }
      } else {
        url = urlInput.value.trim();
        if (!url) clientErrors.url = ['Paste a video link.'];
        else if (!/^https?:\/\/[^\s/]+/i.test(url)) clientErrors.url = ['Only http:// or https:// links are accepted.'];
      }
      if (Object.keys(clientErrors).length) { errs.show({ message: 'Please fix the highlighted fields.', errors: clientErrors }); return; }

      const key = uuid(); // fresh Idempotency-Key per click
      inFlight = true;
      submit.disabled = true;
      seg.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      try {
        let body;
        if (mode === 'file') {
          const fkey = `${file.name}|${file.size}|${file.lastModified}`;
          let uploadId = cachedUpload && cachedUpload.key === fkey ? cachedUpload.upload_id : null;
          if (!uploadId) {
            progress.hidden = false;
            cancelUp.hidden = false;
            uploading = {};
            const started = Date.now();
            const onProg = (loaded, total) => {
              const p = total ? Math.round((loaded / total) * 100) : 0;
              upBar.firstChild.style.width = p + '%';
              upBar.setAttribute('aria-valuenow', String(p));
              const secs = (Date.now() - started) / 1000;
              const rate = secs > 0.5 ? ` · ${fmtBytes(loaded / secs)}/s` : '';
              upText.textContent = `Uploading ${p}% (${fmtBytes(loaded)} of ${fmtBytes(total)})${rate}`;
            };
            onProg(0, file.size);
            const up = await uploadFile(file, onProg, uploading);
            uploading = null;
            cancelUp.hidden = true;
            upText.textContent = `Uploaded ${fmtBytes(up.size)}. Creating the job…`;
            uploadId = up.upload_id;
            cachedUpload = { key: fkey, upload_id: uploadId };
          }
          body = { upload_id: uploadId, options };
        } else {
          body = { url, options };
        }
        const r = await api('POST', '/api/jobs', { body, headers: { 'Idempotency-Key': key } });
        cachedUpload = null;
        toast(r.status === 200 ? 'This job already exists; opening it.' : 'Job queued.', 'ok');
        Queue.kick();
        if (ctx.alive && r.data && r.data.id) location.hash = '#/jobs/' + r.data.id;
      } catch (err) {
        uploading = null;
        cancelUp.hidden = true;
        if (err.status === 404 && mode === 'file') {
          cachedUpload = null;
          err.errors = Object.assign({ filename: ['The uploaded file is gone; press Start job to upload it again.'] }, err.errors);
        }
        if (err.status === 413) err.errors = Object.assign({ filename: [err.message] }, err.errors);
        if (err.status === -1) { upText.textContent = 'Upload cancelled.'; }
        if (ctx.alive) errs.show(err);
      } finally {
        inFlight = false;
        if (ctx.alive) {
          submit.disabled = false;
          seg.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        }
      }
    });
  }

  // ================================================================== JOBS LIST
  function viewJobs(ctx) {
    const LIMIT = 25;
    let offset = 0;
    let lastJson = '';
    const queueBox = el('div');
    const listBox = el('div', { 'aria-live': 'off' });
    const pager = el('div', { class: 'pager' });
    append($main,
      pageHead('Jobs', null, el('a', { class: 'btn btn-primary', href: '#/new', text: 'New job' })),
      el('section', { class: 'card', 'aria-labelledby': 'q-h' }, el('h2', { id: 'q-h', text: 'Queue' }), queueBox),
      el('section', { class: 'card', 'aria-labelledby': 'j-h' }, el('h2', { id: 'j-h', text: 'All jobs' }), listBox, pager));
    listBox.appendChild(loading());

    if (!Queue.snap) queueBox.appendChild(loading());

    const poll = makePoller(ctx, 2000, async () => {
      let data;
      try {
        data = await apiGet(`/api/jobs?limit=${LIMIT}&offset=${offset}`);
      } catch (e) {
        if (ctx.alive) swap(listBox, failBox(e, () => poll.kick()));
        lastJson = '';
        throw e;
      }
      if (!ctx.alive) return false;
      const json = JSON.stringify(data);
      if (json !== lastJson) {
        lastJson = json;
        renderList(data);
      }
      const active = (data.jobs || []).some((j) => j.status === 'queued' || j.status === 'running' || j.task);
      return active || Queue.isBusy();
    });
    Queue.subscribe(ctx, (s) => {
      renderQueue(queueBox, s, () => poll.kick());
      if (Queue.isBusy(s) && !poll.active) poll.kick();
    });

    function renderList(data) {
      const jobs = data.jobs || [];
      if (!jobs.length) {
        swap(listBox, el('div', { class: 'empty' },
          el('p', { text: offset ? 'No more jobs.' : 'No jobs yet.' }),
          el('a', { class: 'btn btn-primary', href: '#/new', text: 'Create your first job' })));
      } else {
        swap(listBox, el('ul', { class: 'job-list' }, jobs.map(jobItem)));
      }
      const total = typeof data.total === 'number' ? data.total : jobs.length;
      swap(pager,
        offset > 0 ? el('button', { type: 'button', class: 'btn btn-small', 'data-fk': 'prev', text: '← Newer',
          on: { click: () => { offset = Math.max(0, offset - LIMIT); lastJson = ''; poll.kick(); } } }) : null,
        total > LIMIT ? el('span', { class: 'small muted', text: `${offset + 1}–${Math.min(offset + LIMIT, total)} of ${total}` }) : null,
        offset + LIMIT < total ? el('button', { type: 'button', class: 'btn btn-small', 'data-fk': 'next', text: 'Older →',
          on: { click: () => { offset += LIMIT; lastJson = ''; poll.kick(); } } }) : null);
    }

    function jobItem(j) {
      const status = j.interrupted ? 'interrupted' : j.status;
      const langs = Array.isArray(j.langs) ? j.langs.map((l) => LANG_SHORT[l] || l).join(', ') : '';
      return el('li', { class: 'job-item' },
        el('div', { class: 'stack' },
          el('a', { class: 'job-link', href: '#/jobs/' + j.id, 'data-fk': 'job-' + j.id, text: j.id }),
          el('div', { class: 'job-source', text: j.source || '' }),
          el('div', { class: 'job-meta' },
            el('span', { text: fmtDate(j.created_at) }),
            el('span', { text: `${j.clip_count || 0} clip${j.clip_count === 1 ? '' : 's'}${langs ? ' × ' + langs : ''}` }),
            el('span', { text: `${j.rendered || 0} rendered` }),
            j.failed ? el('span', { class: 'field-error', text: `${j.failed} failed` }) : null),
          j.error ? el('div', { class: 'small field-error', text: j.error }) : null),
        el('div', { class: 'job-side' },
          chip(status),
          j.task ? chip(j.task.state, taskLabel(j.task)) : null));
    }
  }

  function renderQueue(box, s, after) {
    if (!s) return;
    const items = [];
    if (s.running) items.push(s.running);
    for (const t of s.queued || []) items.push(t);
    const recent = (s.recent || []).slice(0, 10);
    swap(box,
      items.length ? el('ul', { class: 'queue-list' }, items.map((t) => el('li', { class: 'queue-item' },
        chip(t.state, taskLabel(t)),
        el('span', { class: 'grow' }, taskDesc(t), ' · ', el('a', { href: '#/jobs/' + t.job_id, 'data-fk': 'qj-' + t.id, text: t.job_id })),
        el('button', { type: 'button', class: 'btn btn-small btn-danger', 'data-fk': 'qc-' + t.id,
          text: t.state === 'running' ? 'Stop' : 'Cancel', 'aria-label': `${t.state === 'running' ? 'Stop' : 'Cancel'} ${taskDesc(t)} for job ${t.job_id}`,
          on: { click: () => cancelTask(t, after) } }))))
        : el('p', { class: 'muted', text: 'Nothing is running. New jobs and re-renders run one at a time, in order.' }),
      recent.length ? el('details', { class: 'small' },
        el('summary', { text: `Recently finished (${recent.length})` }),
        el('ul', { class: 'queue-list' }, recent.map((t) => el('li', { class: 'queue-item' },
          chip(t.state),
          el('span', { class: 'grow' }, taskDesc(t), ' · ', el('a', { href: '#/jobs/' + t.job_id, text: t.job_id }),
            t.error ? el('span', { class: 'field-error', text: ' - ' + t.error }) : null),
          el('span', { class: 'muted', text: t.finished_at ? fmtDate(t.finished_at) : '' }))))) : null);
  }

  // ================================================================== JOB DETAIL
  function sourceInfoRows(si) {
    if (!si) return [];
    const rows = [];
    const what = [si.title, si.uploader ? 'by ' + si.uploader : '', si.game ? '(video game footage)' : '']
      .filter(Boolean).join(' ');
    if (what) rows.push(el('dt', { text: 'Video' }), el('dd', { text: what }));
    if (si.notes) rows.push(el('dt', { text: 'Context / notes' }), el('dd', { class: 'pre-wrap', text: si.notes }));
    return rows;
  }
  function stageBox(name, st) {
    const s = st || { status: 'pending', progress: 0, message: '' };
    return el('div', { class: 'stage is-' + s.status },
      el('div', { class: 'stage-head' }, el('span', { class: 'stage-name', text: name }), chip(s.status)),
      bar(s.status, s.progress, `${name} progress`),
      el('div', { class: 'row row-between' },
        el('span', { class: 'stage-pct', text: s.status === 'skipped' ? (s.message === 'voice-over off' ? 'off' : 'skipped') : (s.status === 'done' ? 100 : pct(s.progress)) + '%' })),
      s.message ? el('div', { class: 'stage-msg', text: s.message }) : null);
  }
  function miniStage(name, st) {
    const s = st || { status: 'pending', progress: 0, message: '' };
    const v = s.status === 'done' ? 100 : pct(s.progress);
    return el('div', { class: 'mini-stage is-' + s.status, title: `${name}: ${s.status} ${v}%${s.message ? ' - ' + s.message : ''}` },
      el('div', { class: 'label' }, el('b', { text: name }), el('span', { text: s.status === 'pending' ? '' : s.status === 'skipped' ? (s.message === 'voice-over off' ? 'off' : 'skipped') : v + '%' })),
      bar(s.status, s.progress, `${name} ${s.status}`));
  }
  function clipLangStatus(cl) {
    if (cl.task) return cl.task.state;
    if (cl.error) return 'failed';
    if (cl.final) return 'done';
    const sts = CLIP_STAGES.map((k) => (cl.stages && cl.stages[k] ? cl.stages[k].status : 'pending'));
    if (sts.includes('running')) return 'running';
    if (sts.includes('failed')) return 'failed';
    return 'pending';
  }

  function captionsText(cl) {
    if (!cl || !cl.caption_engine) return '';
    if (cl.caption_engine === 'hyperframes') return `Captions: HyperFrames${cl.caption_style ? ' (' + cl.caption_style + ')' : ''}`;
    return 'Captions: ASS';
  }
  function voiceoverChanged(cl) {
    return !!cl && cl.final && typeof cl.rendered_voiceover === 'boolean' && typeof cl.voiceover === 'boolean' && cl.rendered_voiceover !== cl.voiceover;
  }
  function staleText(cl) {
    if (voiceoverChanged(cl)) return `Voice-over turned ${cl.voiceover ? 'on' : 'off'} - re-render to apply.`;
    if (cl && cl.audio_stale) return 'Made with an older audio mix or other audio settings - re-render to apply (or use "Apply & re-render" on the Mode row).';
    if (cl && cl.style_stale) return 'Caption style, hook or keywords changed - re-render to apply.';
    return 'Caption engine/style changed - re-render to apply.';
  }
  function audioText(cl) {
    const a = cl && cl.audio;
    if (!a || a.mix === 'original') return '';
    const parts = [];
    if (a.background === 'no_vocals') parts.push('background: vocals removed');
    if (a.background_note) parts.push('background ' + a.background_note);
    if (typeof a.trimmed_to === 'number') parts.push(`trimmed to ${fmtSecs(a.trimmed_to)} (short narration${typeof a.clip_duration === 'number' ? ', clip ' + fmtSecs(a.clip_duration) : ''})`);
    return parts.length ? 'Audio: ' + parts.join('; ') : '';
  }
  function audioLabel(cl) {
    const t = audioText(cl);
    return t ? el('p', { class: 'small muted audio-info', text: t }) : null;
  }
  function captionsLabel(cl) {
    const t = captionsText(cl);
    return t ? el('p', { class: 'small muted caption-engine', text: t }) : null;
  }

  function viewJob(ctx, id) {
    let lastJson = '';
    let job = null;
    const head = el('div');
    const body = el('div');
    const logPre = el('pre', { class: 'log', tabindex: '0', 'aria-label': 'Job log' });
    const logDetails = el('details', { class: 'card' },
      el('summary', null, el('strong', { text: 'Log' }), el('span', { class: 'muted small', text: ' (last 200 lines)' })),
      logPre);
    append($main, head, body, logDetails);
    body.appendChild(loading());
    logDetails.addEventListener('toggle', () => { if (logDetails.open) loadLog(); });

    async function loadLog() {
      try {
        const d = await apiGet(`/api/jobs/${encodeURIComponent(id)}/log?tail=200`);
        if (!ctx.alive) return;
        const lines = d && Array.isArray(d.lines) ? d.lines : [];
        const atBottom = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 30;
        logPre.textContent = lines.length ? lines.join('\n') : 'No log lines yet.';
        if (atBottom) logPre.scrollTop = logPre.scrollHeight;
      } catch (e) {
        if (ctx.alive) logPre.textContent = 'Could not load the log: ' + e.message;
      }
    }

    const poll = makePoller(ctx, 1500, async () => {
      let d;
      try {
        d = await apiGet(`/api/jobs/${encodeURIComponent(id)}`);
      } catch (e) {
        if (!ctx.alive) return false;
        if (e.status === 404) {
          swap(head, pageHead('Job not found', el('a', { href: '#/jobs', text: '← Jobs' })));
          swap(body, el('div', { class: 'notice notice-error', text: e.message }));
          logDetails.hidden = true;
        } else swap(body, failBox(e, () => poll.kick()));
        lastJson = '';
        throw e;
      }
      if (!ctx.alive) return false;
      job = d;
      const json = JSON.stringify(d);
      if (json !== lastJson) { lastJson = json; render(d); }
      if (logDetails.open) loadLog();
      return isActive(d);
    });
    Queue.subscribe(ctx, (s) => {
      if (!job) return;
      const mine = [s.running].concat(s.queued || []).some((t) => t && t.job_id === id);
      if (mine && !poll.active) poll.kick();
    });

    function isActive(d) {
      if (d.status === 'queued' || d.status === 'running' || d.task) return true;
      return (d.clips || []).some((c) => Object.values(c.langs || {}).some((cl) => cl.task));
    }

    async function resume() {
      try {
        const r = await api('POST', `/api/jobs/${encodeURIComponent(id)}/resume`);
        toast(r.status === 200 ? 'This job is already queued.' : 'Job re-queued.', 'ok');
      } catch (e) { toast(e.message, 'error'); }
      Queue.kick();
      poll.kick();
    }

    async function setMode(on, d) {
      const current = d.voiceover !== false;
      const unfinished = d.status !== 'done' || d.interrupted || (d.failed || 0) > 0;
      const finish = unfinished ? ' The job is not finished, so this also runs its remaining steps (narration scripts, voices, renders) for every clip.' : '';
      const msg = on === current
        ? `Re-render the clips of this job that are out of date (made in the other mode, or with older audio or caption settings)?${finish}`
        : (on
          ? 'Switch this job to voice-over + captions and re-render its clips? Clips without a narration get one written (language model), then a voice (Voicebox).'
          : 'Switch this job to original audio + captions and re-render its clips? The narration voice is kept on disk, so switching back reuses it.') + finish;
      if (!confirm(msg)) return;
      try {
        const r = await api('PUT', `/api/jobs/${encodeURIComponent(id)}/mode`, { body: { voiceover: on } });
        const data = r.data || {};
        let text;
        if (!data.task) text = 'Every clip is already up to date in this mode.';
        else if (r.status !== 202) text = 'A run of this job is already queued.';
        else text = data.changed ? 'Mode changed; re-render queued.' : 'Re-render queued for the clips that are out of date.';
        toast(text, 'ok');
      } catch (e) { toast(e.message, 'error'); }
      Queue.kick();
      poll.kick();
    }

    function render(d) {
      const status = d.interrupted ? 'interrupted' : d.status;
      const canResume = !d.task && (d.status === 'failed' || d.status === 'queued' || d.status === 'running' || d.interrupted || (d.failed || 0) > 0);
      swap(head, pageHead(d.id, el('a', { href: '#/jobs', text: '← Jobs' }),
        chip(status),
        canResume ? el('button', { type: 'button', class: 'btn btn-primary', 'data-fk': 'resume', text: d.interrupted ? 'Resume (interrupted)' : 'Resume',
          on: { click: resume } }) : null,
        d.task ? el('button', { type: 'button', class: 'btn btn-danger', 'data-fk': 'cancel',
          text: d.task.state === 'running' ? 'Stop' : 'Cancel', on: { click: () => cancelTask(d.task, () => poll.kick()) } }) : null));

      const opts = d.options;
      const optText = opts ? [
        opts.clips != null ? `${opts.clips} clips` : null,
        (opts.min_seconds != null || opts.max_seconds != null) ? `${opts.min_seconds != null ? opts.min_seconds : '…'}–${opts.max_seconds != null ? opts.max_seconds : '…'} s` : null,
        Array.isArray(opts.langs) ? opts.langs.map((l) => LANG_SHORT[l] || l).join(', ') : null,
      ].filter(Boolean).join(' · ') || 'settings defaults' : 'settings defaults';
      const voText = d.voiceover === false ? 'off - captions only, original audio' : 'on';
      const voFrom = ' (switch it for this job with Mode below; the Settings switch applies to new jobs)';
      const modeSel = el('select', { id: 'job-mode', 'aria-label': 'Mode for this job', disabled: !!d.task },
        el('option', { value: 'on', text: 'Voice-over + captions', selected: d.voiceover !== false }),
        el('option', { value: 'off', text: 'Original audio + captions', selected: d.voiceover === false }));
      const modeBtn = el('button', { type: 'button', class: 'btn btn-small', 'data-fk': 'mode', disabled: !!d.task, text: 'Apply & re-render',
        on: { click: () => setMode(modeSel.value === 'on', d) } });
      const anyStale = (d.clips || []).some((c) => Object.values(c.langs || {}).some((cl) => cl && cl.render_stale));
      const unfinishedJob = d.status !== 'done' || d.interrupted || (d.failed || 0) > 0;
      const modeHint = () => {
        if (d.task) return 'Wait for the current task to finish.';
        const same = (modeSel.value === 'on') === (d.voiceover !== false);
        if (!same) return 'Switches the mode and re-renders every clip.' + (unfinishedJob ? ' The job is unfinished: its remaining steps run too.' : '');
        if (unfinishedJob) return 'Finishes the job and re-renders out-of-date clips.';
        return anyStale ? 'Re-renders the clips that are out of date.' : 'Every clip is up to date in this mode.';
      };
      const hintEl = el('span', { class: 'small muted', text: modeHint() });
      const syncMode = () => {
        const same = (modeSel.value === 'on') === (d.voiceover !== false);
        modeBtn.disabled = !!d.task || (same && !unfinishedJob && !anyStale);
        hintEl.textContent = modeHint();
      };
      modeSel.addEventListener('change', syncMode);
      syncMode();
      const modeRow = d.clips && d.clips.length ? el('div', { class: 'row mode-row' }, modeSel, modeBtn, hintEl) : null;

      const info = el('section', { class: 'card' },
        el('dl', { class: 'kv' },
          el('dt', { text: 'Source' }), el('dd', { text: d.source || '' }),
          el('dt', { text: 'Created' }), el('dd', { text: fmtDate(d.created_at) }),
          el('dt', { text: 'Options' }), el('dd', { text: optText }),
          ...sourceInfoRows(d.source_info),
          el('dt', { text: 'Voice-over' }), el('dd', null, el('span', { text: voText + voFrom }), modeRow),
          el('dt', { text: 'Output' }), el('dd', { text: `${d.rendered || 0} rendered, ${d.failed || 0} failed` }),
          d.task ? el('dt', { text: 'Task' }) : null,
          d.task ? el('dd', null, chip(d.task.state, taskLabel(d.task)),
            d.task.state === 'queued' && d.task.position > 1 ? el('span', { class: 'muted small', text: ` waiting for ${d.task.position - 1} task(s) ahead` }) : null) : null),
        d.interrupted ? el('p', { class: 'notice notice-warn', text: 'This job was interrupted (server stopped or task cancelled). Press Resume to continue where it left off.' }) : null,
        d.error && !d.interrupted ? el('p', { class: 'notice notice-error', role: 'alert', text: d.error }) : null);

      const stages = el('section', { class: 'card', 'aria-labelledby': 'st-h' },
        el('h2', { id: 'st-h', text: 'Job stages' }),
        el('div', { class: 'stages' }, JOB_STAGES.map((k) => stageBox(k, d.stages && d.stages[k]))));

      const clips = d.clips || [];
      const clipsSec = el('section', { class: 'card', 'aria-labelledby': 'cl-h' },
        el('h2', { id: 'cl-h', text: `Clips (${clips.length})` }),
        clips.length ? el('div', { class: 'clip-grid' }, clips.map((c) => clipRow(d, c)))
          : el('p', { class: 'muted', text: d.status === 'done' ? 'No clips were produced.' : 'Clips appear here once moments are picked and cut.' }));
      swap(body, info, stages, clipsSec);
    }

    function clipRow(d, c) {
      const m = c.moment || {};
      const dur = typeof m.duration === 'number' ? m.duration : (m.end - m.start);
      const langs = Object.keys(c.langs || {});
      return el('article', { class: 'clip-row', 'aria-label': `Clip ${pad2(c.index)}` },
        el('div', { class: 'clip-row-head' },
          el('h3', { text: `Clip ${pad2(c.index)}` }),
          el('span', { class: 'small muted', text: `${fmtClock(m.start)}–${fmtClock(m.end)} (${fmtSecs(dur)})${typeof m.score === 'number' ? ' · score ' + m.score.toFixed(2) : ''}` }),
          c.cut ? chip('done', 'cut') : chip('pending', 'not cut')),
        m.hook ? el('p', { class: 'clip-hook', text: '“' + m.hook + '”' }) : null,
        el('div', { class: 'clip-langs' }, langs.map((lang) => langCell(d, c, lang, c.langs[lang]))));
    }

    function langCell(d, c, lang, cl) {
      const st = clipLangStatus(cl);
      const title = (cl.metadata && cl.metadata.title) || (cl.script && cl.script.title) || '';
      const reviewHref = `#/jobs/${d.id}/clips/${c.index}/${lang}`;
      return el('div', { class: 'lang-cell' },
        el('div', { class: 'lang-cell-head' },
          el('strong', { text: LANG_NAMES[lang] || lang, lang: langAttr(lang) }),
          chip(st, cl.task ? taskLabel(cl.task) : st)),
        title ? el('p', { class: 'small wrap-any', lang: langAttr(lang), text: title }) : null,
        el('div', { class: 'mini-stages' }, CLIP_STAGES.map((k) => miniStage(k, cl.stages && cl.stages[k]))),
        cl.error ? el('p', { class: 'small field-error', role: 'alert', text: cl.error }) : null,
        cl.voice_stale ? el('p', { class: 'small notice notice-warn', text: 'Narration changed - re-render to regenerate the voice.' }) : null,
        cl.warning ? el('p', { class: 'small notice notice-warn', role: 'status', text: cl.warning }) : null,
        cl.render_stale ? el('p', { class: 'small notice notice-info', text: staleText(cl) }) : null,
        captionsLabel(cl),
        audioLabel(cl),
        el('div', { class: 'row' },
          (cl.script || cl.final) ? el('a', { class: 'btn btn-small', href: reviewHref, 'data-fk': `rv-${c.index}-${lang}`, text: 'Review & edit' }) : null,
          cl.final && cl.download_url ? el('a', { class: 'btn btn-small', href: cl.download_url + '?token=' + encodeURIComponent(TOKEN),
            download: '', 'data-fk': `dl-${c.index}-${lang}`, text: 'Download' }) : null,
          cl.task ? el('button', { type: 'button', class: 'btn btn-small btn-danger', 'data-fk': `cx-${c.index}-${lang}`,
            text: cl.task.state === 'running' ? 'Stop' : 'Cancel', on: { click: () => cancelTask(cl.task, () => poll.kick()) } }) : null));
    }
  }

  // ================================================================== CLIP REVIEW
  function viewClip(ctx, id, nStr, lang) {
    const n = Number(nStr);
    const hi = lang === 'hi';
    let settings = null;
    let job = null;
    let clip = null;
    let entry = null;
    let saved = null;          // last ClipScript from the server
    let bust = Date.now();
    let shownSrc = '';
    let taskId = null;
    let busySaving = false;

    const head = el('div');
    const content = el('div');
    append($main, head, content);
    content.appendChild(loading());
    swap(head, pageHead(`Clip ${pad2(n)} · ${LANG_NAMES[lang] || lang}`,
      [el('a', { href: '#/jobs', text: 'Jobs' }), ' / ', el('a', { href: '#/jobs/' + id, text: id })]));

    // ----- player column
    const video = el('video', { controls: true, preload: 'metadata', playsinline: true });
    const playerEmpty = el('div', { class: 'player-empty', text: 'Not rendered yet.' });
    const playerWrap = el('div', { class: 'player-wrap' }, playerEmpty);
    const download = el('a', { class: 'btn', text: 'Download MP4', download: '', hidden: true });
    const rerenderBtn = el('button', { type: 'button', class: 'btn btn-primary', text: 'Re-render' });
    const taskLine = el('div', { class: 'small row', 'aria-live': 'polite' });
    const stagesBox = el('div', { class: 'mini-stages' });
    const voiceInfo = el('dl', { class: 'kv' });
    const clipErr = el('div');
    const navBox = el('div', { class: 'row row-between' });
    const playerCol = el('section', { class: 'card stack', 'aria-label': 'Rendered clip' },
      playerWrap, el('div', { class: 'row' }, rerenderBtn, download), taskLine, stagesBox, clipErr, voiceInfo, navBox);

    // ----- editor column
    const summary = errorNotice();
    const errs = new FormErrors(summary);
    function fieldWrap(path, label, control, extra, idBase) {
      const err = el('div', { class: 'field-error', id: idBase + '-err' });
      control.id = idBase;
      control.setAttribute('aria-describedby', idBase + '-err' + (extra ? ' ' + idBase + '-extra' : ''));
      if (extra) extra.id = idBase + '-extra';
      const w = el('div', { class: 'field' }, el('label', { for: idBase, text: label }), control, extra, err);
      errs.register(path, w, err);
      return w;
    }
    const narration = el('textarea', { class: 'narration', lang: langAttr(lang), maxlength: '5000', spellcheck: hi ? 'false' : 'true' });
    const narrCount = el('div', { class: 'counter', 'aria-live': 'polite' });
    const voOffNote = el('p', { class: 'notice notice-info', hidden: true, text: 'Voice-over: off. This clip keeps its original audio and the captions show what is said, so there is no narration to edit. The title, description and hashtags are still used when publishing.' });
    const title = el('input', { type: 'text', lang: langAttr(lang), maxlength: '200' });
    const titleCount = el('div', { class: 'counter' });
    const desc = el('textarea', { lang: langAttr(lang), maxlength: '5000', rows: '4' });
    const tags = el('input', { type: 'text', lang: langAttr(lang), spellcheck: 'false' });
    const tagPreview = el('div', { class: 'tag-preview', lang: langAttr(lang), 'aria-hidden': 'true' });
    const tagHelp = el('div', null, el('div', { class: 'hint', text: 'Separate with spaces or commas (up to 8).' }), tagPreview);
    const staleNote = el('p', { class: 'notice notice-warn', hidden: true, text: 'Narration changed - re-render to regenerate the voice.' });
    // G4 hook line + G5 keywords
    const hookIn = el('input', { type: 'text', lang: langAttr(lang), maxlength: String(HOOK_MAX_CHARS), spellcheck: hi ? 'false' : 'true' });
    const hookCount = el('div', { class: 'counter', 'aria-live': 'polite' });
    const hookHelp = el('div', null, el('div', { class: 'hint', text: 'Shown big at the top for the first seconds of the clip (Settings › Hook text). About 8 words reads best. Empty = no hook.' }), hookCount);
    const kwIn = el('input', { type: 'text', lang: langAttr(lang), spellcheck: 'false' });
    const kwPreview = el('div', { class: 'tag-preview', lang: langAttr(lang), 'aria-hidden': 'true' });
    const kwHelp = el('div', null, el('div', { class: 'hint', text: 'Words to highlight in the captions, separated by commas. An emoji after a word (e.g. "clutch 🔥") is shown after it with the HyperFrames caption engine only.' }), kwPreview);
    const styleNote = el('p', { class: 'notice notice-info', hidden: true, text: 'Hook or keywords changed - re-render to apply.' });
    const serverNote = el('p', { class: 'notice notice-info', hidden: true });
    const saveBtn = el('button', { type: 'submit', class: 'btn btn-primary', text: 'Save' });
    const revertBtn = el('button', { type: 'button', class: 'btn', text: 'Revert' });
    const dirtyLabel = el('span', { class: 'small muted', 'aria-live': 'polite' });
    const narrField = fieldWrap('narration', 'Narration', narration, narrCount, 'cr-narr');
    function voiceoverOn() { return !(entry && entry.voiceover === false); }
    const form = el('form', { class: 'card stack', novalidate: true, 'aria-label': 'Script and metadata' },
      el('h2', { text: 'Script & metadata' }),
      serverNote,
      voOffNote,
      narrField,
      staleNote,
      fieldWrap('title', 'Title', title, titleCount, 'cr-title'),
      fieldWrap('description', 'Description', desc, null, 'cr-desc'),
      fieldWrap('hashtags', 'Hashtags', tags, tagHelp, 'cr-tags'),
      fieldWrap('hook', 'On-screen hook', hookIn, hookHelp, 'cr-hook'),
      fieldWrap('keywords', 'Keywords', kwIn, kwHelp, 'cr-kw'),
      styleNote,
      summary,
      el('div', { class: 'row' }, saveBtn, revertBtn, dirtyLabel));

    function wps() {
      const v = settings && settings.script ? settings.script['wps_' + lang] : undefined;
      return typeof v === 'number' && v > 0 ? v : DEFAULT_WPS;
    }
    function clipDuration() {
      const m = clip && clip.moment;
      if (!m) return null;
      return typeof m.duration === 'number' ? m.duration : (m.end - m.start);
    }
    function formValue() {
      return { narration: narration.value, title: title.value, description: desc.value, hashtags: parseTags(tags.value),
        hook: hookIn.value.replace(/\s+/g, ' ').trim(), keywords: parseKeywords(kwIn.value) };
    }
    function isDirty() {
      if (!saved) return false;
      const f = formValue();
      return f.narration !== saved.narration || f.title !== saved.title || f.description !== (saved.description || '') ||
        JSON.stringify(f.hashtags) !== JSON.stringify(saved.hashtags || []) || f.hook !== (saved.hook || '') ||
        JSON.stringify(f.keywords) !== JSON.stringify(saved.keywords || []);
    }
    function updateCounters() {
      const w = countWords(narration.value);
      const r = wps();
      const est = w / r;
      const dur = clipDuration();
      let note = '';
      let over = false;
      if (dur) {
        const maxSpeed = settings && settings.audio && typeof settings.audio.max_speedup === 'number' ? settings.audio.max_speedup : 1.25;
        if (est > dur * maxSpeed) { note = ' - too long: will be shortened or trimmed'; over = true; } else if (est > dur) { note = ' - a bit long: voice will be sped up'; over = true; } else if (est < dur * 0.6) note = ' - short: the end of the clip will have no narration';
      }
      narrCount.textContent = `${w} words · about ${est.toFixed(1)} s at ${r} words/s${dur ? ` · clip ${dur.toFixed(1)} s` : ''}${note}`;
      narrCount.classList.toggle('over', over);
      const tl = [...title.value].length;
      titleCount.textContent = `${tl}/${TITLE_SOFT_MAX}${tl > TITLE_SOFT_MAX ? ' - will be shortened to 70 on save' : ''}`;
      titleCount.classList.toggle('over', tl > TITLE_SOFT_MAX);
      const list = parseTags(tags.value);
      swap(tagPreview, list.slice(0, MAX_HASHTAGS).map((t) => el('span', { text: t })));
      const hw = countWords(hookIn.value);
      const hl = [...hookIn.value.trim()].length;
      hookCount.textContent = hl ? `${hw} words · ${hl}/${HOOK_MAX_CHARS} characters${hw > 8 ? ' - long for a hook' : ''}` : 'No hook: nothing is shown.';
      hookCount.classList.toggle('over', hw > 8);
      swap(kwPreview, parseKeywords(kwIn.value).map((k) => el('span', { text: k.emoji ? `${k.word} ${k.emoji}` : k.word })));
      dirtyLabel.textContent = isDirty() ? 'Unsaved changes' : '';
    }
    for (const i of [narration, title, desc, tags, hookIn, kwIn]) i.addEventListener('input', updateCounters);

    function fillForm(s) {
      saved = { narration: s.narration || '', title: s.title || '', description: s.description || '', hashtags: Array.isArray(s.hashtags) ? s.hashtags.slice() : [],
        hook: s.hook || '', keywords: Array.isArray(s.keywords) ? s.keywords.map((k) => ({ word: k.word, emoji: k.emoji || '' })) : [] };
      narration.value = saved.narration;
      title.value = saved.title;
      desc.value = saved.description;
      tags.value = saved.hashtags.join(' ');
      hookIn.value = saved.hook;
      kwIn.value = formatKeywords(saved.keywords);
      updateCounters();
      updateButtons();
    }
    revertBtn.addEventListener('click', () => { if (saved) { fillForm(saved); errs.clear(); } });
    ctx.guard = () => (isDirty() ? 'You have unsaved script changes. Leave without saving?' : null);

    function jobBusy() { return !!(job && job.task && job.task.kind === 'run_job'); }
    function clipBusy() { return !!taskId || jobBusy(); }
    function updateButtons() {
      const busy = clipBusy();
      rerenderBtn.disabled = busy || busySaving || !(clip && clip.cut) || !saved;
      saveBtn.disabled = busy || busySaving || !saved;
      rerenderBtn.textContent = isDirty() ? 'Save & re-render' : 'Re-render';
    }
    for (const i of [narration, title, desc, tags, hookIn, kwIn]) i.addEventListener('input', updateButtons);

    function validateClient(f) {
      const e = {};
      const nl = f.narration.trim().length;
      // An empty narration is allowed while the voice-over is off, and when none was written yet
      // (script made with the voice-over off): Re-render then writes it.
      if (!nl && voiceoverOn() && (saved && saved.narration || '').trim()) e.narration = ['Narration cannot be empty.'];
      else if (nl > 5000) e.narration = ['At most 5000 characters.'];
      const tl = f.title.trim().length;
      if (!tl) e.title = ['Title cannot be empty.'];
      else if (tl > 200) e.title = ['At most 200 characters.'];
      if (f.description.length > 5000) e.description = ['At most 5000 characters.'];
      if (f.hashtags.length > MAX_HASHTAGS) e.hashtags = [`At most ${MAX_HASHTAGS} hashtags.`];
      else if (f.hashtags.some((t) => t.length > 100)) e.hashtags = ['Each hashtag can have at most 100 characters.'];
      if ([...f.hook].length > HOOK_MAX_CHARS) e.hook = [`At most ${HOOK_MAX_CHARS} characters.`];
      else if (countWords(f.hook) > HOOK_MAX_WORDS) e.hook = [`At most ${HOOK_MAX_WORDS} words (about 8 reads best).`];
      if (f.keywords.length > 8) e.keywords = ['At most 8 keywords.'];
      else if (f.keywords.some((k) => [...k.word].length > 40 || k.word.split(' ').length > 3)) e.keywords = ['Each keyword can have at most 3 words and 40 characters.'];
      return e;
    }

    async function save() {
      errs.clear();
      const f = formValue();
      const ce = validateClient(f);
      if (Object.keys(ce).length) { errs.show({ message: 'Please fix the highlighted fields.', errors: ce }); return false; }
      busySaving = true;
      updateButtons();
      try {
        const r = await api('PUT', `/api/jobs/${encodeURIComponent(id)}/clips/${n}/${lang}/script`, { body: f });
        if (!ctx.alive) return false;
        const sc = r.data && r.data.script ? r.data.script : f;
        const titleChanged = sc.title !== f.title;
        const tagsChanged = JSON.stringify(sc.hashtags || []) !== JSON.stringify(f.hashtags);
        fillForm(sc);
        staleNote.hidden = !(r.data && r.data.voice_stale) || !voiceoverOn();
        styleNote.hidden = !(r.data && r.data.style_stale);
        const notes = [];
        if (titleChanged) notes.push('The title was shortened to fit 70 characters.');
        if (tagsChanged) notes.push('The hashtags were adjusted ("#" added, spaces and duplicates removed).');
        toast(notes.length ? 'Saved. ' + notes.join(' ') : 'Saved.', 'ok');
        return true;
      } catch (e) {
        if (ctx.alive) errs.show(e);
        return false;
      } finally {
        busySaving = false;
        if (ctx.alive) updateButtons();
      }
    }
    form.addEventListener('submit', (e) => { e.preventDefault(); if (!saveBtn.disabled) save(); });

    rerenderBtn.addEventListener('click', async () => {
      if (rerenderBtn.disabled) return;
      if (isDirty()) { const ok = await save(); if (!ok) return; }
      rerenderBtn.disabled = true;
      try {
        const r = await api('POST', `/api/jobs/${encodeURIComponent(id)}/clips/${n}/${lang}/rerender`);
        const t = r.data && r.data.task;
        toast(r.status === 200 ? 'A re-render for this clip is already queued.' : 'Re-render queued.', 'ok');
        if (t && t.id) { taskId = t.id; showTask(t); }
        Queue.kick();
        taskPoll.kick();
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        if (ctx.alive) updateButtons();
      }
    });

    function showTask(t) {
      if (!t) { taskLine.replaceChildren(); return; }
      swap(taskLine, chip(t.state, taskLabel(t)), ' ', taskDesc(t),
        (t.state === 'queued' || t.state === 'running') ? el('button', { type: 'button', class: 'btn btn-small btn-danger', 'data-fk': 'tcancel',
          text: t.state === 'running' ? 'Stop' : 'Cancel', on: { click: () => cancelTask(t, () => taskPoll.kick()) } }) : null,
        t.error ? el('div', { class: 'field-error', text: t.error }) : null);
    }

    function setVideo() {
      if (entry && entry.final && entry.video_url) {
        const src = entry.video_url + '?token=' + encodeURIComponent(TOKEN) + '&v=' + encodeURIComponent(String(bust));
        if (src !== shownSrc) {
          shownSrc = src;
          video.src = src;
          if (!playerWrap.contains(video)) swap(playerWrap, video);
          video.load();
        }
        download.href = entry.download_url + '?token=' + encodeURIComponent(TOKEN);
        download.hidden = !entry.download_url;
      } else {
        shownSrc = '';
        video.removeAttribute('src');
        if (playerWrap.contains(video)) { video.load(); }
        playerEmpty.textContent = clipBusy() ? 'Rendering…' : 'Not rendered yet. Press Re-render to build it.';
        swap(playerWrap, playerEmpty);
        download.hidden = true;
      }
    }

    function renderSide() {
      swap(stagesBox, CLIP_STAGES.map((k) => miniStage(k, entry.stages && entry.stages[k])));
      swap(clipErr, entry.error ? el('p', { class: 'notice notice-error', role: 'alert', text: entry.error }) : null,
        entry.warning ? el('p', { class: 'notice notice-warn', role: 'status', text: entry.warning }) : null,
        entry.render_stale ? el('p', { class: 'notice notice-info', text: staleText(entry) }) : null,
        captionsLabel(entry),
        jobBusy() ? el('p', { class: 'notice notice-info', text: 'The whole job is being processed; editing is locked until it finishes.' }) : null);
      const v = entry.voice;
      const dur = clipDuration();
      const rows = [];
      if (dur != null) rows.push(['Clip', `${fmtSecs(dur)} (${fmtClock(clip.moment.start)}–${fmtClock(clip.moment.end)} in the source)`]);
      const vo = voiceoverOn();
      rows.push(['Voice-over', vo ? 'on' : 'off (captions only, original audio)']);
      narrField.hidden = !vo;
      voOffNote.hidden = vo;
      if (v && vo) {
        rows.push(['Voice', fmtSecs(v.duration) + (v.chunks > 1 ? ` (${v.chunks} sentences, generated one by one)` : '')]);
        rows.push(['Tempo', typeof v.tempo === 'number' ? `${v.tempo.toFixed(2)}×` : '?']);
        const flags = ['shortened', 'trimmed', 'lengthened', 'truncated'].filter((k) => v[k]).map((k) => (k === 'truncated' ? 'cut short by Voicebox' : k));
        rows.push(['Adjusted', flags.length ? flags.join(', ') : 'no']);
      }
      const a = entry.audio;
      if (a && vo && a.mix !== 'original') {
        const bg = a.background === 'no_vocals' ? 'vocals removed (Demucs)' : (a.background === 'none' ? 'none' : 'original audio');
        rows.push(['Background', bg]);
        if (typeof a.voice_lufs === 'number') {
          const under = typeof a.background_lufs_under_voice === 'number' && typeof a.background_gain_db_under_voice === 'number'
            ? `, background under it ${(a.background_lufs_under_voice + a.background_gain_db_under_voice).toFixed(1)} LUFS` : '';
          rows.push(['Mix', `${a.mix}: voice ${(a.voice_lufs + (a.voice_gain_db || 0)).toFixed(1)} LUFS${under} (before the final normalisation)`]);
        } else rows.push(['Mix', a.mix]);
        if (a.background_note) rows.push(['Background level', 'The background is ' + a.background_note + '.']);
        if (typeof a.trimmed_to === 'number') rows.push(['Final length', `${fmtSecs(a.trimmed_to)} - trimmed after the short narration (ends at ${fmtSecs(a.voice_end || 0)})`]);
      }
      // the moment's hook (why the clip was picked, in English) - not the on-screen hook line below
      if (clip.moment && clip.moment.hook) rows.push(['Moment', clip.moment.hook]);
      swap(voiceInfo, rows.map(([k, val]) => [el('dt', { text: k }), el('dd', { text: val })]));
      staleNote.hidden = !entry.voice_stale || !vo;
      styleNote.hidden = !entry.style_stale || isDirty();
      // prev/next clip-lang
      const pairs = [];
      for (const c of job.clips || []) for (const l of Object.keys(c.langs || {})) pairs.push([c.index, l]);
      const i = pairs.findIndex(([ci, l]) => ci === n && l === lang);
      const link = (p, label, fk) => (p ? el('a', { class: 'btn btn-small', 'data-fk': fk, href: `#/jobs/${id}/clips/${p[0]}/${p[1]}`, text: label }) : el('span'));
      swap(navBox, link(i > 0 ? pairs[i - 1] : null, '← Previous', 'prev'), link(i >= 0 && i < pairs.length - 1 ? pairs[i + 1] : null, 'Next →', 'next'));
    }

    function applyJob(d, firstLoad) {
      job = d;
      clip = (d.clips || []).find((c) => c.index === n) || null;
      entry = clip && clip.langs ? clip.langs[lang] : null;
      if (!clip || !entry) {
        swap(content, el('div', { class: 'notice notice-error', role: 'alert',
          text: !clip ? `Clip ${n} was not found in job ${id}.` : `Clip ${pad2(n)} has no ${LANG_NAMES[lang] || lang} version.` }),
        el('a', { class: 'btn', href: '#/jobs/' + id, text: 'Back to the job' }));
        return false;
      }
      if (firstLoad) {
        swap(content, el('div', { class: 'review-layout' }, playerCol, form));
        if (window.ACPublish) window.ACPublish.panel(ctx, id, n, lang, content); // step 4: publish panel
        if (entry.script) fillForm(entry.script);
        else {
          serverNote.hidden = false;
          serverNote.textContent = 'This clip has no script yet. It appears after the script stage runs.';
        }
        if (entry.task) { taskId = entry.task.id; showTask(entry.task); }
      } else if (entry.script && saved) {
        const s = entry.script;
        const changed = s.narration !== saved.narration || s.title !== saved.title || s.description !== saved.description ||
          JSON.stringify(s.hashtags || []) !== JSON.stringify(saved.hashtags) || (s.hook || '') !== (saved.hook || '') ||
          JSON.stringify((s.keywords || []).map((k) => ({ word: k.word, emoji: k.emoji || '' }))) !== JSON.stringify(saved.keywords || []);
        if (changed) {
          if (!isDirty()) { fillForm(s); serverNote.hidden = true; } else {
            serverNote.hidden = false;
            serverNote.textContent = 'The script was changed on the server (e.g. narration shortened during voice generation). Revert to load it.';
            saved = deepClone(s);
            saved.description = saved.description || '';
            saved.hashtags = saved.hashtags || [];
            saved.hook = saved.hook || '';
            saved.keywords = (saved.keywords || []).map((k) => ({ word: k.word, emoji: k.emoji || '' }));
          }
        }
      } else if (entry.script && !saved) {
        fillForm(entry.script);
        serverNote.hidden = true;
      }
      renderSide();
      setVideo();
      updateButtons();
      return true;
    }

    async function loadJob(first) {
      const d = await apiGet(`/api/jobs/${encodeURIComponent(id)}`);
      if (!ctx.alive) return false;
      return applyJob(d, first);
    }

    // Poll the re-render task (and the job, for stage progress) while it is queued/running.
    const taskPoll = makePoller(ctx, 1500, async () => {
      if (!taskId) {
        // nothing of ours; still follow a whole-job run so the lock lifts when it ends
        if (!jobBusy() && !(entry && entry.task)) return false;
        await loadJob(false);
        if (entry && entry.task && !taskId) { taskId = entry.task.id; showTask(entry.task); }
        return jobBusy() || !!taskId;
      }
      let t;
      try {
        t = await apiGet(`/api/tasks/${taskId}`);
      } catch (e) {
        if (e.status === 404) { taskId = null; await loadJob(false); return false; }
        throw e;
      }
      if (!ctx.alive) return false;
      showTask(t);
      if (t.state === 'queued' || t.state === 'running') {
        await loadJob(false);
        return true;
      }
      // finished
      taskId = null;
      bust = t.finished_at || Date.now();
      if (t.state === 'done') toast('Re-render finished.', 'ok');
      else if (t.state === 'failed') toast('Re-render failed: ' + (t.error || 'see the job log'), 'error');
      else toast('Re-render cancelled.');
      await loadJob(false);
      return false;
    });

    Promise.all([
      getSettings(true).catch(() => null),
      apiGet(`/api/jobs/${encodeURIComponent(id)}`),
    ]).then(([s, d]) => {
      if (!ctx.alive) return;
      settings = s;
      applyJob(d, true);
      taskPoll.kick(); // one pass decides whether to keep polling
    }).catch((e) => {
      if (!ctx.alive) return;
      swap(content, failBox(e), el('a', { class: 'btn', href: '#/jobs/' + id, text: 'Back to the job' }));
    });
  }

  // ================================================================== SETTINGS
  const SECTION_LABELS = {
    _general: ['General', null],
    llm: ['Language model (Claude)', 'Picks the moments and writes the narration.'],
    whisper: ['Transcription (Whisper)', 'Runs locally with faster-whisper.'],
    voicebox: ['Voice (Voicebox)', 'Local text-to-speech server and the voice profile per language.'],
    clips: ['Clips', 'How many clips to cut and how long they are.'],
    audio: ['Audio mix', 'Levels of the original audio and the narration. The "loudness" mix measures both and keeps the background a fixed number of dB under the voice, however loud the source is.'],
    captions: ['Captions', 'Burned-in word-by-word captions. Pick a style preset to fill the style fields, then change any of them by hand.'],
    hook: ['Hook text', 'A short, punchy line (written by Claude with the script, editable per clip) shown big at the top for the first seconds of every clip. A clip with an empty hook shows nothing.'],
    script: ['Narration script', 'Speaking rate used to size the narration to the clip.'],
    signals: ['Moment signals', 'For videos with little speech (gameplay, sports, music): loudness peaks, scene cuts and static/menu screens are measured locally with FFmpeg and given to Claude with the transcript, so action moments can be picked and maps or menus avoided. Speech-rich videos are picked from the transcript as before.'],
    context: ['Video context', 'The video page\'s title, channel, description and tags (and your notes on the New job form) are given to Claude, so titles and hashtags say what the video really is and credit its creator.'],
    web: ['Web uploads', 'Limits for files uploaded through this page.'],
    separation: ['Vocal separation (optional)', 'Demucs removes the speech from the clip so the narration is not talking over someone else. Used only with the voice-over on and Background = "no_vocals"; a clip falls back to the original audio (with a warning) if Demucs is missing or fails. Run Doctor to check the install.'],
    hyperframes: ['HyperFrames captions (optional)', 'Animated captions rendered with the HyperFrames CLI (Node 22+). Used only when the caption engine is "hyperframes"; a clip falls back to ASS captions if HyperFrames is missing or fails. Run Doctor to check the install.'],
  };
  const FIELD_LABELS = {
    languages: ['Output languages', 'Each clip is made in every selected language.'],
    voiceover: ['Voice-over narration', 'On: a narrator voice (Voicebox) talks over each clip. Off: captions only - the clip keeps its original audio and the captions show what is said (translated into the other language).'],
    'llm.provider': ['Provider', null],
    'llm.model': ['Model', 'Anthropic model id, e.g. claude-opus-5-5.'],
    'llm.effort': ['Effort', 'Higher effort gives better picks but is slower and costs more.'],
    'llm.use_fallbacks': ['Use refusal fallback', 'Route a declined request to another model.'],
    'llm.api_key_env': ['API key variable', 'Name of the .env variable that holds the key. The key itself is never shown here.'],
    'llm.max_tokens': ['Max output tokens', 'Long (2 h+) sources at high effort may need 32000+.'],
    'whisper.model': ['Model', 'tiny, base, small, medium, large-v3. Bigger is more accurate and slower.'],
    'whisper.device': ['Device', 'auto, cpu or cuda.'],
    'whisper.compute_type': ['Compute type', 'auto picks int8 on CPU and float16 on CUDA.'],
    'whisper.language': ['Source language', 'Leave empty to auto-detect (or e.g. en, hi).'],
    'voicebox.url': ['Voicebox URL', 'e.g. http://127.0.0.1:17493'],
    'voicebox.poll_interval': ['Poll interval (s)', null],
    'voicebox.timeout_seconds': ['Timeout per Voicebox call (s)', 'Max wait for one generation. With sentence-by-sentence voices that is one sentence (the sentence timeouts below are capped at it); there is no limit for the whole narration, which can take several minutes on CPU. Cancel the job to stop it.'],
    'voicebox.lengthen_timeout_seconds': ['Timeout for the "lengthen" retry (s)', 'A too-short narration is re-generated once with a longer text. If that takes longer than this in total (all its sentences together), it is cancelled and the first voice is kept (with a warning).'],
    'voicebox.chunking': ['Generate sentence by sentence', 'One Voicebox call per sentence (joined with a short pause) instead of one call for the whole narration. Chatterbox often stops early on long text, which made Hindi narrations far too short. Turn off only to compare.'],
    'voicebox.chunk_max_chars': ['Longest chunk (characters)', 'A longer sentence is split at commas, then between words.'],
    'voicebox.chunk_min_chars': ['Shortest chunk (characters)', 'Shorter sentences are joined with a neighbour (very short input can make Chatterbox ramble). Must be smaller than the longest chunk.'],
    'voicebox.chunk_gap_seconds': ['Pause between sentences (s)', 'Silence inserted between the generated sentences.'],
    'voicebox.chunk_timeout_seconds': ['Timeout per sentence (s)', 'Max wait for one sentence (capped at the timeout per Voicebox call). A sentence that fails is tried once more.'],
    'voicebox.chunk_retry_timeout_seconds': ['Timeout for a sentence retry (s)', 'A sentence that came back cut short (or far too long) is generated once more with this limit; if it is still wrong it is kept with a warning.'],
    'voicebox.seed': ['Seed', 'Same seed for every sentence so the voice stays consistent. Empty = picked from the voice profile.'],
    'voicebox.english': ['English voice', null],
    'voicebox.hindi': ['Hindi voice', 'Hindi needs a multilingual engine (Chatterbox).'],
    'voicebox.english.profile_id': ['Voice profile', null],
    'voicebox.hindi.profile_id': ['Voice profile', null],
    'voicebox.english.engine': ['Engine', null],
    'voicebox.hindi.engine': ['Engine', null],
    'clips.count': ['Clips per job', null],
    'clips.min_seconds': ['Minimum length (s)', null],
    'clips.max_seconds': ['Maximum length (s)', 'Must be at least the minimum.'],
    'clips.width': ['Width (px)', 'Even number.'],
    'clips.height': ['Height (px)', 'Even number.'],
    'clips.reframe': ['Reframe mode', 'crop = centre crop to 9:16; blur = whole frame over a blurred fill.'],
    'signals.enabled': ['Use audio/video signals', 'Off = moments from the transcript only.'],
    'signals.video': ['Analyse the picture', 'Scene cuts, motion and static/menu screens. Off = loudness only (faster).'],
    'signals.sample_fps': ['Frames per second analysed', 'Lower is faster; 4 is enough for cuts and motion.'],
    'signals.sample_width': ['Analysis width (px)', 'Frames are shrunk to this width before analysis.'],
    'signals.window_seconds': ['Window (s)', 'One line per window in the table Claude gets.'],
    'signals.speech_rich_coverage': ['Speech-rich above', 'Share of the video with speech (0-1) from which the transcript alone picks the moments, as before.'],
    'signals.static_motion': ['Static below', 'Share of changed pixels (0-1) under which a second counts as static.'],
    'signals.reject_static_fraction': ['Reject moments static for', 'Share (0-1) of a moment that may be static or menu-like (without speech) before it is dropped.'],
    'signals.cut_threshold': ['Scene-cut threshold', 'Picture change (0-1) that counts as a cut; lower finds more cuts.'],
    'signals.menu_flatness': ['Menu/map flatness', 'Share of flat, texture-less picture (0-1) from which a second counts as a map or menu screen.'],
    'signals.timeout_seconds': ['Timeout (s)', 'Signals that take longer are skipped (moments from the transcript only).'],
    'context.enabled': ['Give Claude the video context', 'Page title, channel, description excerpt, tags and your notes.'],
    'context.credit_source': ['Credit the creator', 'Each description names the original creator and links the source video.'],
    'context.description_chars': ['Description excerpt (characters)', 'How much of the page description is kept (0-1000).'],
    'audio.mix': ['Mix', 'loudness = measure the voice and the background and set their levels from that (recommended). gain = the old fixed volume factors.'],
    'audio.voice_lufs': ['Voice loudness (LUFS)', 'loudness mix: the narration is normalised to this before the final loudness normalisation.'],
    'audio.background_db_below_voice': ['Background under the voice (dB)', 'loudness mix: how far the background sits under the narration while it plays. 18-20 dB keeps the voice clear over loud game or music audio.'],
    'audio.original_volume': ['Original volume under the voice', 'gain mix only: level of the source audio while the narration plays (the loudness mix uses "Background under the voice").'],
    'audio.original_volume_after': ['Original volume without voice', 'Level before and after the narration. loudness mix: relative to the voice (1 = as loud as the voice). gain mix: a plain factor. With the voice-over off this is the level of the whole clip (loudness normalisation runs afterwards).'],
    'audio.duck_ramp_seconds': ['Duck ramp (s)', 'Fade time between the two levels.'],
    'audio.voice_volume': ['Voice volume', 'gain mix: a factor; loudness mix: a trim on the voice loudness (1 = unchanged).'],
    'audio.short_voice': ['Short narration', 'trim_clip = when the narration ends early, end the clip shortly after it; keep = keep the whole clip (the original audio comes back after the narration).'],
    'audio.short_voice_below': ['Short below (× clip)', 'A narration that ends before this fraction of the clip counts as short.'],
    'audio.short_voice_tail': ['Tail after the narration (s)', 'trim_clip: how long the clip runs on after the narration ends (the clip is never shorter than 3 s).'],
    'separation.background': ['Background', 'original = the clip\'s own audio under the narration; no_vocals = the clip with its speech removed by Demucs.'],
    'separation.python': ['Demucs Python', 'Python of the venv Demucs is installed in, e.g. ~/tools/demucs/bin/python or %USERPROFILE%\\tools\\demucs\\Scripts\\python.exe.'],
    'separation.model': ['Model', 'Demucs model name; htdemucs is the default.'],
    'separation.segment': ['Segment (s)', 'Seconds processed at once; lower uses less memory (htdemucs allows at most 7).'],
    'separation.threads': ['CPU threads', '0 = all cores.'],
    'separation.timeout_seconds': ['Timeout per clip (s)', 'A longer separation is stopped and the original audio is used.'],
    'separation.models_dir': ['Model folder', 'Where Demucs keeps its downloaded model (TORCH_HOME). Empty = ~/.cache/torch.'],
    'audio.max_speedup': ['Max voice speed-up', 'Longest allowed tempo-up before the narration is shortened.'],
    'audio.loudnorm': ['Normalise loudness', 'Single-pass loudnorm on the final mix.'],
    'audio.target_lufs': ['Target loudness (LUFS)', '-14 suits Shorts / Reels.'],
    'captions.font_en': ['English font', null],
    'captions.font_hi': ['Hindi font', null],
    'captions.font_size': ['Font size (px)', null],
    'captions.words_per_line': ['Words per line', null],
    'captions.primary_color': ['Text colour', 'ASS colour &HAABBGGRR (white = &H00FFFFFF).'],
    'captions.highlight_color': ['Highlight colour', 'ASS colour of the spoken word.'],
    'captions.position_pct': ['Vertical position', 'Centre of the caption line, % from the top.'],
    'captions.margin_v': ['Bottom margin (px)', 'Only used with the legacy bottom position.'],
    'captions.preset': ['Style preset', 'Fills the style fields below (size, weight, case, colours, outline, box, highlight, words per line). Fonts, position and engine are kept.'],
    'captions.bold': ['Bold', null],
    'captions.uppercase': ['Upper case', 'Latin text only; Devanagari has no case.'],
    'captions.outline_color': ['Outline colour', 'ASS colour &HAABBGGRR.'],
    'captions.outline_px': ['Outline (px)', '0 = no outline.'],
    'captions.shadow_px': ['Shadow (px)', '0 = no shadow.'],
    'captions.box': ['Box behind the line', 'A filled box (Box colour) behind each caption line instead of the outline.'],
    'captions.box_color': ['Box colour', 'ASS colour &HAABBGGRR; AA = transparency (00 opaque, 80 half).'],
    'captions.highlight_style': ['Highlight style', 'color = the spoken word turns the highlight colour; pop = it also grows; karaoke = spoken words stay highlighted; none. Not set = ASS color, HyperFrames its animation style.'],
    'captions.emphasis': ['Keyword emphasis', 'Draw the clip keywords (picked by Claude, editable per clip) in the emphasis colour, bigger.'],
    'captions.emphasis_color': ['Emphasis colour', 'ASS colour of the keywords.'],
    'captions.emphasis_scale': ['Emphasis size', 'Keyword size relative to the other words.'],
    'captions.emphasis_max_per_line': ['Keywords per line (max)', null],
    'captions.emoji': ['Emoji after keywords', 'HyperFrames engine only (the ASS engine has no colour emoji).'],
    'hook.enabled': ['Show the hook', null],
    'hook.duration': ['Duration (s)', 'Seconds from the start of the clip.'],
    'hook.top_pct': ['Top edge', 'Where the hook starts, % from the top; it grows downwards. A long hook is drawn smaller so it stays above the captions.'],
    'hook.style': ['Style', 'box = text on a filled box; plain = outlined text.'],
    'hook.font_size': ['Font size (px)', 'The largest size: a long hook is drawn smaller to fit.'],
    'hook.uppercase': ['Upper case', null],
    'hook.text_color': ['Text colour', 'ASS colour &HAABBGGRR.'],
    'hook.box_color': ['Box colour', 'Box style only.'],
    'hook.outline_color': ['Outline colour', 'Plain style only.'],
    'hook.animation': ['Animation', 'HyperFrames engine: pop, slide or fade in. The ASS engine always fades in and out.'],
    'captions.engine': ['Caption engine', 'ass = burned-in libass captions (always available). hyperframes = animated HyperFrames captions (optional install, see README); falls back to ass per clip.'],
    'hyperframes.node_bin_dir': ['Node folder', 'Folder with node (Node 22+), e.g. ~/tools/node22/bin or %USERPROFILE%\\tools\\node22. Empty = the node on PATH.'],
    'hyperframes.package_dir': ['HyperFrames folder', 'Folder where `npm --prefix` installed hyperframes, e.g. ~/tools/hyperframes.'],
    'hyperframes.style': ['Animation style', 'pop = the spoken word pops and turns the highlight colour; karaoke = words stay highlighted once spoken.'],
    'hyperframes.fps': ['Frame rate', 'Empty = same as the clip.'],
    'hyperframes.format': ['Overlay format', 'webm = small VP9 with alpha (needs FFmpeg with libvpx); mov = ProRes 4444 (large).'],
    'hyperframes.workers': ['Render workers', '0 = auto. Each worker is a Chrome process (~256 MB).'],
    'hyperframes.timeout_seconds': ['Timeout per clip (s)', 'A longer render falls back to ASS captions.'],
    'script.wps_en': ['English words per second', 'Measured speaking rate of the English voice.'],
    'script.wps_hi': ['Hindi words per second', 'Measured speaking rate of the Hindi voice.'],
    'script.fill_ratio': ['Fill ratio', 'Narration target = clip length × this.'],
    'script.lengthen_below': ['Lengthen below', 'If the voice is shorter than this × clip, ask for a longer script once.'],
    'web.max_upload_mb': ['Max upload size (MiB)', null],
    'web.allowed_extensions': ['Allowed file extensions', 'Separate with spaces or commas, e.g. .mp4 .mov'],
  };
  const DATALISTS = {
    'whisper.model': ['tiny', 'base', 'small', 'medium', 'large-v3'],
    'whisper.device': ['auto', 'cpu', 'cuda'],
    'whisper.compute_type': ['auto', 'int8', 'float16', 'int8_float16'],
    'voicebox.english.engine': ['chatterbox', 'chatterbox_turbo', 'kokoro', 'qwen'],
    'voicebox.hindi.engine': ['chatterbox', 'chatterbox_turbo', 'kokoro', 'qwen'],
  };
  const PROFILE_FIELDS = { 'voicebox.english.profile_id': 'en', 'voicebox.hindi.profile_id': 'hi' };

  function viewSettings(ctx) {
    const head = pageHead('Settings', null);
    const content = el('div');
    append($main, head, content);
    content.appendChild(loading());

    let schema = null;
    let loaded = null;
    let values = null;         // what the form is built from: loaded, or the form plus a preset just applied
    let presets = null;        // G5: [{id, name, description, values}] or null
    let previewLang = 'en';
    let profiles = null;       // array or null
    let profilesError = null;  // ApiError
    let fields = [];
    let dirty = false;
    ctx.guard = () => (dirty ? 'You have unsaved settings. Leave without saving?' : null);

    function resolveRef(node) {
      let n = node;
      let guard = 0;
      while (n && typeof n === 'object' && guard++ < 20) {
        if (n.$ref) {
          const m = /^#\/(\$defs|definitions)\/(.+)$/.exec(n.$ref);
          const defs = m ? (schema[m[1]] || {}) : {};
          const target = m ? defs[decodeURIComponent(m[2])] : null;
          if (!target) return n;
          // keep sibling keywords (title, default, description) from the referencing node
          const { $ref, ...rest } = n;
          n = Object.assign({}, target, rest);
          continue;
        }
        if (Array.isArray(n.allOf) && n.allOf.length === 1) {
          const { allOf, ...rest } = n;
          n = Object.assign({}, allOf[0], rest);
          continue;
        }
        break;
      }
      return n;
    }
    function unwrapNullable(node) {
      const n = resolveRef(node);
      const alts = n && (n.anyOf || n.oneOf);
      if (Array.isArray(alts)) {
        const nonNull = alts.filter((a) => !(a && a.type === 'null'));
        if (nonNull.length === 1 && nonNull.length < alts.length) {
          const { anyOf, oneOf, ...rest } = n;
          return { node: Object.assign({}, resolveRef(nonNull[0]), rest), nullable: true };
        }
      }
      if (n && Array.isArray(n.type) && n.type.includes('null')) {
        const t = n.type.filter((x) => x !== 'null');
        return { node: Object.assign({}, n, { type: t.length === 1 ? t[0] : t }), nullable: true };
      }
      return { node: n, nullable: false };
    }
    function isObjectNode(n) { return n && (n.type === 'object' || n.properties) && n.properties; }

    function load() {
      swap(content, loading('Loading settings…'));
      Promise.all([
        apiGet('/api/settings/schema'),
        apiGet('/api/settings'),
        apiGet('/api/voicebox/profiles').then((d) => { profilesError = null; return d; }, (e) => { profilesError = e; return null; }),
        apiGet('/api/caption-presets').then((d) => (d && Array.isArray(d.presets) ? d.presets : null), () => null),
      ]).then(([sc, st, pr, cp]) => {
        if (!ctx.alive) return;
        presets = cp;
        schema = sc;
        loaded = st;
        settingsCache = st;
        profiles = pr && Array.isArray(pr.profiles) ? pr.profiles : null;
        build();
      }).catch((e) => {
        if (ctx.alive) swap(content, failBox(e, load));
      });
    }

    function build(vals, markDirty) {
      fields = [];
      values = vals || loaded;
      dirty = !!markDirty;
      const summary = errorNotice();
      const errs = new FormErrors(summary);
      const root = resolveRef(schema);
      const props = (root && root.properties) || {};
      const general = [];
      const sections = [];
      for (const [key, raw] of Object.entries(props)) {
        const { node } = unwrapNullable(raw);
        if (key === 'publish') continue; // step 4: owned by the Accounts page (left out on save; the server keeps it)
        if (isObjectNode(node)) sections.push([key, node]);
        else general.push([key, raw]);
      }
      const fsList = [];
      if (general.length) {
        const [lbl, help] = SECTION_LABELS._general;
        const fs = el('fieldset', null, el('legend', { text: lbl }), help ? el('p', { class: 'small muted', text: help }) : null);
        const grid = el('div', { class: 'fields' });
        for (const [key, raw] of general) grid.appendChild(buildField([key], raw, errs));
        fs.appendChild(grid);
        fsList.push(fs);
      }
      for (const [key, node] of sections) fsList.push(buildGroup([key], node, errs, 0));

      const saveBtn = el('button', { type: 'submit', class: 'btn btn-primary', text: 'Save settings' });
      const revertBtn = el('button', { type: 'button', class: 'btn', text: 'Revert', on: { click: () => { if (!dirty || confirm('Discard your changes?')) build(); } } });
      const state = el('span', { class: 'small muted', 'aria-live': 'polite', text: markDirty ? 'Unsaved changes' : '' });
      const form = el('form', { novalidate: true, 'aria-label': 'Settings' }, fsList, summary,
        el('div', { class: 'form-actions' }, saveBtn, revertBtn, state,
          el('span', { class: 'small muted', text: 'Changes apply to the next task.' })));
      form.addEventListener('input', () => { dirty = true; state.textContent = 'Unsaved changes'; });
      form.addEventListener('change', () => { dirty = true; state.textContent = 'Unsaved changes'; });
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        errs.clear();
        const out = deepClone(loaded) || {};
        // step 4: never send the (possibly stale) publish section: PUT /api/settings keeps the stored one,
        // so a tab opened earlier cannot undo the dry-run switch or the account mapping set on #/accounts
        delete out.publish;
        const clientErrors = {};
        for (const f of fields) {
          try { setPath(out, f.path, f.get()); } catch (err) { clientErrors[f.path.join('.')] = [err.message]; }
        }
        if (Object.keys(clientErrors).length) { errs.show({ message: 'Please fix the highlighted fields.', errors: clientErrors }); return; }
        saveBtn.disabled = true;
        state.textContent = 'Saving…';
        try {
          const r = await api('PUT', '/api/settings', { body: out });
          if (!ctx.alive) return;
          loaded = r.data || out;
          settingsCache = loaded;
          toast('Settings saved.', 'ok');
          build();
        } catch (err) {
          if (!ctx.alive) return;
          state.textContent = 'Not saved';
          errs.show(err);
        } finally {
          if (ctx.alive) saveBtn.disabled = false;
        }
      });
      swap(content, form);
      syncEngine(form);
      syncVoiceover(form);
      syncStyle(form);
      form.addEventListener('change', () => { syncEngine(form); syncVoiceover(form); });
      form.addEventListener('input', () => syncStyle(form));
      form.addEventListener('change', () => syncStyle(form));
    }

    // ---- G5: caption presets + live preview (a CSS mock of one 9:16 frame) ----
    function fieldValue(key, dflt) {
      const f = fields.find((x) => x.path.join('.') === key);
      if (!f) return dflt;
      try { const v = f.get(); return v === undefined ? dflt : v; } catch (e) { return dflt; }
    }
    function formValues() {
      const out = deepClone(values) || {};
      for (const f of fields) { try { setPath(out, f.path, f.get()); } catch (e) { /* keep the old value */ } }
      return out;
    }
    function applyPreset(id) {
      const p = (presets || []).find((x) => x.id === id);
      const cur = formValues();
      cur.captions = Object.assign({}, cur.captions || {}, p ? p.values : {}, { preset: id });
      build(cur, true);
      toast(p ? `Preset "${p.name}" applied - save to keep it.` : 'Preset cleared.', 'ok');
    }
    function presetModified(id) {
      const p = (presets || []).find((x) => x.id === id);
      if (!p) return false;
      return Object.entries(p.values).some(([k, v]) => fieldValue('captions.' + k, v) !== v);
    }
    function presetField(id, value, describedBy) {
      const sel = el('select', { id, 'aria-describedby': describedBy });
      sel.appendChild(el('option', { value: 'custom', text: 'Custom (no preset)' }));
      for (const p of presets || []) sel.appendChild(el('option', { value: p.id, text: p.name }));
      sel.value = value || 'custom';
      if (sel.value !== (value || 'custom')) sel.value = 'custom';
      const note = el('div', { class: 'hint preset-note', 'aria-live': 'polite' });
      sel.addEventListener('change', () => applyPreset(sel.value));
      return { el: el('div', { class: 'stack' }, sel, note), get: () => sel.value };
    }
    function assCss(v, fallback) {
      const m = /^&H([0-9A-Fa-f]{8})$/.exec(String(v || '').trim());
      if (!m) return fallback;
      const h = m[1];
      const a = parseInt(h.slice(0, 2), 16);
      const b = parseInt(h.slice(2, 4), 16);
      const g = parseInt(h.slice(4, 6), 16);
      const r = parseInt(h.slice(6, 8), 16);
      return `rgba(${r},${g},${b},${(1 - a / 255).toFixed(2)})`;
    }
    // An ASS colour text field (&HAABBGGRR) with a colour swatch next to it: picking a colour writes the BGR
    // digits and keeps the transparency (AA); typing in the text field moves the swatch.
    function assColorControl(inp, label) {
      const sw = el('input', { type: 'color', class: 'ass-swatch', 'aria-label': `${label}: pick a colour` });
      const toHex = () => {
        const m = /^&H([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})$/.exec(inp.value.trim());
        if (m) sw.value = ('#' + m[4] + m[3] + m[2]).toLowerCase();
      };
      sw.addEventListener('input', () => {
        const m = /^&H([0-9A-Fa-f]{2})/.exec(inp.value.trim());
        const aa = m ? m[1].toUpperCase() : '00';
        const h = sw.value.slice(1).toUpperCase();
        inp.value = `&H${aa}${h.slice(4, 6)}${h.slice(2, 4)}${h.slice(0, 2)}`;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      });
      inp.addEventListener('input', toHex);
      toHex();
      return el('div', { class: 'ass-color' }, inp, sw);
    }
    const PREVIEW = {
      en: { words: ['total', 'madness', 'in', 'the', 'final', 'round', 'right', 'now'], emph: 1, hook: 'Wait for the last second' },
      hi: { words: ['पूरा', 'पागलपन', 'आखिरी', 'राउंड', 'में', 'देखो', 'अभी', 'यार'], emph: 1, hook: 'आखिरी सेकंड तक देखो' },
    };
    function stylePreview() {
      const frame = el('div', { class: 'cp-frame', 'aria-hidden': 'true' },
        el('div', { class: 'cp-face' }), el('div', { class: 'cp-hook' }), el('div', { class: 'cp-cap' }));
      const langBtns = ['en', 'hi'].map((l) => el('button', { type: 'button', class: 'btn btn-small', 'data-lang': l,
        text: l === 'en' ? 'English' : 'हिन्दी', 'aria-pressed': String(l === previewLang),
        on: { click: () => { previewLang = l; const f = frame.closest('form'); if (f) syncStyle(f); } } }));
      return el('div', { class: 'cap-preview' },
        el('div', { class: 'row' }, el('strong', { class: 'small', text: 'Preview' }), ...langBtns),
        frame,
        el('div', { class: 'hint', text: 'Approximate: the spoken word is "total", the keyword "madness" (emoji with HyperFrames only). Re-render clips to apply saved changes.' }));
    }
    function syncStyle(form) {
      const fr = form.querySelector('.cp-frame');
      const note = form.querySelector('.preset-note');
      const pid = fieldValue('captions.preset', 'custom');
      if (note) note.textContent = pid !== 'custom' && presetModified(pid) ? 'Changed by hand after the preset was applied.' : '';
      if (!fr) return;
      for (const b of form.querySelectorAll('.cap-preview [data-lang]')) b.setAttribute('aria-pressed', String(b.getAttribute('data-lang') === previewLang));
      for (const f of form.querySelectorAll('.cp-frame')) paintPreview(f);
    }
    // One preview frame (there is one under Captions and one under Hook text, both show the same settings).
    function paintPreview(fr) {
      const k = fr.clientWidth > 0 ? fr.clientWidth / 1080 : 0.2;
      const sample = PREVIEW[previewLang];
      const hiLang = previewLang === 'hi';
      const font = hiLang ? `"${fieldValue('captions.font_hi', 'Noto Sans Devanagari')}", "Noto Sans Devanagari", sans-serif`
        : `"${fieldValue('captions.font_en', 'Noto Sans')}", "Noto Sans", sans-serif`;
      const size = Number(fieldValue('captions.font_size', 78)) * 0.75 * k;
      const upper = !!fieldValue('captions.uppercase', false);
      const wpl = Math.max(1, Number(fieldValue('captions.words_per_line', 3)) || 3);
      const primary = assCss(fieldValue('captions.primary_color'), '#fff');
      const hl = assCss(fieldValue('captions.highlight_color'), '#ffe500');
      const emc = assCss(fieldValue('captions.emphasis_color'), '#ffa500');
      const outline = assCss(fieldValue('captions.outline_color'), '#000');
      const box = !!fieldValue('captions.box', false);
      const engine = fieldValue('captions.engine', 'ass');
      const hstyle = fieldValue('captions.highlight_style', null) || (engine === 'hyperframes' ? fieldValue('hyperframes.style', 'pop') : 'color');
      const cap = fr.querySelector('.cp-cap');
      cap.replaceChildren();
      const pct = fieldValue('captions.position_pct', 72);
      if (typeof pct === 'number') { cap.style.top = pct + '%'; cap.style.bottom = ''; } else {
        cap.style.top = ''; cap.style.bottom = (Number(fieldValue('captions.margin_v', 520)) / 1920 * 100) + '%';
      }
      cap.style.fontFamily = font;
      cap.style.fontWeight = fieldValue('captions.bold', true) ? '700' : '400';
      cap.style.background = box ? assCss(fieldValue('captions.box_color'), 'rgba(0,0,0,.75)') : 'transparent';
      cap.style.padding = box ? `${0.12 * size}px ${0.3 * size}px` : '0';
      const sh = Number(fieldValue('captions.shadow_px', 2)) || 0;
      sample.words.slice(0, wpl).forEach((w, i) => {
        const span = el('span', { class: 'cp-w', text: upper ? w.toUpperCase() : w, lang: hiLang ? 'hi' : null });
        const emph = !!fieldValue('captions.emphasis', true) && i === sample.emph;
        const active = i === 0;
        let color = primary;
        let scale = 1;
        // like the render: a keyword rests in the emphasis colour; the spoken word takes the highlight colour
        // (a keyword too); the ASS engine never scales words on a box
        if (emph) { color = emc; scale = Number(fieldValue('captions.emphasis_scale', 1.2)) || 1; }
        if (hstyle !== 'none' && active) color = hl;
        if (active && hstyle === 'pop') scale *= 1.15;
        if (box && engine === 'ass') scale = 1;
        span.style.color = color;
        span.style.fontSize = (size * scale).toFixed(1) + 'px';
        span.style.webkitTextStroke = box ? '0' : `${(Number(fieldValue('captions.outline_px', 5)) * 2 * k).toFixed(2)}px ${outline}`;
        span.style.textShadow = sh && !box ? `0 ${2 * sh * k}px ${7 * sh * k}px rgba(0,0,0,.55)` : 'none';
        cap.appendChild(span);
        if (emph && engine === 'hyperframes' && fieldValue('captions.emoji', true)) cap.appendChild(el('span', { class: 'cp-emo', text: '🔥' }));
      });
      const hook = fr.querySelector('.cp-hook');
      const on = !!fieldValue('hook.enabled', true);
      hook.hidden = !on;
      if (!on) return;
      const hass = Number(fieldValue('hook.font_size', 112)) || 112;
      const hsize = hass * 0.75 * k;
      const hbox = fieldValue('hook.style', 'box') === 'box';
      hook.textContent = fieldValue('hook.uppercase', false) ? sample.hook.toUpperCase() : sample.hook;
      hook.lang = hiLang ? 'hi' : '';
      hook.style.top = Number(fieldValue('hook.top_pct', 8)) + '%';  // top edge; grows downwards like the render
      hook.style.maxWidth = (900 * k).toFixed(1) + 'px';  // the render's wrap width (5/6 of the frame)
      hook.style.fontFamily = font;
      hook.style.fontWeight = fieldValue('captions.bold', true) ? '700' : '400';
      hook.style.fontSize = hsize.toFixed(1) + 'px';
      hook.style.color = assCss(fieldValue('hook.text_color'), '#fff');
      hook.style.background = hbox ? assCss(fieldValue('hook.box_color'), '#e81a2a') : 'transparent';
      hook.style.padding = hbox ? `${(Math.max(10, 0.18 * hass) * k).toFixed(1)}px ${0.42 * hsize}px` : '0';
      hook.style.webkitTextStroke = hbox ? '0' : `${(hsize / 7).toFixed(2)}px ${assCss(fieldValue('hook.outline_color'), '#000')}`;
    }

    // Voicebox and narration-script fields stay editable but are greyed while the voice-over is off.
    function syncVoiceover(form) {
      const f = fields.find((x) => x.path.join('.') === 'voiceover');
      let on = true;
      try { on = f ? !!f.get() : true; } catch (e) { on = true; }
      for (const key of ['voicebox', 'script', 'separation']) {
        const fs = form.querySelector(`fieldset[data-path="${key}"]`);
        if (!fs) continue;
        fs.classList.toggle('is-inactive', !on);
        let note = fs.querySelector('.voiceover-note');
        if (!on && !note) {
          note = el('p', { class: 'small notice notice-info voiceover-note', text: 'Not used: Voice-over narration is off (General). Clips keep their original audio with captions only.' });
          const legend = fs.querySelector('legend');
          if (legend && legend.nextSibling) fs.insertBefore(note, legend.nextSibling.nextSibling || null); else fs.appendChild(note);
        } else if (on && note) note.remove();
      }
      // Audio mix: the under-voice / duck / voice fields do nothing without a voice-over.
      for (const key of ['audio.mix', 'audio.voice_lufs', 'audio.background_db_below_voice', 'audio.original_volume', 'audio.duck_ramp_seconds', 'audio.voice_volume', 'audio.max_speedup', 'audio.short_voice', 'audio.short_voice_below', 'audio.short_voice_tail']) {
        const w = form.querySelector(`.field[data-path="${key}"]`);
        if (!w) continue;
        w.classList.toggle('is-inactive', !on);
        if (!on) w.title = 'Not used: Voice-over narration is off'; else w.removeAttribute('title');
      }
      if (!on) return;
      // Voice-over on: grey the fields of the mix that is not selected.
      const mf = fields.find((x) => x.path.join('.') === 'audio.mix');
      let mix = 'loudness';
      try { mix = mf ? mf.get() : 'loudness'; } catch (e) { mix = 'loudness'; }
      const unused = mix === 'gain' ? ['audio.voice_lufs', 'audio.background_db_below_voice'] : ['audio.original_volume'];
      for (const key of unused) {
        const w = form.querySelector(`.field[data-path="${key}"]`);
        if (!w) continue;
        w.classList.add('is-inactive');
        w.title = `Not used with the "${mix}" mix`;
      }
    }

    // HyperFrames fields stay editable but are greyed while the caption engine is "ass".
    function syncEngine(form) {
      const fs = form.querySelector('fieldset[data-path="hyperframes"]');
      const sel = form.querySelector('#set-captions-engine');
      if (!fs || !sel) return;
      const f = fields.find((x) => x.path.join('.') === 'captions.engine');
      let engine = null;
      try { engine = f ? f.get() : null; } catch (e) { engine = null; }
      const off = engine !== 'hyperframes';
      fs.classList.toggle('is-inactive', off);
      let note = fs.querySelector('.engine-note');
      if (off && !note) {
        note = el('p', { class: 'small notice notice-info engine-note', text: 'Not used: the caption engine is "ass". Set Captions › Caption engine to "hyperframes" to use these.' });
        const legend = fs.querySelector('legend');
        if (legend && legend.nextSibling) fs.insertBefore(note, legend.nextSibling.nextSibling || null); else fs.appendChild(note);
      } else if (!off && note) note.remove();
    }

    function buildGroup(path, node, errs, depth) {
      const key = path.join('.');
      const lbl = depth === 0 ? (SECTION_LABELS[key] || [node.title || humanize(path[path.length - 1]), null])
        : (FIELD_LABELS[key] || [node.title || humanize(path[path.length - 1]), null]);
      const fs = el('fieldset', { 'data-path': key }, el('legend', { text: lbl[0] }), lbl[1] ? el('p', { class: 'small muted', text: lbl[1] }) : null);
      if (key === 'captions' || key === 'hook') fs.appendChild(stylePreview());
      const errEl = el('div', { class: 'field-error' });
      errs.register(key, fs, errEl);
      if (key === 'voicebox' && profilesError) {
        fs.appendChild(el('p', { class: 'notice notice-warn', role: 'status',
          text: `${profilesError.message} Voice profiles cannot be listed, so type the profile ids by hand.` }));
      } else if (key === 'voicebox' && profiles && !profiles.length) {
        fs.appendChild(el('p', { class: 'notice notice-warn', text: 'Voicebox has no voice profiles yet. Create one in Voicebox first.' }));
      }
      const grid = el('div', { class: 'fields' });
      const subs = [];
      for (const [k, raw] of Object.entries(node.properties || {})) {
        const { node: inner } = unwrapNullable(raw);
        if (isObjectNode(inner)) subs.push(buildGroup(path.concat(k), inner, errs, depth + 1));
        else grid.appendChild(buildField(path.concat(k), raw, errs));
      }
      if (grid.childNodes.length) fs.appendChild(grid);
      for (const s of subs) fs.appendChild(s);
      fs.appendChild(errEl);
      return fs;
    }

    function buildField(path, raw, errs) {
      const key = path.join('.');
      const id = 'set-' + key.replace(/[^A-Za-z0-9_-]/g, '-');
      const { node, nullable } = unwrapNullable(raw);
      const def = FIELD_LABELS[key] || [node.title || raw.title || humanize(path[path.length - 1]), node.description || null];
      let value = getPath(values, path);
      if (value === undefined) value = 'default' in raw ? raw.default : node.default;
      const err = el('div', { class: 'field-error', id: id + '-err' });
      const hintText = [def[1], rangeText(node)].filter(Boolean).join(' ');
      const hint = hintText ? el('div', { class: 'hint', id: id + '-hint', text: hintText }) : null;
      const wrap = el('div', { class: 'field', 'data-path': key });
      const describedBy = [hint ? id + '-hint' : null, id + '-err'].filter(Boolean).join(' ');
      let control;
      let get;

      const type = Array.isArray(node.type) ? node.type[0] : node.type;
      const enumVals = Array.isArray(node.enum) ? node.enum : ('const' in node ? [node.const] : null);

      if (key === 'captions.position_pct' && (type === 'number' || type === 'integer')) {
        const r = positionField(id, node, nullable, value, describedBy);
        control = r.el; get = r.get;
        wrap.classList.add('field-wide');
      } else if (key === 'captions.preset' && presets) {
        const r = presetField(id, value, describedBy);
        control = r.el; get = r.get;
      } else if (PROFILE_FIELDS[key] && profiles && !profilesError) {
        const lang = PROFILE_FIELDS[key];
        const sel = el('select', { id, 'aria-describedby': describedBy });
        sel.appendChild(el('option', { value: '', text: '- not set -' }));
        const sorted = profiles.slice().sort((a, b) => {
          const am = (a.language || '').startsWith(lang) ? 0 : 1;
          const bm = (b.language || '').startsWith(lang) ? 0 : 1;
          return am - bm || String(a.name || '').localeCompare(String(b.name || ''));
        });
        let found = false;
        for (const p of sorted) {
          const pid = String(p.id == null ? '' : p.id);
          if (!pid) continue;
          if (pid === value) found = true;
          sel.appendChild(el('option', { value: pid, text: `${p.name || pid}${p.language ? ' (' + p.language + ')' : ''}` }));
        }
        if (value && !found) sel.appendChild(el('option', { value, text: `${value} (not found in Voicebox)` }));
        sel.value = value || '';
        control = sel;
        get = () => sel.value;
      } else if (type === 'boolean') {
        const cb = el('input', { type: 'checkbox', id, checked: !!value, 'aria-describedby': describedBy });
        control = el('label', { class: 'check' }, cb, def[0]);
        get = () => cb.checked;
        append(wrap, control, hint, err);
        errs.register(key, wrap, err);
        fields.push({ path, get });
        return wrap;
      } else if (enumVals && (type === 'string' || type === 'integer' || type === 'number' || !type)) {
        const sel = el('select', { id, 'aria-describedby': describedBy, disabled: enumVals.length === 1 && 'const' in node });
        if (nullable) sel.appendChild(el('option', { value: '__null__', text: '- not set -' }));
        enumVals.forEach((v, i) => sel.appendChild(el('option', { value: String(i), text: String(v) })));
        const idx = enumVals.findIndex((v) => v === value);
        sel.value = value === null && nullable ? '__null__' : (idx >= 0 ? String(idx) : '0');
        control = sel;
        get = () => (sel.value === '__null__' ? null : enumVals[Number(sel.value)]);
      } else if (type === 'integer' || type === 'number') {
        const inp = el('input', { type: 'number', id, inputmode: type === 'integer' ? 'numeric' : 'decimal',
          step: type === 'integer' ? '1' : 'any', 'aria-describedby': describedBy });
        if (typeof node.minimum === 'number') inp.min = String(node.minimum);
        if (typeof node.maximum === 'number') inp.max = String(node.maximum);
        if (value !== null && value !== undefined) inp.value = String(value);
        if (nullable) inp.placeholder = 'not set';
        control = inp;
        get = () => {
          const s = inp.value.trim();
          if (s === '') { if (nullable) return null; throw new Error('Enter a number.'); }
          const v = Number(s);
          if (!isFinite(v)) throw new Error('Enter a number.');
          if (type === 'integer' && !Number.isInteger(v)) throw new Error('Enter a whole number.');
          return v;
        };
      } else if (type === 'array') {
        const items = unwrapNullable(node.items || {}).node;
        const itemEnum = Array.isArray(items.enum) ? items.enum : null;
        if (itemEnum) {
          const arr = Array.isArray(value) ? value : [];
          const boxes = itemEnum.map((v) => {
            const cb = el('input', { type: 'checkbox', value: String(v), checked: arr.includes(v) });
            const lbl = el('label', { class: 'check', lang: v === 'hi' ? 'hi' : null }, cb, v === 'en' ? 'English' : (v === 'hi' ? 'हिन्दी (Hindi)' : String(v)));
            return [cb, lbl, v];
          });
          control = el('div', { class: 'checks', role: 'group', id, 'aria-labelledby': id + '-lbl', 'aria-describedby': describedBy }, boxes.map((b) => b[1]));
          get = () => boxes.filter((b) => b[0].checked).map((b) => b[2]);
          append(wrap, el('span', { class: 'field-label', id: id + '-lbl', text: def[0] }), control, hint, err);
          errs.register(key, wrap, err);
          fields.push({ path, get });
          return wrap;
        }
        const inp = el('input', { type: 'text', id, spellcheck: 'false', 'aria-describedby': describedBy });
        inp.value = Array.isArray(value) ? value.join(' ') : '';
        control = inp;
        const itemType = items.type;
        get = () => {
          const parts = parseTags(inp.value);
          if (nullable && !parts.length) return null;
          if (itemType === 'integer' || itemType === 'number') {
            return parts.map((p) => { const v = Number(p); if (!isFinite(v)) throw new Error(`“${p}” is not a number.`); return v; });
          }
          return parts;
        };
      } else if (type === 'string') {
        const inp = el('input', { type: 'text', id, spellcheck: 'false', autocomplete: 'off', 'aria-describedby': describedBy });
        if (typeof node.maxLength === 'number') inp.maxLength = node.maxLength;
        inp.value = value == null ? '' : String(value);
        if (nullable) inp.placeholder = key === 'whisper.language' ? 'auto-detect' : 'not set';
        if (DATALISTS[key]) {
          const listId = id + '-list';
          inp.setAttribute('list', listId);
          append(wrap, el('datalist', { id: listId }, DATALISTS[key].map((v) => el('option', { value: v }))));
        }
        control = inp;
        if (/^(captions|hook)\.[a-z_]*color$/.test(key)) control = assColorControl(inp, def[0]);
        get = () => {
          const s = inp.value.trim();
          if (nullable && s === '') return null;
          return s;
        };
      } else {
        // unknown shape: edit as JSON so nothing is lost
        const ta = el('textarea', { id, rows: '3', class: 'mono', spellcheck: 'false', 'aria-describedby': describedBy });
        ta.value = JSON.stringify(value === undefined ? null : value);
        control = ta;
        get = () => { try { return JSON.parse(ta.value); } catch (e) { throw new Error('Not valid JSON.'); } };
      }
      wrap.prepend(el('label', { for: id, text: def[0] }));
      append(wrap, control, hint, err);
      errs.register(key, wrap, err);
      fields.push({ path, get });
      return wrap;
    }

    function rangeText(node) {
      const lo = typeof node.minimum === 'number' ? node.minimum : (typeof node.exclusiveMinimum === 'number' ? `>${node.exclusiveMinimum}` : null);
      const hi = typeof node.maximum === 'number' ? node.maximum : (typeof node.exclusiveMaximum === 'number' ? `<${node.exclusiveMaximum}` : null);
      if (lo === null && hi === null) return null;
      if (lo !== null && hi !== null) return `Range ${lo} to ${hi}.`;
      return lo !== null ? `At least ${lo}.` : `At most ${hi}.`;
    }

    function positionField(id, node, nullable, value, describedBy) {
      const min = typeof node.minimum === 'number' ? node.minimum : 5;
      const max = typeof node.maximum === 'number' ? node.maximum : 95;
      const start = typeof value === 'number' ? value : (typeof node.default === 'number' ? node.default : 72);
      const range = el('input', { type: 'range', id, min: String(min), max: String(max), step: '0.5', 'aria-describedby': describedBy });
      range.value = String(start);
      const num = el('input', { type: 'number', min: String(min), max: String(max), step: '0.5', 'aria-label': 'Vertical position in percent' });
      num.value = String(start);
      const preview = el('div', { class: 'pos-preview', 'aria-hidden': 'true' }, el('div', { class: 'face' }), el('div', { class: 'cap' }));
      const valueText = el('span', { class: 'pos-value' });
      const legacy = nullable ? el('input', { type: 'checkbox', checked: value === null }) : null;
      function sync(v) {
        const x = Number(v);
        if (isFinite(x)) {
          preview.querySelector('.cap').style.top = Math.max(0, Math.min(100, x)) + '%';
          valueText.textContent = `${x}% from top`;
        }
        const off = legacy && legacy.checked;
        preview.classList.toggle('legacy', !!off);
        range.disabled = !!off;
        num.disabled = !!off;
        if (off) { preview.querySelector('.cap').style.top = ''; valueText.textContent = 'legacy: bottom margin'; }
      }
      range.addEventListener('input', () => { num.value = range.value; sync(range.value); });
      num.addEventListener('input', () => { if (num.value !== '') range.value = num.value; sync(num.value); });
      if (legacy) legacy.addEventListener('change', () => sync(range.value));
      sync(start);
      const box = el('div', { class: 'pos-wrap' },
        el('div', { class: 'stack' }, range, el('div', { class: 'row' }, num, valueText),
          legacy ? el('label', { class: 'check small' }, legacy, 'Use the legacy bottom margin instead') : null),
        preview);
      return {
        el: box,
        get: () => {
          if (legacy && legacy.checked) return null;
          const v = Number(num.value);
          if (num.value.trim() === '' || !isFinite(v)) throw new Error('Enter a number.');
          return v;
        },
      };
    }

    load();
  }

  // ================================================================== DOCTOR
  function viewDoctor(ctx) {
    const rerun = el('button', { type: 'button', class: 'btn', text: 'Re-run checks' });
    const content = el('div');
    append($main, pageHead('Doctor', null, rerun),
      el('p', { class: 'muted', text: 'The same checks as `autoclipper doctor`: tools, fonts, models, Voicebox and the API key.' }),
      content);
    async function run() {
      rerun.disabled = true;
      swap(content, loading('Running checks (this can take a few seconds)…'));
      try {
        const d = await apiGet('/api/doctor');
        if (!ctx.alive) return;
        const checks = Array.isArray(d.checks) ? d.checks : [];
        const problems = d.problems || 0;
        const warnings = d.warnings || 0;
        swap(content,
          el('p', { class: 'notice ' + (problems ? 'notice-error' : (warnings ? 'notice-warn' : 'notice-ok')), role: 'status',
            text: problems ? `${problems} problem${problems === 1 ? '' : 's'}, ${warnings} warning${warnings === 1 ? '' : 's'}.`
              : (warnings ? `No problems, ${warnings} warning${warnings === 1 ? '' : 's'}.` : 'All checks passed.') }),
          el('div', { class: 'card table-wrap' },
            el('table', { class: 'doctor' },
              el('caption', { class: 'sr-only', text: 'Doctor checks' }),
              el('thead', null, el('tr', null, el('th', { scope: 'col', text: 'Status' }), el('th', { scope: 'col', text: 'Check' }), el('th', { scope: 'col', text: 'Detail' }))),
              el('tbody', null, checks.map((c) => el('tr', null,
                el('td', null, chip(c.level, c.level)),
                el('td', null, el('strong', { text: c.name })),
                el('td', { class: 'detail', text: c.detail || '' })))))));
      } catch (e) {
        if (ctx.alive) swap(content, failBox(e, run));
      } finally {
        if (ctx.alive) rerun.disabled = false;
      }
    }
    rerun.addEventListener('click', run);
    run();
  }

  // step 4: helpers shared with publish.js (CONTRACT_STEP4 section 11)
  window.ACApp = { api, apiGet, el, append, swap, toast, makePoller, Queue, FormErrors, errorNotice, pageHead, loading, failBox, uuid, fmtBytes, fmtSecs, fmtDate, chip, bar, pad2, langAttr, cancelTask, taskLabel, LANG_NAMES, $main };

  // ------------------------------------------------------------------ boot
  function boot() {
    initTheme();
    if (!TOKEN || TOKEN === '__AUTOCLIPPER_TOKEN__') {
      Banner.show('auth', 'This page has no access token. Open it from the address that `autoclipper serve` prints, then reload.', true);
    }
    route(true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
