/* Auto Clipper - step 4 publishing UI: Accounts page + Publish panel on the clip review page.
 * Same rules as app.js: CSP script-src 'self', no inline handlers, no innerHTML for server data.
 * Uses the helpers app.js exports as window.ACApp. Contract: docs/CONTRACT_STEP4.md sections 9 and 11.
 * Tokens typed here go straight into one POST and are cleared; the server never sends a token back.
 */
'use strict';

(() => {
  const PLATFORMS = ['youtube', 'facebook', 'instagram'];
  const PNAME = { youtube: 'YouTube', facebook: 'Facebook', instagram: 'Instagram' };
  const LANGS = ['en', 'hi'];
  const LNAME = { en: 'English', hi: 'Hindi' };
  const ACTIVE_STATES = ['queued', 'uploading', 'processing', 'publishing'];
  const CONNECT_POLL_MS = 2000;
  const CONNECT_POLL_MAX_MS = 10 * 60 * 1000;
  const FORCE_WORD = 'again';
  const ERROR_KIND = {
    auth: 'Login expired', quota: 'Quota used up', rate_limit: 'Rate limit', invalid: 'Rejected',
    unsupported_video: 'Video not accepted', transient: 'Temporary error', processing: 'Processing failed',
    network: 'Network error', unknown: 'Error',
  };

  const A = () => window.ACApp;
  const lang = (l) => (l === 'hi' ? 'hi' : 'en');
  const deepClone = (o) => JSON.parse(JSON.stringify(o));

  function getPath(obj, path) { return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj); }
  function setPath(obj, path, v) {
    const parts = path.split('.');
    let o = obj;
    for (let i = 0; i < parts.length - 1; i++) { if (o[parts[i]] == null) o[parts[i]] = {}; o = o[parts[i]]; }
    o[parts[parts.length - 1]] = v;
  }
  let uid = 0;
  const nextId = (p) => `${p}-${++uid}`;

  function fieldWrap(errs, path, label, control, hint) {
    const { el } = A();
    const id = control.id || nextId('pf');
    control.id = id;
    const err = el('div', { class: 'field-error', id: id + '-err' });
    const hintEl = hint ? el('div', { class: 'hint', id: id + '-hint', text: hint }) : null;
    control.setAttribute('aria-describedby', [hintEl ? id + '-hint' : null, id + '-err'].filter(Boolean).join(' '));
    const isCheck = control.type === 'checkbox';
    const w = isCheck
      ? el('div', { class: 'field' }, el('label', { class: 'check', for: id }, control, ' ', label), hintEl, err)
      : el('div', { class: 'field' }, el('label', { for: id, text: label }), control, hintEl, err);
    if (errs) errs.register(path, w, err);
    return w;
  }

  function asStatus(as) {
    if (!as) return { text: 'not connected', cls: 'pending' };
    if (as.connected) return { text: 'connected', cls: as.problems && as.problems.length ? 'warn' : 'ok' };
    return { text: 'not connected', cls: 'pending' };
  }

  function problemsList(problems) {
    const { el } = A();
    if (!problems || !problems.length) return null;
    return el('ul', { class: 'pub-problems' }, problems.map((p) => el('li', { text: p })));
  }

  // ================================================================== ACCOUNTS PAGE
  function viewAccounts(ctx) {
    const { el, append, swap, api, apiGet, toast, pageHead, loading, failBox, errorNotice, FormErrors, fmtDate, chip, $main } = A();
    const head = pageHead('Accounts & publishing', null);
    const content = el('div', { class: 'stack' });
    append($main, head, content);
    content.appendChild(loading('Loading accounts…'));

    let accounts = null;
    let saved = null;   // PublishSettings from the server
    let draft = null;   // edited copy
    let dirty = false;
    let connectPoll = null;
    ctx.guard = () => (dirty ? 'You have unsaved publishing settings. Leave without saving?' : null);
    ctx.onCleanup(() => { if (connectPoll) clearTimeout(connectPoll.timer); });

    function load() {
      swap(content, loading('Loading accounts…'));
      Promise.all([apiGet('/api/publish/accounts'), apiGet('/api/publish/settings')]).then(([a, s]) => {
        if (!ctx.alive) return;
        accounts = a;
        saved = s;
        draft = deepClone(s);
        dirty = false;
        render();
      }).catch((e) => { if (ctx.alive) swap(content, failBox(e, load)); });
    }

    async function refreshAccounts() {
      const a = await apiGet('/api/publish/accounts');
      if (!ctx.alive) return null;
      accounts = a;
      return a;
    }

    function render() {
      const summary = errorNotice();
      const errs = new FormErrors(summary);
      swap(content,
        dryRunCard(),
        mappingCard(),
        googleClientCard(),
        LANGS.map((l) => langCard(l, errs)),
        advancedCard(errs),
        saveBar(errs, summary));
    }

    // ----- dry run switch (saved immediately)
    function dryRunCard() {
      const on = !!(saved && saved.dry_run);
      const box = el('input', { type: 'checkbox', checked: on, id: 'pub-dry' });
      const notice = on
        ? el('p', { class: 'notice notice-ok pub-dry-notice', role: 'status' },
          el('strong', { text: 'Dry run is ON - nothing is posted.' }),
          ' Publish only builds and shows the exact request plan.')
        : el('p', { class: 'notice notice-error pub-dry-notice', role: 'status' },
          el('strong', { text: 'Dry run is OFF - Publish posts for real.' }),
          ' Clips are uploaded to the connected accounts after you confirm each publish.');
      box.addEventListener('change', async () => {
        const want = box.checked;
        if (!want && !confirm('Turn dry run off? After this, confirming a publish uploads the clip to YouTube / Facebook / Instagram for real.')) {
          box.checked = true;
          return;
        }
        box.disabled = true;
        try {
          const body = deepClone(saved);
          body.dry_run = want;
          const r = await api('PUT', '/api/publish/settings', { body });
          if (!ctx.alive) return;
          saved = r.data;
          draft.dry_run = saved.dry_run;
          if (accounts) accounts.dry_run = saved.dry_run;
          toast(want ? 'Dry run is on: nothing will be posted.' : 'Dry run is off: Publish now posts for real.', want ? 'ok' : undefined);
          render();
        } catch (e) {
          if (!ctx.alive) return;
          box.checked = !want;
          box.disabled = false;
          toast(e.message, 'error');
        }
      });
      return el('section', { class: 'card stack', 'aria-labelledby': 'pub-dry-h' },
        el('h2', { id: 'pub-dry-h', text: 'Publishing mode' }),
        notice,
        el('label', { class: 'check pub-switch', for: 'pub-dry' }, box, ' ', 'Dry run (nothing is sent to any platform)'));
    }

    // ----- mapping summary
    function mappingCard() {
      const rows = LANGS.map((l) => {
        const langs = (accounts && accounts.langs && accounts.langs[l]) || {};
        return el('tr', null, el('th', { scope: 'row', text: `${LNAME[l]} clips` }),
          PLATFORMS.map((p) => {
            const as = langs[p];
            const name = as && as.connected ? (as.account_name || as.account_id || 'connected') : 'not connected';
            const off = as && as.connected && !as.enabled ? ' (off)' : '';
            return el('td', null, el('span', { class: 'pub-acc', text: name + off }));
          }));
      });
      return el('section', { class: 'card stack', 'aria-labelledby': 'pub-map-h' },
        el('h2', { id: 'pub-map-h', text: 'What is posted where' }),
        el('p', { class: 'small muted', text: 'English clips only go to the English accounts and Hindi clips only to the Hindi accounts. A platform marked (off) is connected but not enabled.' }),
        el('div', { class: 'table-wrap' }, el('table', { class: 'pub-map' },
          el('thead', null, el('tr', null, el('th', { scope: 'col', text: 'Clip language' }),
            PLATFORMS.map((p) => el('th', { scope: 'col', text: PNAME[p] })))),
          el('tbody', null, rows))));
    }

    // ----- Google OAuth client
    function googleClientCard() {
      const g = (accounts && accounts.google_client) || { configured: false, client_id_hint: null };
      const file = el('input', { type: 'file', accept: '.json,application/json', id: 'pub-gclient' });
      const msg = el('div', { class: 'field-error', id: 'pub-gclient-err', role: 'alert' });
      file.setAttribute('aria-describedby', 'pub-gclient-hint pub-gclient-err');
      file.addEventListener('change', () => {
        const f = file.files && file.files[0];
        msg.textContent = '';
        if (!f) return;
        if (f.size > 64 * 1024) { msg.textContent = 'This file is larger than 64 KiB; choose the client_secret.json you downloaded.'; file.value = ''; return; }
        const reader = new FileReader();
        reader.addEventListener('load', async () => {
          let data;
          try { data = JSON.parse(String(reader.result)); } catch (e) { msg.textContent = 'This is not a JSON file.'; file.value = ''; return; }
          try {
            await api('PUT', '/api/publish/google-client', { body: data });
            if (!ctx.alive) return;
            toast('Google client saved.', 'ok');
            await refreshAccounts();
            if (ctx.alive) render();
          } catch (e) {
            if (!ctx.alive) return;
            const extra = Object.values(e.errors || {}).flat().join(' ');
            msg.textContent = e.message + (extra ? ': ' + extra : '');
          } finally {
            file.value = '';
            data = null;
          }
        });
        reader.addEventListener('error', () => { msg.textContent = 'The file could not be read.'; });
        reader.readAsText(f);
      });
      const remove = el('button', { type: 'button', class: 'btn btn-danger btn-small', text: 'Remove client', hidden: !g.configured,
        on: { click: async () => {
          if (!confirm('Remove the Google OAuth client? Connected YouTube channels stay connected, but you cannot connect new ones until you add a client again.')) return;
          try { await api('DELETE', '/api/publish/google-client'); await refreshAccounts(); if (ctx.alive) render(); } catch (e) { toast(e.message, 'error'); }
        } } });
      return el('section', { class: 'card stack', 'aria-labelledby': 'pub-g-h' },
        el('h2', { id: 'pub-g-h', text: 'Google OAuth client (for YouTube)' }),
        el('p', null, chip(g.configured ? 'ok' : 'pending', g.configured ? 'configured' : 'not configured'), ' ',
          g.client_id_hint ? el('span', { class: 'mono small', text: g.client_id_hint }) : null),
        el('div', { class: 'field' },
          el('label', { for: 'pub-gclient', text: g.configured ? 'Replace client_secret.json' : 'Upload client_secret.json' }),
          file,
          el('div', { class: 'hint', id: 'pub-gclient-hint', text: 'The file you download for an OAuth client of type "Desktop app" (see README). The secret stays on this computer and is never shown again.' }),
          msg),
        el('div', { class: 'row' }, remove));
    }

    // ----- one language
    function langCard(l, errs) {
      const langs = (accounts && accounts.langs && accounts.langs[l]) || {};
      return el('section', { class: 'card stack pub-lang', 'aria-labelledby': `pub-${l}-h` },
        el('h2', { id: `pub-${l}-h`, text: `${LNAME[l]} clips` }),
        el('div', { class: 'pub-platforms' },
          youtubeBlock(l, langs.youtube, errs),
          metaBlock(l, langs.facebook, langs.instagram, errs)));
    }

    function statusLine(as) {
      const s = asStatus(as);
      return el('div', { class: 'pub-status-line' }, chip(s.cls, s.text),
        as && as.connected && as.account_name ? el('span', { class: 'pub-acc', text: ' ' + as.account_name }) : null,
        as && as.connected && as.account_id ? el('span', { class: 'mono small muted', text: ' ' + as.account_id }) : null,
        as && as.connected_at ? el('span', { class: 'small muted', text: ' · since ' + fmtDate(as.connected_at) }) : null);
    }

    function bindCheck(path, label, hint, errs) {
      const c = el('input', { type: 'checkbox', checked: !!getPath(draft, path) });
      c.addEventListener('change', () => { setPath(draft, path, c.checked); markDirty(); });
      return fieldWrap(errs, path, label, c, hint);
    }
    function bindSelect(path, label, options, hint, errs) {
      const s = el('select', null, options.map(([v, t]) => el('option', { value: v, text: t, selected: getPath(draft, path) === v })));
      s.addEventListener('change', () => { setPath(draft, path, s.value); markDirty(); });
      return fieldWrap(errs, path, label, s, hint);
    }
    function bindText(path, label, hint, errs, attrs) {
      const t = el('input', Object.assign({ type: 'text', value: getPath(draft, path) ?? '' }, attrs || {}));
      t.addEventListener('input', () => { setPath(draft, path, t.value.trim()); markDirty(); });
      return fieldWrap(errs, path, label, t, hint);
    }
    function bindNumber(path, label, hint, errs, attrs) {
      const t = el('input', Object.assign({ type: 'number', value: String(getPath(draft, path) ?? '') }, attrs || {}));
      t.addEventListener('input', () => {
        // the server is strict: a non-number or a fraction for an int field comes back as a field error
        setPath(draft, path, t.value.trim() === '' ? null : Number(t.value));
        markDirty();
      });
      return fieldWrap(errs, path, label, t, hint);
    }

    function youtubeBlock(l, as, errs) {
      const p = `${l}.youtube`;
      const g = (accounts && accounts.google_client) || {};
      const connectBtn = el('button', { type: 'button', class: 'btn btn-primary btn-small', text: as && as.connected ? 'Reconnect' : 'Connect YouTube', disabled: !g.configured });
      const waitLine = el('p', { class: 'small muted', 'aria-live': 'polite' });
      connectBtn.addEventListener('click', () => connectYouTube(l, connectBtn, waitLine));
      const disc = as && as.connected ? el('button', { type: 'button', class: 'btn btn-small', text: 'Disconnect',
        on: { click: () => disconnect(l, 'youtube') } }) : null;
      return el('div', { class: 'pub-block stack' },
        el('h3', { text: 'YouTube Shorts' }),
        statusLine(as),
        problemsList(as && as.problems),
        !g.configured ? el('p', { class: 'small muted', text: 'Upload the Google client file above first.' }) : null,
        el('div', { class: 'row' }, connectBtn, disc),
        waitLine,
        el('div', { class: 'fields' },
          bindCheck(`${p}.enabled`, `Publish ${LNAME[l]} clips to this channel`, null, errs),
          bindSelect(`${p}.privacy`, 'Privacy', [['private', 'Private (only you)'], ['unlisted', 'Unlisted (anyone with the link)'], ['public', 'Public']],
            'Uploads stay private until you change this.', errs),
          bindText(`${p}.category_id`, 'Category id', '22 = People & Blogs, 24 = Entertainment, 27 = Education.', errs, { inputmode: 'numeric', maxlength: '3' }),
          bindText(`${p}.channel_id`, 'Channel id', 'Filled in by Connect. Publishing is refused when the connected channel differs.', errs, { readOnly: true, class: 'mono' }),
          bindCheck(`${p}.made_for_kids`, 'Made for kids', null, errs),
          bindCheck(`${p}.contains_synthetic_media`, 'Disclose altered or synthetic content', 'On by default: the narration is an AI voice.', errs),
          bindCheck(`${p}.notify_subscribers`, 'Notify subscribers', null, errs),
          bindCheck(`${p}.add_shorts_hashtag`, 'Add #Shorts to the description', null, errs)));
    }

    function metaBlock(l, fb, ig, errs) {
      const ids = { page: nextId('pub-page'), tok: nextId('pub-tok'), ig: nextId('pub-ig'), igtok: nextId('pub-igtok') };
      const pageId = el('input', { type: 'text', id: ids.page, inputmode: 'numeric', autocomplete: 'off', maxlength: '32',
        value: (fb && fb.account_id) || getPath(draft, `${l}.facebook.page_id`) || '' });
      const pageTok = el('input', { type: 'password', id: ids.tok, autocomplete: 'off', spellcheck: 'false', maxlength: '1024' });
      const igId = el('input', { type: 'text', id: ids.ig, inputmode: 'numeric', autocomplete: 'off', maxlength: '32',
        value: (ig && ig.account_id) || getPath(draft, `${l}.instagram.ig_user_id`) || '' });
      const igTok = el('input', { type: 'password', id: ids.igtok, autocomplete: 'off', spellcheck: 'false', maxlength: '1024' });
      const summary = errorNotice();
      const ferrs = new FormErrors(summary);
      const submit = el('button', { type: 'submit', class: 'btn btn-primary btn-small', text: fb && fb.connected ? 'Replace token' : 'Connect Facebook / Instagram' });
      const form = el('form', { class: 'stack', novalidate: true, 'aria-label': `Connect Facebook and Instagram for ${LNAME[l]} clips` },
        el('div', { class: 'fields' },
          fieldWrap(ferrs, 'page_id', 'Facebook Page id', pageId, 'Digits only, from /me/accounts.'),
          fieldWrap(ferrs, 'page_token', 'Page access token', pageTok, 'Long-lived Page token. Sent once, stored in data/secrets, never shown again.'),
          fieldWrap(ferrs, 'ig_user_id', 'Instagram account id (optional)', igId, 'instagram_business_account id of the Page. Leave empty for Facebook only.'),
          fieldWrap(ferrs, 'ig_token', 'Instagram token (optional)', igTok, 'Only if Instagram needs a different token; default is the Page token.')),
        summary,
        el('div', { class: 'row' }, submit));
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        ferrs.clear();
        const body = {
          page_id: pageId.value.trim(),
          page_token: pageTok.value.trim(),
          ig_user_id: igId.value.trim() || null,
          ig_token: igTok.value.trim() || null,
        };
        const client = {};
        if (!/^[0-9]{1,32}$/.test(body.page_id)) client.page_id = ['Enter the numeric Page id.'];
        if (!/^\S{20,1024}$/.test(body.page_token)) client.page_token = ['Paste the Page access token (20-1024 characters, no spaces).'];
        if (body.ig_user_id !== null && !/^[0-9]{1,32}$/.test(body.ig_user_id)) client.ig_user_id = ['Digits only.'];
        if (body.ig_token !== null && !/^\S{20,1024}$/.test(body.ig_token)) client.ig_token = ['20-1024 characters, no spaces.'];
        if (Object.keys(client).length) { ferrs.show({ message: 'Please fix the highlighted fields.', errors: client }); body.page_token = body.ig_token = null; return; }
        submit.disabled = true;
        try {
          await api('POST', `/api/publish/accounts/${l}/meta/connect`, { body });
          if (!ctx.alive) return;
          toast(`Facebook${body.ig_user_id ? ' and Instagram' : ''} connected for ${LNAME[l]} clips.`, 'ok');
          await reloadAfterConnect();
        } catch (err) {
          if (ctx.alive) ferrs.show(err);
        } finally {
          pageTok.value = '';
          igTok.value = '';
          body.page_token = body.ig_token = null;
          submit.disabled = false;
        }
      });
      const discBtns = [];
      if (fb && fb.connected) discBtns.push(el('button', { type: 'button', class: 'btn btn-small', text: 'Disconnect Facebook + Instagram', on: { click: () => disconnect(l, 'meta') } }));
      if (ig && ig.connected) discBtns.push(el('button', { type: 'button', class: 'btn btn-small', text: 'Disconnect Instagram only', on: { click: () => disconnect(l, 'instagram') } }));
      return el('div', { class: 'pub-block stack' },
        el('h3', { text: 'Facebook Reels' }),
        statusLine(fb),
        problemsList(fb && fb.problems),
        el('div', { class: 'fields' }, bindCheck(`${l}.facebook.enabled`, `Publish ${LNAME[l]} clips to this Page`, null, errs)),
        el('h3', { text: 'Instagram Reels' }),
        statusLine(ig),
        problemsList(ig && ig.problems),
        el('div', { class: 'fields' },
          bindCheck(`${l}.instagram.enabled`, `Publish ${LNAME[l]} clips to this Instagram account`, null, errs),
          bindCheck(`${l}.instagram.share_to_feed`, 'Also show the reel in the main feed', null, errs)),
        el('details', { class: 'pub-connect', open: !(fb && fb.connected) },
          el('summary', { text: fb && fb.connected ? 'Change Page / token' : 'Connect a Page and Instagram account' }),
          form),
        discBtns.length ? el('div', { class: 'row' }, discBtns) : null);
    }

    function advancedCard(errs) {
      return el('section', { class: 'card stack', 'aria-labelledby': 'pub-adv-h' },
        el('details', null,
          el('summary', { id: 'pub-adv-h', text: 'Advanced' }),
          el('div', { class: 'fields' },
            bindCheck('allow_same_account', 'Allow English and Hindi on the same account', 'Off: publishing is refused when both languages point at the same channel, Page or Instagram account.', errs),
            bindText('graph_api_version', 'Graph API version', 'Facebook/Instagram API version, e.g. v27.0.', errs, { maxlength: '6', class: 'mono' }),
            bindNumber('chunk_mb', 'YouTube upload chunk (MiB)', '1-256.', errs, { min: '1', max: '256', step: '1' }),
            bindNumber('max_retries', 'Retries on temporary errors', '0-10.', errs, { min: '0', max: '10', step: '1' }),
            bindNumber('poll_interval_seconds', 'Status poll interval (s)', '1-60.', errs, { min: '1', max: '60', step: '1' }),
            bindNumber('poll_timeout_seconds', 'Processing timeout (s)', '60-3600.', errs, { min: '60', max: '3600', step: '1' }))));
    }

    let stateLabel = null;
    function markDirty() { dirty = true; if (stateLabel) stateLabel.textContent = 'Unsaved changes'; }

    function saveBar(errs, summary) {
      const btn = el('button', { type: 'button', class: 'btn btn-primary', text: 'Save publishing settings' });
      const revert = el('button', { type: 'button', class: 'btn', text: 'Revert',
        on: { click: () => { if (!dirty || confirm('Discard your changes?')) { draft = deepClone(saved); dirty = false; render(); } } } });
      stateLabel = el('span', { class: 'small muted', 'aria-live': 'polite', text: dirty ? 'Unsaved changes' : '' });
      btn.addEventListener('click', async () => {
        errs.clear();
        const body = deepClone(draft);
        body.dry_run = saved.dry_run; // the switch above owns dry_run
        btn.disabled = true;
        stateLabel.textContent = 'Saving…';
        try {
          const r = await api('PUT', '/api/publish/settings', { body });
          if (!ctx.alive) return;
          saved = r.data;
          draft = deepClone(saved);
          dirty = false;
          toast('Publishing settings saved.', 'ok');
          await refreshAccounts();
          if (ctx.alive) render();
        } catch (e) {
          if (!ctx.alive) return;
          stateLabel.textContent = 'Not saved';
          errs.show(e);
        } finally {
          if (ctx.alive) btn.disabled = false;
        }
      });
      return el('div', { class: 'card stack pub-savebar' }, summary, el('div', { class: 'form-actions' }, btn, revert, stateLabel));
    }

    async function reloadAfterConnect() {
      const [a, s] = await Promise.all([apiGet('/api/publish/accounts'), apiGet('/api/publish/settings')]);
      if (!ctx.alive) return;
      accounts = a;
      // connect fills channel_id / page_id / ig_user_id: take those, keep the user's other unsaved edits
      for (const l of LANGS) {
        for (const [p, k] of [['youtube', 'channel_id'], ['facebook', 'page_id'], ['instagram', 'ig_user_id']]) {
          setPath(draft, `${l}.${p}.${k}`, getPath(s, `${l}.${p}.${k}`));
          setPath(saved, `${l}.${p}.${k}`, getPath(s, `${l}.${p}.${k}`));
        }
      }
      render();
    }

    async function connectYouTube(l, btn, waitLine) {
      btn.disabled = true;
      let r;
      const before = getPath(accounts, `langs.${l}.youtube.connected_at`) || null;
      // open the tab synchronously (popup blockers), then point it at Google's page
      const tab = window.open('', '_blank');
      try {
        r = await api('POST', `/api/publish/accounts/${l}/youtube/connect`);
      } catch (e) {
        if (tab) tab.close();
        btn.disabled = false;
        toast(e.message, 'error');
        return;
      }
      const url = r.data && r.data.auth_url;
      if (!url || !/^https:\/\/accounts\.google\.com\//.test(url)) {
        if (tab) tab.close();
        btn.disabled = false;
        toast('The server returned no Google sign-in address.', 'error');
        return;
      }
      if (tab) { try { tab.opener = null; } catch (e) { /* ignore */ } tab.location.href = url; }
      waitLine.textContent = `Waiting for Google sign-in (choose the ${LNAME[l]} channel)…`;
      if (!tab) append(waitLine, ' ', el('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: 'Open the Google sign-in page' }));
      const started = Date.now();
      if (connectPoll) clearTimeout(connectPoll.timer);
      connectPoll = { timer: null };
      const poll = async () => {
        if (!ctx.alive) return;
        let a = null;
        try { a = await refreshAccounts(); } catch (e) { a = null; }
        if (!ctx.alive) return;
        const now = a ? getPath(a, `langs.${l}.youtube.connected_at`) : null;
        if (a && now && now !== before) {
          toast(`YouTube connected for ${LNAME[l]} clips.`, 'ok');
          await reloadAfterConnect();
          return;
        }
        if (Date.now() - started > CONNECT_POLL_MAX_MS) {
          waitLine.textContent = 'Stopped waiting for Google sign-in. Press Connect to try again.';
          btn.disabled = false;
          return;
        }
        connectPoll.timer = setTimeout(poll, CONNECT_POLL_MS);
      };
      connectPoll.timer = setTimeout(poll, CONNECT_POLL_MS);
    }

    async function disconnect(l, platform) {
      const what = platform === 'meta' ? 'Facebook and Instagram' : PNAME[platform];
      if (!confirm(`Disconnect ${what} for ${LNAME[l]} clips? The stored token is deleted${platform === 'youtube' ? ' and revoked at Google' : ''}.`)) return;
      try {
        await api('DELETE', `/api/publish/accounts/${l}/${platform}?revoke=true`);
        if (!ctx.alive) return;
        toast(`${what} disconnected.`, 'ok');
        await refreshAccounts();
        if (ctx.alive) render();
      } catch (e) {
        toast(e.message, 'error');
      }
    }

    load();
  }

  // ================================================================== CLIP PUBLISH PANEL
  function panel(ctx, jobId, clip, clipLang, container) {
    const { el, append, swap, api, apiGet, toast, makePoller, fmtBytes, fmtSecs, fmtDate, chip, bar, uuid, Queue } = A();
    const base = `/api/jobs/${encodeURIComponent(jobId)}/clips/${clip}/${clipLang}/publish`;
    const body = el('div', { class: 'stack' });
    const card = el('section', { class: 'card stack pub-panel', 'aria-labelledby': 'pub-panel-h' },
      el('h2', { id: 'pub-panel-h', text: 'Publish' }), body);
    container.appendChild(card);
    body.appendChild(A().loading('Loading publishing status…'));

    let view = null;
    let plans = {};            // from the last dry-run response
    const checked = new Set();
    let busy = false;
    let lastTaskId = null;

    function isActive(v) {
      return !!(v && v.task && (v.task.state === 'queued' || v.task.state === 'running'));
    }

    function ready(p) {
      const x = view && view.platforms ? view.platforms[p] : null;
      return !!(x && x.enabled && x.connected && !(x.problems && x.problems.length));
    }
    function reason(p) {
      const x = view && view.platforms ? view.platforms[p] : null;
      if (!x) return 'not available';
      if (!x.enabled) return 'not enabled on the Accounts page';
      if (!x.connected) return 'not connected';
      if (x.problems && x.problems.length) return x.problems.join('; ');
      return '';
    }

    const poller = makePoller(ctx, 2000, async () => {
      const v = await apiGet(base);
      if (!ctx.alive) return false;
      const was = view && view.task ? view.task : null;
      view = v;
      if (was && (!v.task || v.task.id !== was.id) && lastTaskId === was.id) {
        lastTaskId = null;
        const st = PLATFORMS.map((p) => v.platforms[p] && v.platforms[p].status).filter(Boolean);
        if (st.some((s) => s.state === 'failed')) toast('Publishing finished with errors; see the Publish panel.', 'error');
        else toast('Publishing finished.', 'ok');
      }
      if (v.task) lastTaskId = v.task.id;
      render();
      return isActive(v);
    });

    function render() {
      if (!view) return;
      const dry = !!view.dry_run;
      const mode = dry
        ? el('p', { class: 'notice notice-ok', role: 'status' }, el('strong', { text: 'Dry run is ON - nothing is posted.' }),
          ' Preview builds the exact request plan only. ', el('a', { href: '#/accounts', text: 'Accounts & publishing' }))
        : el('p', { class: 'notice notice-error', role: 'status' }, el('strong', { text: 'Dry run is OFF - Publish posts for real.' }),
          ' ', el('a', { href: '#/accounts', text: 'Accounts & publishing' }));
      const finalNote = view.final ? null
        : el('p', { class: 'notice notice-warn', text: 'This clip has no final render yet. Render it before publishing.' });
      const taskLine = view.task
        ? el('div', { class: 'row small' }, chip(view.task.state, 'publish ' + A().taskLabel(view.task)),
          (view.task.state === 'queued' || view.task.state === 'running')
            ? el('button', { type: 'button', class: 'btn btn-small', text: 'Stop', on: { click: () => A().cancelTask(view.task, () => poller.kick()) } }) : null)
        : null;
      const rows = PLATFORMS.map((p) => platformRow(p, dry));
      const previewBtn = el('button', { type: 'button', class: 'btn', text: 'Preview (dry run)', disabled: busy || !view.final || !checked.size });
      previewBtn.addEventListener('click', () => preview());
      const publishBtn = dry ? null : el('button', { type: 'button', class: 'btn btn-danger', text: 'Publish…',
        disabled: busy || !view.final || !checked.size || isActive(view) || [...checked].some((p) => !ready(p)) });
      if (publishBtn) publishBtn.addEventListener('click', () => openConfirm());
      swap(body, mode, finalNote, taskLine,
        el('fieldset', { class: 'pub-choose' }, el('legend', { text: 'Platforms' }), rows),
        el('div', { class: 'row' }, previewBtn, publishBtn,
          checked.size ? null : el('span', { class: 'small muted', text: 'Choose at least one platform.' })));
    }

    function platformRow(p, dry) {
      const x = (view.platforms && view.platforms[p]) || {};
      const why = reason(p);
      const disabled = !dry && !ready(p);
      if (disabled) checked.delete(p);
      const id = `pub-cb-${p}`;
      const cb = el('input', { type: 'checkbox', id, checked: checked.has(p), disabled, 'data-fk': id });
      cb.addEventListener('change', () => { if (cb.checked) checked.add(p); else checked.delete(p); render(); });
      const acc = x.account ? (x.account.name || x.account.id) : null;
      const st = x.status;
      const plan = plans[p] || x.plan;
      return el('div', { class: 'pub-row' },
        el('div', { class: 'pub-row-head' },
          el('label', { class: 'check', for: id }, cb, ' ', el('strong', { text: PNAME[p] })),
          acc ? el('span', { class: 'pub-acc', text: '→ ' + acc }) : null,
          why ? el('span', { class: 'small muted', id: id + '-why', text: `(${why})` }) : null),
        st ? statusBlock(p, st, x) : null,
        plan ? planBlock(p, plan) : null);
    }

    function statusBlock(p, st, x) {
      const kids = [];
      const label = st.state + (st.message ? ' · ' + st.message : '');
      kids.push(el('div', { class: 'row small' }, chip(st.state === 'published' ? 'done' : (['uploading', 'processing', 'publishing'].includes(st.state) ? 'running' : st.state), st.state),
        st.message ? el('span', { class: 'muted', text: st.message }) : null,
        st.attempts > 1 ? el('span', { class: 'muted', text: `attempt ${st.attempts}` }) : null));
      if (ACTIVE_STATES.includes(st.state)) kids.push(bar(st.state === 'queued' ? 'queued' : 'running', st.progress || 0, `${PNAME[p]}: ${label}`));
      if (st.error) {
        kids.push(el('div', { class: 'notice notice-error', role: 'alert' },
          el('strong', { text: (ERROR_KIND[st.error.kind] || 'Error') + ': ' }), st.error.message || '',
          st.error.retry_after ? el('div', { class: 'small', text: `Try again in about ${fmtSecs(st.error.retry_after)}.` }) : null));
      }
      if (st.state === 'published' && st.url && /^https:\/\//.test(st.url)) {
        kids.push(el('div', { class: 'small' }, 'Published ', st.published_at ? fmtDate(st.published_at) + ' ' : '',
          el('a', { href: st.url, target: '_blank', rel: 'noopener noreferrer', text: st.url })));
      }
      if (x.published_render_outdated) {
        kids.push(el('p', { class: 'notice notice-warn small', text: 'The clip was re-rendered after it was published. Publish again with "publish again" ticked to post the new version.' }));
      }
      return el('div', { class: 'pub-status stack' }, kids);
    }

    function planBlock(p, plan) {
      const v = plan.video || {};
      const md = plan.metadata || {};
      const reqs = Array.isArray(plan.requests) ? plan.requests : [];
      return el('details', { class: 'pub-plan' },
        el('summary', null, `Request plan${plan.dry_run ? ' (dry run)' : ''}`,
          plan.created_at ? el('span', { class: 'small muted', text: ' · ' + fmtDate(plan.created_at) }) : null,
          plan.problems && plan.problems.length ? el('span', { class: 'chip st-fail', text: `${plan.problems.length} problem${plan.problems.length > 1 ? 's' : ''}` }) : null),
        problemsList(plan.problems),
        plan.warnings && plan.warnings.length ? el('ul', { class: 'pub-warnings' }, plan.warnings.map((w) => el('li', { text: w }))) : null,
        el('dl', { class: 'kv' },
          el('dt', { text: 'Account' }), el('dd', { text: plan.account ? `${plan.account.name || ''} (${plan.account.id})` : 'not connected' }),
          el('dt', { text: 'API' }), el('dd', { class: 'mono', text: String(plan.api_version || '') }),
          el('dt', { text: 'File' }), el('dd', { text: `${v.path || ''} · ${typeof v.size === 'number' ? fmtBytes(v.size) : '?'} · ${typeof v.duration === 'number' ? fmtSecs(v.duration) : '?'} · ${v.width || '?'}×${v.height || '?'}` })),
        el('h4', { text: 'Metadata sent' }),
        metadataView(md),
        el('h4', { text: `Requests (${reqs.length})` }),
        el('ol', { class: 'pub-reqs' }, reqs.map((r) => el('li', null,
          el('div', null, el('span', { class: 'mono pub-method', text: r.method || '' }), ' ',
            el('span', { class: 'mono pub-url', text: r.url || '' })),
          el('div', { class: 'small muted', text: [r.step, r.note].filter(Boolean).join(' - ') }),
          r.headers && Object.keys(r.headers).length ? el('pre', { class: 'pub-pre', text: Object.entries(r.headers).map(([k, val]) => `${k}: ${val}`).join('\n') }) : null,
          r.body ? el('pre', { class: 'pub-pre', lang: clipLang === 'hi' ? 'hi' : 'en', text: JSON.stringify(r.body, null, 2) }) : null))));
    }

    function metadataView(md) {
      const rows = [];
      const flat = (obj, prefix) => {
        for (const [k, v] of Object.entries(obj || {})) {
          const key = prefix ? `${prefix}.${k}` : k;
          if (v && typeof v === 'object' && !Array.isArray(v)) flat(v, key);
          else rows.push([key, Array.isArray(v) ? v.join(', ') : String(v)]);
        }
      };
      flat(md, '');
      return el('dl', { class: 'kv pub-md', lang: clipLang === 'hi' ? 'hi' : 'en' },
        rows.map(([k, v]) => [el('dt', { class: 'mono', text: k }), el('dd', { class: 'pub-md-val', text: v })]));
    }

    async function preview(silent) {
      busy = true;
      render();
      try {
        const r = await api('POST', base, { body: { platforms: PLATFORMS.filter((p) => checked.has(p)), dry_run: true } });
        if (!ctx.alive) return null;
        plans = Object.assign({}, plans, (r.data && r.data.plans) || {});
        if (!silent) toast('Dry-run plan saved. Nothing was posted.', 'ok');
        return r.data.plans || {};
      } catch (e) {
        if (ctx.alive) toast(e.message + errText(e), 'error');
        return null;
      } finally {
        busy = false;
        if (ctx.alive) { render(); poller.kick(); }
      }
    }

    function errText(e) {
      const parts = Object.entries(e.errors || {}).map(([k, v]) => `${PNAME[k] || k}: ${[].concat(v).join(' ')}`);
      return parts.length ? ' (' + parts.join('; ') + ')' : '';
    }

    async function openConfirm() {
      const chosen = PLATFORMS.filter((p) => checked.has(p));
      const fresh = await preview(true); // exact plans for the dialog (writes the plan files, no network)
      if (!fresh || !ctx.alive) return;
      const blocked = chosen.filter((p) => fresh[p] && fresh[p].problems && fresh[p].problems.length);
      if (blocked.length) {
        toast(`Cannot publish: ${blocked.map((p) => PNAME[p] + ': ' + fresh[p].problems.join('; ')).join(' | ')}`, 'error');
        return;
      }
      const key = uuid();
      const already = chosen.filter((p) => view.platforms[p] && view.platforms[p].status && view.platforms[p].status.state === 'published');
      const forceBox = el('input', { type: 'checkbox', id: 'pub-force' });
      const forceText = el('input', { type: 'text', id: 'pub-force-text', autocomplete: 'off', disabled: true, 'aria-describedby': 'pub-force-hint' });
      const forceWrap = el('div', { class: 'stack pub-force' },
        el('label', { class: 'check', for: 'pub-force' }, forceBox, ' ', 'Publish again even where it is already published (creates a second post)'),
        el('div', { class: 'field' }, el('label', { for: 'pub-force-text', text: `Type "${FORCE_WORD}" to confirm` }), forceText,
          el('div', { class: 'hint', id: 'pub-force-hint', text: 'Only needed for publishing again.' })));
      const ok = el('button', { type: 'submit', class: 'btn btn-danger', value: 'ok', text: 'Publish now' });
      const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
      const items = chosen.map((p) => {
        const pl = fresh[p] || {};
        const md = pl.metadata || {};
        const title = (md.snippet && md.snippet.title) || md.title || (md.caption ? String(md.caption).split('\n')[0] : '');
        const privacy = md.status && md.status.privacyStatus;
        const acc = pl.account ? (pl.account.name || pl.account.id) : '?';
        const isPublic = p !== 'youtube' || privacy === 'public';
        return el('li', null,
          el('strong', { text: PNAME[p] }), ' → ', el('span', { text: acc }),
          p === 'youtube' ? el('span', { class: 'muted', text: ` · ${privacy || 'private'}` }) : null,
          el('div', { class: 'pub-dialog-title', lang: clipLang === 'hi' ? 'hi' : 'en', text: title }),
          isPublic ? el('div', { class: 'pub-public', text: `This posts publicly to ${acc} on ${PNAME[p]}.` }) : null,
          already.includes(p) ? el('div', { class: 'small muted', text: 'Already published: skipped unless "publish again" is ticked.' }) : null);
      });
      const form = el('form', { method: 'dialog', class: 'stack' },
        el('h2', { id: 'pub-dlg-h', text: `Publish ${LNAME[clipLang]} clip ${A().pad2(clip)} for real?` }),
        el('p', { text: 'The video and the metadata below are uploaded now. This cannot be undone from Auto Clipper; delete the post on the platform if needed.' }),
        el('ul', { class: 'pub-dialog-list' }, items),
        forceWrap,
        el('div', { class: 'row' }, ok, cancel));
      const dlg = el('dialog', { class: 'pub-dialog', 'aria-labelledby': 'pub-dlg-h' }, form);
      document.body.appendChild(dlg);
      const close = () => { try { dlg.close(); } catch (e) { /* ignore */ } dlg.remove(); };
      ctx.onCleanup(close);
      forceBox.addEventListener('change', () => { forceText.disabled = !forceBox.checked; if (forceBox.checked) forceText.focus(); });
      cancel.addEventListener('click', close);
      dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const force = forceBox.checked;
        if (force && forceText.value.trim().toLowerCase() !== FORCE_WORD) {
          forceText.setAttribute('aria-invalid', 'true');
          forceText.focus();
          toast(`Type "${FORCE_WORD}" to publish again.`, 'error');
          return;
        }
        ok.disabled = true;
        try {
          const r = await api('POST', base, { body: { platforms: chosen, dry_run: false, confirm: true, force },
            headers: { 'Idempotency-Key': key } });
          if (!ctx.alive) return;
          close();
          const d = r.data || {};
          const skipped = Object.keys(d.already_published || {});
          if (d.task) { lastTaskId = d.task.id; toast('Publishing started.' + (skipped.length ? ` Already published: ${skipped.map((p) => PNAME[p]).join(', ')}.` : ''), 'ok'); } else toast('Already published; nothing was sent again.', 'ok');
          Queue.kick();
          poller.kick();
        } catch (err) {
          ok.disabled = false;
          if (ctx.alive) toast(err.message + errText(err), 'error');
        }
      });
      dlg.showModal();
      cancel.focus();
    }
  }

  window.ACPublish = { viewAccounts, panel };
})();
