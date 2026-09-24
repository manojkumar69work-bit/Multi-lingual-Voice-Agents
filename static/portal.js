/* Shared helpers for the portals (admin, client, dashboard, test caller).
   Plain script, no build step — each page includes it before its own code. */

const P = (() => {
  // Brand glyph: a tiny turn strip — caller, agent, caller — on ink.
  const GLYPH = `<svg viewBox="0 0 20 20" aria-hidden="true">
    <rect x="2" y="8" width="5" height="4" rx="1" fill="#2fa88b"/>
    <rect x="8" y="8" width="7" height="4" rx="1" fill="#e0912f"/>
    <rect x="16" y="8" width="2" height="4" rx="1" fill="#2fa88b"/></svg>`;

  const ICON = {
    home:   '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    calls:  '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
    leads:  '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/>',
    send:   '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>',
    users:  '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    live:   '<circle cx="12" cy="12" r="2"/><path d="M16.2 7.8a6 6 0 0 1 0 8.5M7.8 16.2a6 6 0 0 1 0-8.5M19.1 4.9a10 10 0 0 1 0 14.2M4.9 19.1a10 10 0 0 1 0-14.2"/>',
    mic:    '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    menu:   '<path d="M3 6h18M3 12h18M3 18h18"/>',
    sun:    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon:   '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    out:    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
    down:   '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
    refresh:'<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.8-3.4L23 10M1 14l4.7 4.4A9 9 0 0 0 20.5 15"/>',
    plus:   '<path d="M12 5v14M5 12h14"/>',
    x:      '<path d="M18 6L6 18M6 6l12 12"/>',
  };
  const icon = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[n] || ''}</svg>`;

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));

  async function api(url, opts = {}) {
    const init = { ...opts, headers: { ...(opts.body ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) } };
    if (opts.body && typeof opts.body !== 'string') init.body = JSON.stringify(opts.body);
    const r = await fetch(url, init);
    if (r.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }

  async function guard(role) {
    const r = await fetch('/api/auth/me');
    if (!r.ok) { location.href = '/login'; return null; }
    const u = await r.json();
    if (role && u.role !== role) { location.href = u.role === 'admin' ? '/admin' : '/client'; return null; }
    return u;
  }
  async function logout() { await fetch('/api/auth/logout', { method: 'POST' }); location.href = '/login'; }

  // ── Formatting ──
  const dur = (s) => { s = Math.round(s || 0); const m = Math.floor(s / 60); return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`; };
  const clock = (s) => { s = Math.round(s || 0); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
  const mins = (m) => (m || 0).toLocaleString('en-IN', { maximumFractionDigits: 1 });
  const num = (n) => (n || 0).toLocaleString('en-IN');
  function ago(iso) {
    if (!iso) return '—';
    const d = new Date(iso), s = (Date.now() - d) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 86400 * 6) return d.toLocaleDateString('en-IN', { weekday: 'short' }) + ' ' + d.toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit' });
    return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  }
  const when = (iso) => iso ? new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }) : '—';
  const title = (k) => String(k).replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase());

  // ── Lead helpers ──
  const leadOf = (c) => (c && c.lead_data) || {};
  const hasLead = (c) => Object.entries(leadOf(c)).some(([k, v]) => v && k !== 'notes');
  function leadName(c) {
    const ld = leadOf(c);
    return ld.name || ld.full_name || ld.caller_name || c.caller_phone || '';
  }
  function callTitle(c) {
    const n = leadName(c);
    if (n) return n;
    return c.status === 'active' ? 'Caller on the line' : 'Unnamed caller';
  }
  function callLine(c) {
    const ld = leadOf(c);
    const bits = ['property_type', 'budget', 'location', 'timeline']
      .map(k => ld[k]).filter(Boolean);
    if (bits.length) return bits.join(' · ');
    if (c.summary) return c.summary;
    return `${(c.transcript || []).length} turns`;
  }
  // Very rough, transparent scoring — shown as “how complete is this lead”, not a prediction.
  function completeness(c, fields) {
    const ld = leadOf(c);
    const keys = (fields && fields.length ? fields : ['name', 'phone', 'property_type', 'budget', 'location', 'timeline']).filter(k => k !== 'notes');
    const got = keys.filter(k => ld[k]).length;
    return { got, of: keys.length };
  }

  // ── Signature: turn strip ──
  const words = (t) => Math.max(1, String(t || '').trim().split(/\s+/).length);
  function strip(transcript, { tall = false, label = true } = {}) {
    const t = transcript || [];
    if (!t.length) return `<div class="strip none${tall ? ' tall' : ''}" ${label ? 'role="img" aria-label="No conversation recorded"' : ''}></div>`;
    const u = t.filter(m => m.role === 'user').reduce((a, m) => a + words(m.content), 0);
    const a = t.filter(m => m.role !== 'user').reduce((s, m) => s + words(m.content), 0);
    const share = Math.round((u / (u + a)) * 100);
    const segs = t.map((m, i) => `<i class="${m.role === 'user' ? 'u' : 'a'}" style="flex:${words(m.content)}" data-i="${i}" title="${m.role === 'user' ? 'Caller' : 'Agent'} · ${words(m.content)} words"></i>`).join('');
    return `<div class="strip${tall ? ' tall' : ''}" ${label ? `role="img" aria-label="${t.length} turns; caller spoke ${share}% of the words"` : ''}>${segs}</div>`;
  }
  function talkShare(transcript) {
    const t = transcript || [];
    const u = t.filter(m => m.role === 'user').reduce((a, m) => a + words(m.content), 0);
    const a = t.filter(m => m.role !== 'user').reduce((s, m) => s + words(m.content), 0);
    return u + a ? Math.round((u / (u + a)) * 100) : 0;
  }

  function transcriptHTML(transcript, agentName = 'Agent', q = '') {
    const t = transcript || [];
    if (!t.length) return '<div class="empty">No transcript was saved for this call.</div>';
    const hi = (s) => {
      const e = esc(s);
      if (!q) return e;
      const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      return e.replace(re, m => `<mark>${m}</mark>`);
    };
    return `<div class="transcript">${t.map((m, i) => {
      const u = m.role === 'user';
      return `<div class="turn ${u ? 'u' : 'a'}" id="turn-${i}"><div class="who">${u ? 'Caller' : esc(agentName)}</div><div class="say">${hi(m.content)}</div></div>`;
    }).join('')}</div>`;
  }
  // Clicking a strip segment scrolls to that turn.
  function wireStrip(root) {
    root.querySelectorAll('.strip.tall i').forEach(seg => seg.addEventListener('click', () => {
      const el = root.querySelector('#turn-' + seg.dataset.i);
      if (!el) return;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1200);
    }));
  }

  function factsHTML(c, fields) {
    const ld = leadOf(c);
    const keys = fields && fields.length
      ? fields.filter(k => k !== 'notes')
      : Object.keys(ld).filter(k => k !== 'notes');
    if (!keys.length) return '';
    const cells = keys.map(k => {
      const v = ld[k];
      return `<div class="${v ? '' : 'missing'}"><dt>${esc(title(k))}</dt><dd>${v ? esc(v) : 'not captured'}</dd></div>`;
    }).join('');
    return `<dl class="facts">${cells}</dl>${ld.notes ? `<div class="notes">${esc(ld.notes)}</div>` : ''}`;
  }

  function deliveryBadge(c, deliveries) {
    if (c.status === 'active') return '<span class="badge live">On the line</span>';
    const ds = (deliveries || []).filter(d => d.session_id === c.session_id);
    if (ds.some(d => d.status === 'dead')) return '<span class="badge bad">Delivery failed</span>';
    if (ds.some(d => d.status === 'pending')) return '<span class="badge warn">Sending lead</span>';
    if (c.lead_submitted || ds.some(d => d.status === 'delivered')) return '<span class="badge ok">Lead sent</span>';
    if (hasLead(c)) return '<span class="badge">Partial lead</span>';
    return '<span class="badge">No lead</span>';
  }

  // ── Call detail pane (shared by client + admin) ──
  function renderDetail(el, c, { deliveries = [], soft = false, agentName = 'Agent', extraMeta = '' } = {}) {
    if (!c) { el.innerHTML = '<div class="empty"><b>Call not found</b>It may have been removed.</div>'; el.dataset.id = ''; return; }
    // Don't redraw an unchanged call on every poll — it would reset the audio player.
    if (soft && el.dataset.id === c.session_id && el.dataset.v === String(c.updated_at)) return;
    const tq = (el.dataset.id === c.session_id && el.querySelector('#tq')?.value) || '';
    el.dataset.id = c.session_id; el.dataset.v = String(c.updated_at);

    const ld = leadOf(c);
    const phone = ld.phone || ld.phone_number || ld.contact || c.caller_phone || '';
    const digits = String(phone).replace(/[^\d+]/g, '');
    const wa = digits.replace(/^\+/, '').replace(/^(?=[6-9]\d{9}$)/, '91');
    const rec = '/api/recordings/' + encodeURIComponent(c.session_id);
    el.innerHTML = `
      <div class="detail-head">
        <div><h3>${esc(callTitle(c))}</h3>
          <div class="meta">${extraMeta}${when(c.created_at)} · ${dur(c.duration_seconds)}${c.caller_phone ? ' · <span class="mono">' + esc(c.caller_phone) + '</span>' : ''}</div></div>
        <div class="actions">${deliveryBadge(c, deliveries)}</div>
      </div>
      ${c.summary ? `<p class="summary">${esc(c.summary)}</p>` : c.status === 'active' ? '<p class="summary muted">The summary is written when the call ends.</p>' : ''}
      ${c.error ? `<div class="notes" style="border-color:var(--bad)">Call error: ${esc(c.error)}</div>` : ''}
      ${hasLead(c) ? `<div class="block-label">What the agent captured</div>${factsHTML(c)}` : ''}
      <div class="actions" style="margin-top:14px">
        ${digits.length >= 10 ? `<a class="btn sm" href="tel:${esc(digits)}">${icon('calls')}Call back</a>
          <a class="btn ghost sm" target="_blank" rel="noopener" href="https://wa.me/${esc(wa)}">WhatsApp</a>` : ''}
        ${hasLead(c) ? '<button class="btn ghost sm" data-copy>Copy lead</button>' : ''}
      </div>
      <div class="block-label"><span>Conversation · ${(c.transcript || []).length} turns · caller spoke ${talkShare(c.transcript)}%</span>
        <span class="key" style="text-transform:none;letter-spacing:0;font-weight:400"><span style="--c:var(--caller)">Caller</span><span style="--c:var(--agent)">Agent</span></span></div>
      ${strip(c.transcript, { tall: true })}
      ${c.recording_path && c.status !== 'active' ? `<div class="block-label"><span>Recording</span><a class="muted" style="text-transform:none;letter-spacing:0;font-weight:400" href="${rec}" download>Download</a></div>
        <audio controls preload="none" src="${rec}"></audio>` : ''}
      <div class="block-label"><span>Transcript</span></div>
      <label class="search" style="margin:0 0 12px"><span class="sr-only">Find in transcript</span>${icon('search')}<input id="tq" type="search" placeholder="Find in this call" value="${esc(tq)}"/></label>
      <div data-tbody>${transcriptHTML(c.transcript, agentName, tq)}</div>`;
    wireStrip(el);
    if (!soft && innerWidth <= 1080) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.querySelector('#tq').addEventListener('input', (e) => { el.querySelector('[data-tbody]').innerHTML = transcriptHTML(c.transcript, agentName, e.target.value.trim()); });
    el.querySelector('[data-copy]')?.addEventListener('click', async () => {
      const text = Object.entries(ld).filter(([, v]) => v).map(([k, v]) => `${title(k)}: ${v}`).join('\n');
      try { await navigator.clipboard.writeText(text); toast('Lead copied'); } catch (_) { toast('Copy failed — select the text instead', true); }
    });
  }

  function callItem(c, selectedId, extra = '') {
    const live = c.status === 'active';
    return `<button class="call-item" role="option" data-id="${esc(c.session_id)}" aria-selected="${selectedId === c.session_id}">
      <span class="t1">${esc(callTitle(c))}</span>
      <span class="when">${live ? '<span class="dot-live">Live</span>' : ago(c.created_at)}</span>
      <span class="t2">${extra}${esc(callLine(c))} · ${dur(c.duration_seconds)}</span>
      ${strip(c.transcript, { label: false })}
    </button>`;
  }

  // ── Bar chart: calls per day (single series, hover tooltip, table fallback) ──
  // Days ending today; if nothing happened in that window, end at the latest call
  // instead so the chart shows the most recent activity rather than a flat line.
  function perDay(calls, days = 14) {
    const out = [];
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const latest = calls.reduce((m, c) => c.created_at ? Math.max(m, +new Date(c.created_at)) : m, 0);
    if (latest && latest < today - (days - 1) * 86400000) { today.setTime(latest); today.setHours(0, 0, 0, 0); out.shifted = true; }
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today); d.setDate(d.getDate() - i);
      out.push({ d, key: d.toDateString(), calls: 0, leads: 0, secs: 0 });
    }
    const idx = Object.fromEntries(out.map((o, i) => [o.key, i]));
    for (const c of calls) {
      if (!c.created_at) continue;
      const k = new Date(c.created_at); k.setHours(0, 0, 0, 0);
      const i = idx[k.toDateString()];
      if (i === undefined) continue;
      out[i].calls++; out[i].secs += c.duration_seconds || 0;
      if (hasLead(c)) out[i].leads++;
    }
    return out;
  }
  function barChart(el, series) {
    const W = Math.max(280, el.clientWidth || 640), H = 180, padL = 26, padB = 22, padT = 14;
    const max = Math.max(1, ...series.map(s => s.calls));
    const step = max <= 4 ? 1 : Math.ceil(max / 4);
    const top = Math.ceil(max / step) * step;
    const band = (W - padL) / series.length;
    const bw = Math.min(24, band * 0.62);
    const y = (v) => padT + (H - padT - padB) * (1 - v / top);
    let grid = '', ax = '';
    for (let v = 0; v <= top; v += step) {
      grid += `<line x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`;
      ax += `<text x="${padL - 6}" y="${y(v) + 3}" text-anchor="end">${v}</text>`;
    }
    const peak = series.reduce((m, s, i) => s.calls > series[m].calls ? i : m, 0);
    const bars = series.map((s, i) => {
      const x = padL + band * i + (band - bw) / 2;
      const h = Math.max(0, y(0) - y(s.calls));
      const r = Math.min(4, h);
      const path = h ? `M${x},${y(0)} v${-(h - r)} q0,${-r} ${r},${-r} h${bw - 2 * r} q${r},0 ${r},${r} v${h - r} z` : '';
      const every = W < 480 ? 3 : 2;
      const lbl = ((series.length - 1 - i) % every === 0) ? `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${s.d.getDate()}</text>` : '';
      const val = (i === peak && s.calls) || (i === series.length - 1 && s.calls) ? `<text class="vlabel" x="${x + bw / 2}" y="${y(s.calls) - 5}" text-anchor="middle">${s.calls}</text>` : '';
      return `<g data-i="${i}"><rect class="hit" x="${padL + band * i}" y="${padT}" width="${band}" height="${H - padT - padB}"/>${path ? `<path class="bar" d="${path}"/>` : ''}${val}<g class="axis">${lbl}</g></g>`;
    }).join('');
    const last = series[series.length - 1].d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
    const total = series.reduce((a, s) => a + s.calls, 0);
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Calls per day for the ${series.length} days to ${last}: ${total} calls">
      <g class="grid">${grid}</g><g class="axis">${ax}</g>${bars}</svg><div class="tip" hidden></div>
      ${series.shifted ? `<div class="chart-note">No calls in the last ${series.length} days — showing the ${series.length} days to ${last}, the latest activity.</div>` : ''}`;
    if (!el._ro) { el._ro = true; let t; window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => barChart(el, series), 150); }); }
    el._series = series;
    const tip = el.querySelector('.tip');
    el.querySelectorAll('svg > g[data-i]').forEach(g => {
      g.addEventListener('mouseenter', () => {
        const s = series[+g.dataset.i];
        g.classList.add('on');
        tip.innerHTML = `<b>${s.d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' })}</b>${s.calls} call${s.calls === 1 ? '' : 's'} · ${s.leads} lead${s.leads === 1 ? '' : 's'} · ${mins(s.secs / 60)} min`;
        const box = g.querySelector('.hit').getBoundingClientRect(), host = el.getBoundingClientRect();
        tip.style.left = (box.left - host.left + box.width / 2) + 'px';
        tip.style.top = (el.querySelector('svg').getBoundingClientRect().top - host.top + 16) + 'px';
        tip.hidden = false;
      });
      g.addEventListener('mouseleave', () => { g.classList.remove('on'); tip.hidden = true; });
    });
  }

  // ── Views (hash routing), theme, toast, mobile rail ──
  function router(onChange) {
    const go = () => {
      const want = (location.hash || '').slice(1).split('/')[0];
      const views = [...document.querySelectorAll('.view')];
      const v = views.find(x => x.id === 'v-' + want) || views[0];
      views.forEach(x => x.classList.toggle('on', x === v));
      document.querySelectorAll('.nav [data-view]').forEach(b => b.setAttribute('aria-current', b.dataset.view === v.id.slice(2) ? 'page' : 'false'));
      document.querySelector('.rail')?.classList.remove('open');
      onChange && onChange(v.id.slice(2), (location.hash || '').slice(1).split('/').slice(1).join('/'));
    };
    document.querySelectorAll('.nav [data-view]').forEach(b => b.addEventListener('click', () => { location.hash = b.dataset.view; }));
    window.addEventListener('hashchange', go);
    go();
  }
  function theme() {
    const root = document.documentElement;
    let saved = null;
    try { saved = localStorage.getItem('mea-theme'); } catch (_) {}
    if (saved) root.dataset.theme = saved;
    const btn = document.getElementById('themeBtn');
    if (!btn) return;
    const isDark = () => root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    const paint = () => { btn.innerHTML = icon(isDark() ? 'sun' : 'moon'); btn.setAttribute('aria-label', isDark() ? 'Use light theme' : 'Use dark theme'); };
    btn.addEventListener('click', () => {
      root.dataset.theme = isDark() ? 'light' : 'dark';
      try { localStorage.setItem('mea-theme', root.dataset.theme); } catch (_) {}
      paint();
    });
    paint();
  }
  let toastT;
  function toast(msg, bad = false) {
    let t = document.querySelector('.toast');
    if (!t) { t = document.createElement('div'); t.className = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = msg; t.classList.toggle('bad', bad); t.classList.add('show');
    clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 3200);
  }
  function mobileRail() {
    const rail = document.querySelector('.rail');
    document.getElementById('railBtn')?.addEventListener('click', () => rail.classList.toggle('open'));
  }
  function mountChrome() {
    document.querySelectorAll('[data-glyph]').forEach(e => e.innerHTML = GLYPH);
    document.querySelectorAll('[data-icon]').forEach(e => e.insertAdjacentHTML('afterbegin', icon(e.dataset.icon)));
    theme(); mobileRail();
  }

  function downloadCSV(name, header, rows) {
    const q = (v) => `"${String(v ?? '').replace(/"/g, '""').replace(/\n/g, ' ')}"`;
    const csv = [header.map(q).join(','), ...rows.map(r => r.map(q).join(','))].join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }));
    a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function leadsCSV(calls, name = 'leads.csv') {
    const leads = calls.filter(hasLead);
    if (!leads.length) { toast('No leads to export yet'); return; }
    const keys = [];
    for (const c of leads) for (const k of Object.keys(leadOf(c))) if (!keys.includes(k)) keys.push(k);
    downloadCSV(name, ['date', ...keys, 'duration_s', 'summary', 'session_id'],
      leads.map(c => [c.created_at, ...keys.map(k => leadOf(c)[k] || ''), Math.round(c.duration_seconds || 0), c.summary || '', c.session_id]));
    toast(`Exported ${leads.length} lead${leads.length === 1 ? '' : 's'}`);
  }

  // Poll only while the tab is visible.
  function poll(fn, ms) {
    let t = setInterval(() => { if (!document.hidden) fn(); }, ms);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) fn(); });
    return () => clearInterval(t);
  }

  return { GLYPH, icon, esc, api, guard, logout, dur, clock, mins, num, ago, when, title,
    leadOf, hasLead, leadName, callTitle, callLine, completeness, strip, talkShare,
    transcriptHTML, wireStrip, factsHTML, deliveryBadge, renderDetail, callItem, perDay, barChart,
    router, toast, mountChrome, downloadCSV, leadsCSV, poll };
})();
