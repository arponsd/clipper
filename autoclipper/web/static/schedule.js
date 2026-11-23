/* Auto Clipper - G6 scheduled publishing + G7 cover picker UI.
 * - ACSchedule.coverCard(ctx, jobId, clip, lang, container): cover frame scrubber / image upload / title
 * - ACSchedule.scheduleBox(ctx, opts): the "Schedule" part of the clip Publish panel (publish.js)
 * - ACSchedule.viewSchedules(ctx): the #/schedule page (upcoming / done / missed / failed / cancelled)
 * Same rules as app.js: CSP script-src 'self', no inline handlers, no innerHTML for server data.
 * Images are fetched with the X-Auth-Token header and shown from blob: URLs (CSP img-src allows blob:).
 */
'use strict';

(() => {
  const PLATFORMS = ['youtube', 'facebook', 'instagram'];
  const PNAME = { youtube: 'YouTube', facebook: 'Facebook', instagram: 'Instagram' };
  const LNAME = { en: 'English', hi: 'Hindi' };
  const ITEM_CHIP = { scheduled: 'pending', queued: 'queued', running: 'running', handed_off: 'running', done: 'done',
    failed: 'failed', missed: 'warn', cancelled: 'cancelled', preview: 'pending' };
  const A = () => window.ACApp;
  const TOKEN = (() => { const m = document.querySelector('meta[name="autoclipper-token"]'); return m ? m.getAttribute('content') || '' : ''; })();

  async function fetchBlobUrl(path, signal) {
    const res = await fetch(path, { headers: { 'X-Auth-Token': TOKEN }, cache: 'no-store', credentials: 'same-origin', signal });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const j = await res.json(); if (j && j.message) msg = j.message; } catch (e) { /* not JSON */ }
      throw new Error(msg);
    }
    return URL.createObjectURL(await res.blob());
  }

  async function uploadRaw(path, file) {
    const res = await fetch(path, { method: 'POST', body: file, cache: 'no-store', credentials: 'same-origin',
      headers: { 'X-Auth-Token': TOKEN, Accept: 'application/json', 'Content-Type': file.type || 'application/octet-stream' } });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) {
      const errs = data && data.errors ? Object.values(data.errors).flat().join(' ') : '';
      throw new Error(((data && data.message) || `HTTP ${res.status}`) + (errs && errs !== (data && data.message) ? ` (${errs})` : ''));
    }
    return data;
  }

  // ---- time helpers (the browser's local time zone) -------------------------------------------
  const pad = (n) => String(n).padStart(2, '0');
  function localInputValue(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function withOffset(value) {
    // "2026-11-24T18:30" (datetime-local) -> "2026-11-24T18:30:00+06:00" using the browser's offset at that time
    const d = new Date(value);
    if (isNaN(d.getTime())) return null;
    const off = -d.getTimezoneOffset();
    const sign = off >= 0 ? '+' : '-';
    const a = Math.abs(off);
    return `${value.length === 16 ? value + ':00' : value}${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
  }
  function browserZone() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch (e) { return null; } }
  function fmtWhen(epoch) {
    try {
      return new Date(epoch * 1000).toLocaleString(undefined, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
    } catch (e) { return A().fmtDate(epoch); }
  }
  function defaultTime() {
    const d = new Date(Date.now() + 60 * 60 * 1000);
    d.setMinutes(Math.ceil(d.getMinutes() / 5) * 5, 0, 0);
    return localInputValue(d);
  }
  function modeText(it) {
    return it.mode === 'native' ? `handed to ${PNAME[it.platform]} before the time` : 'posted by Auto Clipper at the time';
  }
  const FINISHED = ['done', 'failed', 'missed', 'cancelled'];
  function errText(e) {
    const parts = Object.entries(e.errors || {}).map(([k, v]) => `${PNAME[k] || k}: ${[].concat(v).join(' ')}`);
    return parts.length ? ' (' + parts.join('; ') + ')' : '';
  }

  // ================================================================== COVER (G7)
  function coverCard(ctx, jobId, clip, lang, container) {
    const { el, swap, api, apiGet, toast, fmtBytes } = A();
    const base = `/api/jobs/${encodeURIComponent(jobId)}/clips/${clip}/${lang}/cover`;
    const body = el('div', { class: 'stack' });
    const card = el('section', { class: 'card stack cover-card', 'aria-labelledby': 'cover-h' },
      el('h2', { id: 'cover-h', text: 'Cover' }),
      el('p', { class: 'small muted', text: 'Pick a frame with the slider, or upload an image. YouTube gets it as the custom thumbnail, Facebook as the reel thumbnail, Instagram uses the picked frame (thumb_offset).' }),
      body);
    container.appendChild(card);
    body.appendChild(A().loading('Loading cover…'));

    const urls = [];
    ctx.onCleanup(() => urls.forEach((u) => URL.revokeObjectURL(u)));
    const keep = (u) => { urls.push(u); return u; };
    let view = null;
    let t = 0;
    let busy = false;
    let previewTimer = null;
    let previewAbort = null;
    const frameImg = el('img', { class: 'cover-img', alt: 'Frame preview', width: 270, height: 480 });
    const coverImg = el('img', { class: 'cover-img', alt: 'Current cover', width: 270, height: 480 });
    const slider = el('input', { type: 'range', id: 'cover-t', min: 0, step: 0.05, value: 0, 'data-fk': 'cover-t' });
    const tOut = el('output', { for: 'cover-t', class: 'mono', text: '0.00 s' });
    const title = el('input', { type: 'text', id: 'cover-title', maxlength: 100, autocomplete: 'off', lang: lang === 'hi' ? 'hi' : 'en', 'data-fk': 'cover-title' });
    const file = el('input', { type: 'file', id: 'cover-file', accept: 'image/jpeg,image/png,image/webp' });
    const err = el('div', { class: 'field-error', role: 'alert' });

    function schedulePreview() {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(async () => {
        if (previewAbort) previewAbort.abort();
        previewAbort = new AbortController();
        try {
          const u = await fetchBlobUrl(`${base}/frame?t=${t.toFixed(2)}`, previewAbort.signal);
          const old = frameImg.getAttribute('src');
          frameImg.src = keep(u);
          if (old && old.startsWith('blob:')) { URL.revokeObjectURL(old); const i = urls.indexOf(old); if (i >= 0) urls.splice(i, 1); }
          err.textContent = '';
        } catch (e) { if (e.name !== 'AbortError') err.textContent = e.message; }
      }, 200);
    }
    ctx.onCleanup(() => { clearTimeout(previewTimer); if (previewAbort) previewAbort.abort(); });

    slider.addEventListener('input', () => { t = Number(slider.value) || 0; tOut.textContent = t.toFixed(2) + ' s'; schedulePreview(); });

    async function load() {
      try {
        view = await apiGet(base);
        if (!ctx.alive) return;
        if (view.cover && view.cover.source === 'frame' && typeof view.cover.time === 'number') t = view.cover.time;
        if (view.cover && typeof view.cover.title === 'string' && !title.value) title.value = view.cover.title;
        render();
        if (view.cover) {
          try { coverImg.src = keep(await fetchBlobUrl(view.cover.url + `?v=${encodeURIComponent(view.cover.sha256 || '')}`)); } catch (e) { err.textContent = e.message; }
        }
        if (view.final) schedulePreview();
      } catch (e) { if (ctx.alive) swap(body, A().failBox(e, load)); }
    }

    async function save(source) {
      busy = true; render();
      try {
        const payload = { source, title: title.value.trim() || null };
        if (source === 'frame') payload.time = Math.round(t * 1000) / 1000;
        await api('PUT', base, { body: payload });
        toast('Cover saved. It is used the next time this clip is published.', 'ok');
      } catch (e) { toast(e.message + errText(e), 'error'); }
      finally { busy = false; if (ctx.alive) load(); }
    }

    async function upload() {
      const f = file.files && file.files[0];
      if (!f) return;
      if (view && f.size > view.max_upload_bytes) { toast(`The image is larger than ${fmtBytes(view.max_upload_bytes)}.`, 'error'); return; }
      busy = true; render();
      try {
        await uploadRaw(`${base}/image`, f);
        file.value = '';
        await save('upload');
      } catch (e) { toast(e.message, 'error'); busy = false; if (ctx.alive) render(); }
    }
    file.addEventListener('change', upload);

    async function remove() {
      busy = true; render();
      try { await api('DELETE', base); coverImg.removeAttribute('src'); toast('Cover removed: the platforms pick their default frame.', 'ok'); }
      catch (e) { toast(e.message, 'error'); }
      finally { busy = false; if (ctx.alive) load(); }
    }

    function render() {
      if (!view) return;
      if (!view.final) { swap(body, el('p', { class: 'notice notice-warn', text: 'Render the clip first; the cover is picked from the final video.' })); return; }
      const max = Math.max(0, (view.duration || 0) - 0.05);
      slider.max = String(max.toFixed(2));
      slider.value = String(Math.min(t, max));
      tOut.textContent = Number(slider.value).toFixed(2) + ' s';
      slider.disabled = busy;
      const c = view.cover;
      const current = c
        ? el('figure', { class: 'cover-fig' }, coverImg,
          el('figcaption', { class: 'small' },
            c.source === 'frame' ? `Frame at ${Number(c.time).toFixed(2)} s` : 'Uploaded image',
            c.title ? el('span', { lang: lang === 'hi' ? 'hi' : 'en', text: ` · “${c.title}”` }) : null,
            ` · ${fmtBytes(c.size)}`,
            c.outdated ? el('div', { class: 'pub-warn-text', text: 'Made from an earlier render - pick it again.' }) : null))
        : el('p', { class: 'small muted', text: 'No cover picked: each platform uses its default frame.' });
      swap(body,
        el('div', { class: 'cover-grid' },
          el('div', { class: 'stack' },
            el('h3', { class: 'h-small', text: 'Current cover' }), current,
            c ? el('button', { type: 'button', class: 'btn btn-small', text: 'Remove cover', disabled: busy, on: { click: remove } }) : null),
          el('div', { class: 'stack' },
            el('h3', { class: 'h-small', text: 'Pick a frame' }),
            el('figure', { class: 'cover-fig' }, frameImg),
            el('div', { class: 'field' }, el('label', { for: 'cover-t', text: 'Frame time' }), el('div', { class: 'row' }, slider, tOut)),
            el('div', { class: 'field' }, el('label', { for: 'cover-title', text: 'Title on the cover (optional, up to 100 characters)' }), title,
              el('div', { class: 'hint', text: 'Burned into cover.jpg at the top. Instagram shows the frame without it.' })),
            el('div', { class: 'row' },
              el('button', { type: 'button', class: 'btn btn-primary', text: 'Use this frame', disabled: busy, on: { click: () => save('frame') } }),
              view.has_upload ? el('button', { type: 'button', class: 'btn', text: 'Use the uploaded image', disabled: busy, on: { click: () => save('upload') } }) : null),
            el('div', { class: 'field' }, el('label', { for: 'cover-file', text: 'Or upload an image (JPEG, PNG or WebP, up to 10 MB)' }), file,
              el('div', { class: 'hint', text: 'Cropped to 1080×1920. Not used on Instagram (its API needs a public image URL).' })),
            err)));
      file.disabled = busy;
      title.disabled = busy;
    }
    load();
  }

  // ================================================================== SCHEDULE BOX (G6, in the Publish panel)
  /** opts: {jobId, clip, lang, platforms: () => [..], onPlans: (plans) => void, ready: (p) => bool, reason: (p) => str} */
  function scheduleBox(ctx, opts) {
    const { el, swap, api, apiGet, toast, uuid } = A();
    const root = el('div', { class: 'stack sched-box' });
    const when = el('input', { type: 'datetime-local', id: 'sched-when', value: defaultTime(), step: 60, 'data-fk': 'sched-when' });
    function setBounds(input) { // the API takes 2 minutes to 90 days ahead
      input.min = localInputValue(new Date(Date.now() + 3 * 60 * 1000));
      input.max = localInputValue(new Date(Date.now() + 90 * 24 * 3600 * 1000 - 60 * 1000));
    }
    setBounds(when);
    when.addEventListener('focus', () => setBounds(when));
    const list = el('div', { class: 'stack' });
    let dry = true;
    let busy = false;
    let rows = [];
    const q = `?job_id=${encodeURIComponent(opts.jobId)}&clip=${opts.clip}&lang=${opts.lang}&state=all`;

    async function refresh() {
      try { const d = await apiGet('/api/schedules' + q); rows = d.schedules || []; if (ctx.alive) renderList(); } catch (e) { /* the panel shows errors */ }
    }
    function renderList() {
      const upcoming = rows.filter((s) => s.state === 'upcoming');
      const recent = rows.filter((s) => s.state !== 'upcoming').slice(0, 3);
      swap(list, upcoming.length || recent.length
        ? el('ul', { class: 'sched-mini' }, [...upcoming, ...recent].map((s) => el('li', null,
          el('strong', { text: fmtWhen(s.publish_at) }), ' ', A().chip(s.state === 'upcoming' ? 'queued' : (s.state === 'done' ? 'done' : (s.state === 'cancelled' ? 'cancelled' : 'failed')), s.state), ' ',
          Object.values(s.items).map((it) => el('span', { class: 'small muted', text: `${PNAME[it.platform]}: ${it.state.replace('_', ' ')}` + (it.mode === 'native' && it.state === 'scheduled' && it.handoff_at ? ` (hand-off ${fmtWhen(it.handoff_at)})` : '') + ' · ' })),
          el('a', { href: '#/schedule', text: 'Schedule page' }))))
        : null);
    }

    function selected() { return opts.platforms(); }

    async function submit() {
      const platforms = selected();
      if (!platforms.length) { toast('Choose at least one platform.', 'error'); return; }
      const at = withOffset(when.value);
      if (!at) { when.setAttribute('aria-invalid', 'true'); toast('Pick a date and time.', 'error'); return; }
      when.removeAttribute('aria-invalid');
      const body = { job_id: opts.jobId, clip: opts.clip, lang: opts.lang, platforms, publish_at: at, timezone: browserZone() };
      if (dry) {
        busy = true; render();
        try {
          const r = await api('POST', '/api/schedules', { body: { ...body, dry_run: true } });
          opts.onPlans((r.data && r.data.plans) || {});
          const s = r.data.schedule;
          toast(`Dry run: nothing was scheduled. Plan for ${fmtWhen(s.publish_at)}: ` + Object.values(s.items).map((it) => `${PNAME[it.platform]} ${modeText(it)}`).join(', '), 'ok');
        } catch (e) { toast(e.message + errText(e), 'error'); }
        finally { busy = false; if (ctx.alive) render(); }
        return;
      }
      openConfirm(body);
    }

    async function openConfirm(body) {
      // fresh plans first (dry run, no network): the dialog shows exactly what will go out, like Publish now
      busy = true; render();
      let fresh = null;
      try {
        const r = await api('POST', '/api/schedules', { body: { ...body, dry_run: true } });
        fresh = r.data || null;
        if (fresh && fresh.plans) opts.onPlans(fresh.plans);
      } catch (e) { toast(e.message + errText(e), 'error'); }
      finally { busy = false; if (ctx.alive) render(); }
      if (!fresh || !ctx.alive) return;
      const blocked = body.platforms.filter((p) => fresh.plans[p] && fresh.plans[p].problems && fresh.plans[p].problems.length);
      if (blocked.length) {
        toast(`Cannot schedule: ${blocked.map((p) => PNAME[p] + ': ' + fresh.plans[p].problems.join('; ')).join(' | ')}`, 'error');
        return;
      }
      const key = uuid();
      const ok = el('button', { type: 'submit', class: 'btn btn-danger', value: 'ok', text: 'Schedule' });
      const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
      const at = new Date(body.publish_at).getTime() / 1000;
      const form = el('form', { method: 'dialog', class: 'stack' },
        el('h2', { id: 'sched-dlg-h', text: `Schedule ${LNAME[opts.lang]} clip ${A().pad2(opts.clip)}?` }),
        el('p', null, el('strong', { text: fmtWhen(at) })),
        el('ul', { class: 'pub-dialog-list' }, body.platforms.map((p) => {
          const pl = fresh.plans[p] || {};
          const it = (fresh.schedule && fresh.schedule.items && fresh.schedule.items[p]) || {};
          const md = pl.metadata || {};
          const title = (md.snippet && md.snippet.title) || md.title || (md.caption ? String(md.caption).split('\n')[0] : '');
          const privacy = md.status && md.status.privacyStatus;
          const acc = pl.account ? (pl.account.name || pl.account.id) : '?';
          const finalPrivacy = p === 'youtube' ? (it.mode === 'native' ? 'public' : (privacy || 'private')) : 'public';
          const native = it.mode === 'native';
          return el('li', null,
            el('strong', { text: PNAME[p] }), ' → ', el('span', { text: acc }),
            p === 'youtube' ? el('span', { class: 'muted', text: ` · ${finalPrivacy}` }) : null,
            el('div', { class: 'pub-dialog-title', lang: opts.lang === 'hi' ? 'hi' : 'en', text: title }),
            el('div', { class: 'small', text: native
              ? `Handed to ${PNAME[p]} at ${fmtWhen(it.handoff_at)}; ${PNAME[p]} publishes it at the time.`
              : 'Posted by Auto Clipper at the time: keep the server running.' }),
            it.mode_note ? el('div', { class: 'small muted', text: it.mode_note }) : null,
            pl.cover ? el('div', { class: 'small muted', text: 'Cover: ' + pl.cover.use }) : null,
            finalPrivacy === 'public' ? el('div', { class: 'pub-public', text: `This posts publicly to ${acc} on ${PNAME[p]}.` }) : null,
            native ? el('div', { class: 'small pub-warn-text', text: `Once handed to ${PNAME[p]}, cancelling here or turning dry run on no longer stops it; change or delete it on ${PNAME[p]}.` }) : null);
        })),
        el('p', { class: 'pub-public', text: 'This counts as your confirmation: it posts for real at that time without asking again. You can cancel it in Auto Clipper until it starts (or until it is handed to the platform).' }),
        el('div', { class: 'row' }, ok, cancel));
      const dlg = el('dialog', { class: 'pub-dialog', 'aria-labelledby': 'sched-dlg-h' }, form);
      document.body.appendChild(dlg);
      const close = () => { try { dlg.close(); } catch (e) { /* ignore */ } dlg.remove(); };
      ctx.onCleanup(close);
      cancel.addEventListener('click', close);
      dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        ok.disabled = true;
        try {
          const r = await api('POST', '/api/schedules', { body: { ...body, dry_run: false, confirm: true }, headers: { 'Idempotency-Key': key } });
          close();
          const s = r.data.schedule;
          toast(`Scheduled for ${fmtWhen(s.publish_at)}: ` + Object.values(s.items).map((it) => `${PNAME[it.platform]} ${modeText(it)}`).join(', '), 'ok');
          refresh();
        } catch (err) { ok.disabled = false; toast(err.message + errText(err), 'error'); }
      });
      dlg.showModal();
      cancel.focus();
    }

    function render() {
      const btn = el('button', { type: 'button', class: dry ? 'btn' : 'btn btn-danger', text: dry ? 'Preview schedule (dry run)' : 'Schedule…', disabled: busy || !selected().length, on: { click: submit } });
      swap(root,
        el('h3', { class: 'h-small', text: 'Schedule for later' }),
        el('div', { class: 'row' },
          el('div', { class: 'field' }, el('label', { for: 'sched-when', text: `Date and time (${browserZone() || 'your time zone'})` }), when),
          btn),
        el('p', { class: 'small muted', text: dry
          ? 'Dry run is on: this only shows the plan with the time; nothing is stored or posted.'
          : 'YouTube (public videos) and Facebook schedule natively; Instagram and the rest are posted by Auto Clipper at the time.' }),
        list);
    }

    refresh();
    render();
    return { node: root, update(isDry) { dry = !!isDry; render(); }, refresh };
  }

  // ================================================================== SCHEDULE PAGE (G6)
  function viewSchedules(ctx) {
    const { el, swap, api, apiGet, toast, makePoller, $main, pageHead, chip } = A();
    const FILTERS = ['upcoming', 'done', 'missed', 'failed', 'cancelled', 'all'];
    let filter = 'upcoming';
    let data = null;
    const body = el('div', { class: 'stack' });
    const tabs = el('div', { class: 'row sched-tabs', role: 'group', 'aria-label': 'Filter' });
    $main.append(pageHead('Schedule'), el('p', { class: 'muted', text: 'Times are shown in your time zone. Scheduled posts are sent by the running server (or handed to YouTube/Facebook before the time).' }), tabs, body);
    body.appendChild(A().loading('Loading schedules…'));

    function renderTabs() {
      swap(tabs, FILTERS.map((f) => el('button', { type: 'button', class: 'btn btn-small' + (f === filter ? ' btn-primary' : ''), 'aria-pressed': f === filter ? 'true' : 'false', 'data-fk': 'tab-' + f, text: f[0].toUpperCase() + f.slice(1),
        on: { click: () => { filter = f; renderTabs(); poller.kick(); } } })));
    }

    async function cancel(s, platform) {
      const what = platform ? `the ${PNAME[platform]} part of this schedule` : 'this schedule';
      if (!window.confirm(`Cancel ${what} (${fmtWhen(s.publish_at)})? This cannot be undone; schedule it again if needed.`)) return;
      try {
        const r = await api('POST', `/api/schedules/${s.id}/cancel`, platform ? { body: { platforms: [platform] } } : undefined);
        const refused = r.data && r.data.refused ? Object.entries(r.data.refused) : [];
        toast(refused.length ? 'Cancelled what had not started. ' + refused.map(([p, w]) => `${PNAME[p]}: ${w.join(' ')}`).join(' ') : 'Cancelled.', refused.length ? 'error' : 'ok');
      } catch (e) { toast(e.message + errText(e), 'error'); }
      poller.kick();
    }

    function editor(s) {
      const input = el('input', { type: 'datetime-local', id: `sched-edit-${s.id}`, value: localInputValue(new Date(s.publish_at * 1000)), step: 60 });
      input.min = localInputValue(new Date(Date.now() + 3 * 60 * 1000));
      input.max = localInputValue(new Date(Date.now() + 90 * 24 * 3600 * 1000 - 60 * 1000));
      const save = el('button', { type: 'button', class: 'btn btn-small', text: 'Change time' });
      save.addEventListener('click', async () => {
        const at = withOffset(input.value);
        if (!at) { toast('Pick a date and time.', 'error'); return; }
        save.disabled = true;
        try { await api('PATCH', `/api/schedules/${s.id}`, { body: { publish_at: at, timezone: browserZone() } }); toast('Schedule moved.', 'ok'); }
        catch (e) { toast(e.message + errText(e), 'error'); }
        save.disabled = false;
        poller.kick();
      });
      return el('details', { class: 'sched-edit' }, el('summary', { text: 'Change the time' }),
        el('div', { class: 'row' }, el('label', { for: input.id, class: 'sr-only', text: 'New date and time' }), input, save));
    }

    function card(s) {
      const items = Object.values(s.items || {});
      const anyCancel = items.some((it) => it.cancellable);
      const editable = items.every((it) => it.state === 'scheduled' || it.state === 'cancelled') && items.some((it) => it.state === 'scheduled');
      return el('article', { class: 'card stack sched-card' },
        el('div', { class: 'row sched-head' },
          el('strong', { class: 'sched-when', text: fmtWhen(s.publish_at) }),
          chip(s.state === 'upcoming' ? 'queued' : (s.state === 'missed' ? 'warn' : s.state), s.state),
          el('a', { href: `#/jobs/${s.job_id}/clips/${s.clip}/${s.lang}`, text: `${s.job_id} · clip ${A().pad2(s.clip)} · ${LNAME[s.lang] || s.lang}` })),
        s.title ? el('div', { class: 'sched-title', lang: s.lang === 'hi' ? 'hi' : 'en', text: s.title }) : null,
        el('ul', { class: 'sched-items' }, items.map((it) => el('li', null,
          el('div', { class: 'row' }, el('strong', { text: PNAME[it.platform] }), chip(ITEM_CHIP[it.state] || 'pending', it.state.replace('_', ' ')),
            el('span', { class: 'small muted', text: it.mode === 'native' ? `handed to ${PNAME[it.platform]}` : 'posted by Auto Clipper' }),
            it.account ? el('span', { class: 'small', text: '→ ' + (it.account.name || it.account.id) }) : null,
            it.cancellable && items.length > 1 ? el('button', { type: 'button', class: 'btn btn-small', text: 'Cancel ' + PNAME[it.platform], on: { click: () => cancel(s, it.platform) } }) : null),
          it.message ? el('div', { class: 'small', text: it.message }) : null,
          it.mode === 'native' && it.state === 'scheduled' && it.handoff_at
            ? el('div', { class: 'small', text: `Hand-off to ${PNAME[it.platform]} at ${fmtWhen(it.handoff_at)}: keep Auto Clipper running then.` }) : null,
          it.mode_note && !FINISHED.includes(it.state) ? el('div', { class: 'small muted', text: it.mode_note }) : null,
          it.url && /^https:\/\//.test(it.url) ? el('a', { class: 'small', href: it.url, target: '_blank', rel: 'noopener noreferrer', text: it.url }) : null))),
        el('div', { class: 'row' },
          anyCancel ? el('button', { type: 'button', class: 'btn btn-small btn-danger', text: 'Cancel schedule', on: { click: () => cancel(s, null) } }) : null),
        editable ? editor(s) : null);
    }

    function render() {
      if (!data) return;
      const rows = data.schedules || [];
      const notes = [];
      if (data.dry_run) notes.push(el('p', { class: 'notice notice-ok', text: 'Dry run is ON: new schedules are only previewed, and a schedule that comes due while dry run is on is not posted.' }));
      if (data.dry_run && data.handed_off) notes.push(el('p', { class: 'notice notice-warn', text: `${data.handed_off} scheduled post(s) were already handed to YouTube/Facebook: dry run does not stop those. Change or delete them on the platform.` }));
      if (!data.scheduler || !data.scheduler.running) notes.push(el('p', { class: 'notice notice-warn', text: (data.scheduler && data.scheduler.blocked) || 'The scheduler is not running in this server.' }));
      swap(body, notes, rows.length ? rows.map(card) : el('p', { class: 'card empty', text: filter === 'upcoming' ? 'Nothing scheduled. Schedule a clip from its Publish panel.' : 'Nothing here.' }));
    }

    const poller = makePoller(ctx, 10000, async () => {
      data = await apiGet('/api/schedules?state=' + filter);
      if (ctx.alive) render();
      return true;
    });
    renderTabs();
  }

  window.ACSchedule = { coverCard, scheduleBox, viewSchedules };
})();
