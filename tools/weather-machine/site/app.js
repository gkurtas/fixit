/* app.js — Philadelphia severe weather feed.
   Fetches NWS alerts + IEM storm reports, filters to the Mount Holly (PHI)
   office, renders a ranked feed and a Leaflet map, and polls on a loop.

   Demo mode: append ?demo=1 to the URL to load bundled sample fixtures from
   the relative samples/ folder instead of the live APIs. Demo data is national, so the office
   filter is bypassed in that mode. This lets the map/card code paths be
   exercised on a calm day with no live severe weather. */

(function () {
  'use strict';

  const DEMO = new URLSearchParams(location.search).get('demo') === '1';

  // Track which item IDs we've already seen, so re-fetches can flag new ones
  // without re-animating the whole list. Seeded false on first paint so the
  // initial load doesn't flash everything as "NEW".
  const seenAlerts = new Set();
  const seenLsr = new Set();
  let firstAlertPaint = true;
  let firstLsrPaint = true;

  // Last successfully fetched + scope-filtered features. We re-apply the
  // time filters to these on a timer, so display stays correct between polls
  // (and through failed polls) without re-fetching.
  let lastAlerts = [];
  let lastLsr = [];
  // Neighboring offices' polygon warnings — drawn on the map only, never in the cards.
  let lastMapAlerts = [];
  // Region-wide watch polygons (zone-resolved) — subtle layer beneath warnings.
  let lastWatchAlerts = [];

  // Signature of what's currently painted, so we only touch the DOM when the
  // visible set actually changes. Prevents the prune/poll timers from
  // collapsing an alert detail the reader has expanded. NWS issues updates as
  // new alert IDs, so an ID-list signature also catches content changes.
  let lastAlertKey = null;
  let lastLsrKey = null;

  // Leaflet layers we clear/redraw each cycle.
  let map;
  let alertLayer;
  let lsrLayer;
  let watchLayer;

  // ---- Utilities --------------------------------------------------------

  // Pick black or white badge text by contrast, so a light severity color
  // (e.g. the amber Moderate) stays legible. Falls back to white.
  function badgeTextColor(hex) {
    if (typeof hex !== 'string' || hex[0] !== '#' || hex.length < 7) return '#fff';
    const ch = (i) => parseInt(hex.slice(i, i + 2), 16) / 255;
    const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
    const L = 0.2126 * lin(ch(1)) + 0.7152 * lin(ch(3)) + 0.0722 * lin(ch(5));
    // contrast against white = 1.05 / (L + 0.05); use dark ink when that is < 4.5:1
    return (1.05 / (L + 0.05)) >= 4.5 ? '#fff' : '#15202c';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Format an ISO timestamp in the viewer's local time zone.
  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return escapeHtml(iso);
    return d.toLocaleString([], {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
  }

  function fmtClock(d) {
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  // The moment an alert is no longer in effect: `ends` if the product gives it,
  // otherwise `expires`. Returns null when neither is present (rare — keep it).
  function alertEndMs(p) {
    const t = p.ends || p.expires;
    if (!t) return null;
    const ms = new Date(t).getTime();
    return isNaN(ms) ? null : ms;
  }

  // Drop alerts whose end time has passed, regardless of what the API still
  // returns. This is what makes expired alerts disappear on schedule.
  function isAlertLive(f, now) {
    const end = alertEndMs(f.properties);
    return end === null || end > now;
  }

  function alertId(f) { return f.id || (f.properties && f.properties.id); }
  function lsrId(f) {
    const p = f.properties || {};
    return f.id != null ? f.id : (p.product_id || '') + (p.valid || '') + (p.lat || '');
  }

  // Keep only reports inside the configured rolling window.
  function isLsrRecent(f, now, windowMs) {
    const v = f.properties && f.properties.valid;
    if (!v) return true;
    const t = new Date(v).getTime();
    return isNaN(t) || t >= now - windowMs;
  }

  async function fetchJSON(url) {
    const res = await fetch(url, { headers: { Accept: 'application/geo+json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
    return res.json();
  }

  function setStatus(id, text, state) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'status-item' + (state ? ' ' + state : '');
  }

  // ---- Alerts -----------------------------------------------------------

  function rankAlert(f) {
    return CONFIG.severityRank[f.properties.severity] || 0;
  }

  // Keep only alerts from the Mount Holly office (our geographic scope).
  function filterAlerts(features) {
    if (DEMO) return features; // sample data is national; show it all
    return features.filter(
      (f) => f.properties && f.properties.senderName === CONFIG.alertSenderName
    );
  }

  function sortAlerts(features) {
    return features.slice().sort((a, b) => {
      const r = rankAlert(b) - rankAlert(a);
      if (r !== 0) return r;
      // Newer first within a severity tier.
      const ta = new Date(a.properties.onset || a.properties.effective || 0);
      const tb = new Date(b.properties.onset || b.properties.effective || 0);
      return tb - ta;
    });
  }

  function alertCard(f) {
    const p = f.properties;
    const sev = p.severity || 'Unknown';
    const color = CONFIG.severityColor[sev] || CONFIG.severityColor.Unknown;
    const isNew = !firstAlertPaint && !seenAlerts.has(alertId(f));

    // Plain-language rewrite added by the pipeline. Only trust it when the
    // fidelity guardrail passed; otherwise show the official wording.
    // Alerts always keep the official NWS event name as the headline — the AI
    // rewrite only simplifies the summary text below it.
    const plain = p.plain && p.plain.verified ? p.plain : null;
    const title = p.event || 'Alert';

    const official = (p.description || '') +
      (p.instruction ? '\n\nWHAT TO DO:\n' + p.instruction : '');
    const officialBlock = `<strong>${escapeHtml(p.event || 'Alert')}</strong>` +
      (official ? `\n\n${escapeHtml(official)}` : '');

    return `
      <article class="card${isNew ? ' is-new' : ''}" style="border-left-color:${color}">
        <div class="card-top">
          <span class="badge" style="background:${color};color:${badgeTextColor(color)}">${escapeHtml(sev)}</span>
          ${isNew ? '<span class="new-tag">NEW</span>' : ''}
          <h3>${escapeHtml(title)}</h3>
        </div>
        <p class="area">${escapeHtml(p.areaDesc || '')}</p>
        <p class="times">
          ${fmtTime(p.onset || p.effective)}
          <span class="sep">→</span>
          ${fmtTime(p.expires || p.ends) || 'until further notice'}
        </p>
        ${plain ? `<p class="plain-summary"><span class="ai-tag" title="Plain-language summary generated by AI from the official alert. Open “Show official NWS text” for the exact wording.">AI-simplified</span> ${escapeHtml(plain.summary)}</p>` : ''}
        ${plain
          ? `<details><summary>Show official NWS text</summary><p>${officialBlock}</p></details>`
          : (official ? `<details><summary>Details</summary><p>${escapeHtml(official)}</p></details>` : '')}
        <p class="office">Issued by ${escapeHtml(p.senderName || 'NWS')}</p>
      </article>`;
  }

  // Drive the threat-level meter and the whole-site tonal shift from the most
  // severe alert currently active in the feed (Mount Holly area).
  function updateSeverity(features) {
    let maxRank = 0;
    let name = null;
    features.forEach((f) => {
      const s = (f.properties && f.properties.severity) || 'Unknown';
      const r = CONFIG.severityRank[s] || 0;
      if (r > maxRank) { maxRank = r; name = s; }
    });

    // data-sev drives the CSS tone (masthead rule, page wash, meter band).
    document.body.dataset.sev = maxRank === 0 ? 'none' : (name || 'Unknown').toLowerCase();

    const lvl = document.getElementById('sev-level');
    if (lvl) {
      lvl.textContent = maxRank === 0 ? 'No active alerts'
        : (maxRank === 1 ? 'Active alert' : name);
    }
    document.querySelectorAll('#sev-gauge .seg').forEach((seg) => {
      const r = CONFIG.severityRank[seg.dataset.lvl] || 0;
      seg.classList.toggle('on', maxRank >= 2 && r <= maxRank);
    });
  }

  function renderAlerts(features) {
    const list = document.getElementById('alerts-list');
    document.getElementById('alerts-count').textContent = features.length;
    if (!features.length) {
      list.innerHTML = '<p class="empty">No active alerts for the Philadelphia region. 🌤️</p>';
    } else {
      list.innerHTML = features.map(alertCard).join('');
    }
    features.forEach((f) => seenAlerts.add(alertId(f)));
    firstAlertPaint = false;
  }

  // ---- Storm reports (LSR) ---------------------------------------------

  function lsrCategory(typetext) {
    const t = (typetext || '').toUpperCase();
    for (const cat of CONFIG.lsrCategories) {
      if (cat.match.some((m) => t.includes(m))) return cat;
    }
    return CONFIG.lsrDefault;
  }

  function filterLsr(features) {
    if (DEMO) return features;
    return features.filter((f) => f.properties && f.properties.wfo === CONFIG.lsrWfo);
  }

  function sortLsr(features) {
    return features.slice().sort(
      (a, b) => new Date(b.properties.valid) - new Date(a.properties.valid)
    );
  }

  function lsrCard(f) {
    const p = f.properties;
    const cat = lsrCategory(p.typetext);
    const isNew = !firstLsrPaint && !seenLsr.has(lsrId(f));
    const mag = p.magnitude && String(p.magnitude).trim() ? ` — ${escapeHtml(p.magnitude)}` : '';
    const where = [p.city, p.county ? p.county + ' Co.' : '', p.st || p.state]
      .filter(Boolean).join(', ');

    // Storm reports may have both the headline and summary rewritten. Use the
    // plain headline as the title line when present; keep the magnitude (a
    // structured number) visible; show the original report on tap.
    const plain = p.plain && p.plain.verified ? p.plain : null;
    const typeLabel = plain ? plain.headline : (p.typetext || '');
    const originalReport = [
      p.typetext,
      p.magnitude && String(p.magnitude).trim() ? p.magnitude : '',
      p.remark,
    ].filter(Boolean).join(' — ');

    return `
      <article class="card lsr-card${isNew ? ' is-new' : ''}" style="border-left-color:${cat.color}">
        <div class="lsr-head">
          <span class="badge" style="background:${cat.color};color:${badgeTextColor(cat.color)}">${escapeHtml(cat.label)}</span>
          ${isNew ? '<span class="new-tag">NEW</span>' : ''}
          ${plain ? '<span class="ai-tag" title="Headline and summary generated by AI from the original report. Open “Show original report” for the exact wording.">AI-simplified</span>' : ''}
          <span class="lsr-type">${escapeHtml(typeLabel)}</span>
          <span class="lsr-mag">${mag}</span>
        </div>
        <p class="lsr-where">${escapeHtml(where)}</p>
        ${plain
          ? `<p class="plain-summary">${escapeHtml(plain.summary)}</p>`
          : (p.remark ? `<p class="lsr-remark">${escapeHtml(p.remark)}</p>` : '')}
        ${plain && originalReport
          ? `<details><summary>Show original report</summary><p class="lsr-remark">${escapeHtml(originalReport)}</p></details>`
          : ''}
        <p class="lsr-meta">${fmtTime(p.valid)} · source: ${escapeHtml(p.source || 'unknown')}</p>
      </article>`;
  }

  function renderLsr(features) {
    const list = document.getElementById('lsr-list');
    document.getElementById('lsr-count').textContent = features.length;
    if (!features.length) {
      list.innerHTML = '<p class="empty">No storm reports in the last 24 hours.</p>';
    } else {
      list.innerHTML = features.map(lsrCard).join('');
    }
    features.forEach((f) => seenLsr.add(lsrId(f)));
    firstLsrPaint = false;
  }

  // ---- Map --------------------------------------------------------------

  function initMap() {
    map = L.map('map', { scrollWheelZoom: false }).setView(CONFIG.mapCenter, CONFIG.mapZoom);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map);

    // Dedicated panes so alert polygons ALWAYS sit beneath storm-report dots,
    // regardless of which layer repaints last. The two feeds poll on separate
    // timers, so relying on DOM insertion order in a shared pane is unsafe — a
    // late alert repaint could otherwise cover the reports. Leaflet (>=1.7)
    // auto-creates an SVG renderer per named pane, so paths assigned to these
    // panes stack by pane z-index. (overlayPane=400, markerPane=600.)
    // Stacking, bottom to top: base tiles (200) < radar (300) < watch areas
    // (350) < warning polygons (410) < storm-report dots (420). Watches sit
    // above radar but beneath warnings, so warnings always stand out.
    map.createPane('radarPane').style.zIndex = 300;
    map.createPane('watchesPane').style.zIndex = 350;
    map.createPane('alertsPane').style.zIndex = 410;
    map.createPane('lsrPane').style.zIndex = 420;

    watchLayer = L.layerGroup().addTo(map);
    alertLayer = L.layerGroup().addTo(map);
    lsrLayer = L.layerGroup().addTo(map);
    map.on('click', () => map.scrollWheelZoom.enable()); // enable zoom after intent
    renderLegend();
  }

  function renderMapAlerts(features) {
    if (!map) return;
    alertLayer.clearLayers();
    const withGeom = features.filter((f) => f.geometry &&
      (f.geometry.type === 'Polygon' || f.geometry.type === 'MultiPolygon'));
    withGeom.forEach((f) => {
      const sev = f.properties.severity || 'Unknown';
      const color = CONFIG.severityColor[sev] || CONFIG.severityColor.Unknown;
      L.geoJSON(f, {
        pane: 'alertsPane', // forces these below the storm-report dots
        // Translucent fill + solid colored border reads as an "area," so it
        // stays distinct from a same-colored report dot sitting on top of it.
        style: { color, weight: 2, fillColor: color, fillOpacity: 0.2 },
      }).bindPopup(
        `<strong>${escapeHtml(f.properties.event)}</strong><br>` +
        `${escapeHtml(f.properties.areaDesc || '')}<br>` +
        `<span class="popup-office">${escapeHtml(f.properties.senderName || '')}</span>`
      ).addTo(alertLayer);
    });
  }

  // Watch areas: deliberately subtle — thin dashed outline, faint fill, in a
  // pane beneath the warnings — so "potential threat" areas are visible without
  // competing with the solid "happening now" warning polygons.
  function renderMapWatches(features) {
    if (!map || !watchLayer) return;
    watchLayer.clearLayers();
    features.forEach((f) => {
      if (!f.geometry) return;
      const sev = f.properties.severity || 'Unknown';
      const color = CONFIG.severityColor[sev] || CONFIG.severityColor.Unknown;
      L.geoJSON(f, {
        pane: 'watchesPane',
        style: { color, weight: 1.5, dashArray: '5 5', opacity: 0.8, fillColor: color, fillOpacity: 0.07 },
      }).bindPopup(
        `<strong>${escapeHtml(f.properties.event || 'Watch')}</strong><br>` +
        `${escapeHtml(f.properties.areaDesc || '')}<br>` +
        `<span class="popup-office">${escapeHtml(f.properties.senderName || '')}</span>`
      ).addTo(watchLayer);
    });
  }

  function renderMapLsr(features) {
    if (!map) return;
    lsrLayer.clearLayers();
    features.forEach((f) => {
      const c = f.geometry && f.geometry.coordinates;
      if (!c) return;
      const cat = lsrCategory(f.properties.typetext);
      // Solid fill + white ring + CSS halo (.lsr-dot) keeps every report dot
      // legible on top of any alert polygon fill, even one of the same color.
      L.circleMarker([c[1], c[0]], {
        pane: 'lsrPane', // forces these above the alert polygons
        className: 'lsr-dot',
        radius: 6, color: '#fff', weight: 2,
        fillColor: cat.color, fillOpacity: 1,
      }).bindPopup(
        `<strong>${escapeHtml(f.properties.typetext || '')}</strong><br>` +
        `${escapeHtml(f.properties.city || '')}<br>` +
        `${fmtTime(f.properties.valid)}`
      ).addTo(lsrLayer);
    });
  }

  function renderLegend() {
    const el = document.getElementById('map-legend');
    // Two groups, each with a swatch that mirrors its map form: alert areas as
    // translucent bordered squares, storm reports as white-ringed dots. Grouping
    // by form (not just color) keeps the two readable where palettes overlap —
    // e.g. red is both a Severe alert and a Wind report.
    const sevItems = Object.keys(CONFIG.severityRank)
      .sort((a, b) => CONFIG.severityRank[b] - CONFIG.severityRank[a])
      .map((s) => `<span class="legend-item"><span class="legend-swatch poly"
        style="--c:${CONFIG.severityColor[s]}"></span>${s}</span>`).join('')
      // Dashed outline = a watch (potential threat area), drawn beneath warnings.
      + '<span class="legend-item"><span class="legend-swatch watch"></span>Watch (outline)</span>';
    const lsrItems = CONFIG.lsrCategories.concat([CONFIG.lsrDefault])
      .map((c) => `<span class="legend-item"><span class="legend-swatch dot"
        style="--c:${c.color}"></span>${c.label}</span>`).join('');
    el.innerHTML =
      `<div class="legend-group">` +
        `<span class="legend-title">Alert areas</span>` +
        `<div class="legend-row">${sevItems}</div>` +
      `</div>` +
      `<div class="legend-group">` +
        `<span class="legend-title">Storm reports</span>` +
        `<div class="legend-row">${lsrItems}</div>` +
      `</div>`;
  }

  // ---- Radar overlay (RainViewer) --------------------------------------
  // Free, keyless precipitation tiles. We read RainViewer's manifest for the
  // newest frame and show it in radarPane (beneath the warnings). Refreshed on
  // a timer; the on/off choice is remembered in localStorage.

  let radarLayer = null;
  let radarFrameTime = null;
  let radarOn = (
    (localStorage.getItem('pwm-radar') ||
     (CONFIG.radar && CONFIG.radar.enabled ? 'on' : 'off')) === 'on'
  );

  function radarTileUrl(host, framePath) {
    const r = CONFIG.radar;
    return `${host}${framePath}/${r.size}/{z}/{x}/{y}/${r.color}/${r.smooth}_${r.snow}.png`;
  }

  function setRadarTimeLabel(unixSeconds) {
    const el = document.getElementById('radar-time');
    if (!el) return;
    el.textContent = (radarOn && unixSeconds)
      ? 'radar ' + fmtClock(new Date(unixSeconds * 1000))
      : '';
  }

  async function refreshRadar() {
    if (!map || !CONFIG.radar) return;
    try {
      const res = await fetch(CONFIG.radar.manifestUrl, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const frames = (data.radar && data.radar.past) || [];
      if (!frames.length) return;
      const latest = frames[frames.length - 1];
      if (latest.time === radarFrameTime && radarLayer) return; // already current

      // Build the new frame first, then swap it in, so there's no empty flash.
      const next = L.tileLayer(radarTileUrl(data.host, latest.path), {
        pane: 'radarPane',
        opacity: CONFIG.radar.opacity,
        maxNativeZoom: CONFIG.radar.maxNativeZoom,
        attribution: '<a href="https://www.rainviewer.com/" rel="noopener">RainViewer</a> radar',
      });
      if (radarOn) {
        next.addTo(map);
        if (radarLayer) map.removeLayer(radarLayer);
      }
      radarLayer = next;
      radarFrameTime = latest.time;
      setRadarTimeLabel(latest.time);
    } catch (err) {
      console.warn('radar refresh failed', err);
    }
  }

  function setRadar(on) {
    radarOn = on;
    try { localStorage.setItem('pwm-radar', on ? 'on' : 'off'); } catch (e) { /* ignore */ }
    const btn = document.getElementById('radar-toggle');
    if (btn) {
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
    }
    if (on) {
      if (radarLayer) { if (!map.hasLayer(radarLayer)) radarLayer.addTo(map); }
      else { refreshRadar(); }
      setRadarTimeLabel(radarFrameTime);
    } else if (radarLayer && map.hasLayer(radarLayer)) {
      map.removeLayer(radarLayer);
      setRadarTimeLabel(null);
    }
  }

  function startRadar() {
    if (!CONFIG.radar) return;
    const btn = document.getElementById('radar-toggle');
    if (btn) {
      btn.classList.toggle('on', radarOn);
      btn.setAttribute('aria-pressed', String(radarOn));
      btn.addEventListener('click', () => setRadar(!radarOn));
    }
    refreshRadar();
    setInterval(refreshRadar, CONFIG.radar.refreshMs);
  }

  // ---- Load cycles ------------------------------------------------------

  // Re-apply the expiry filter to the last fetched alerts and repaint.
  // Cheap; safe to call on a timer with no network involved.
  function applyAlerts() {
    const now = Date.now();
    // Demo fixtures are a frozen snapshot, so skip the expiry filter there
    // (it would drop sample alerts once their real expiry passes).
    const live = DEMO ? lastAlerts : lastAlerts.filter((f) => isAlertLive(f, now));
    // Neighboring offices' polygons — map only, pruned by expiry the same way.
    const liveMap = DEMO ? lastMapAlerts : lastMapAlerts.filter((f) => isAlertLive(f, now));
    const liveWatch = DEMO ? lastWatchAlerts : lastWatchAlerts.filter((f) => isAlertLive(f, now));
    const key = live.map(alertId).join('|') + '#' + liveMap.map(alertId).join('|') +
      '#' + liveWatch.map(alertId).join('|');
    if (key === lastAlertKey) return; // nothing changed; leave the DOM alone
    lastAlertKey = key;
    renderAlerts(live);                     // cards: PHI only
    updateSeverity(live);                   // threat meter + site tone (PHI alerts)
    renderMapWatches(liveWatch);            // map: subtle watch areas (region-wide)
    renderMapAlerts(live.concat(liveMap));  // map: PHI + neighboring offices' warnings
  }

  function applyLsr() {
    const now = Date.now();
    const windowMs = CONFIG.lsrWindowHours * 60 * 60 * 1000;
    const recent = DEMO ? lastLsr : lastLsr.filter((f) => isLsrRecent(f, now, windowMs));
    const key = recent.map(lsrId).join('|');
    if (key === lastLsrKey) return;
    lastLsrKey = key;
    renderLsr(recent);
    renderMapLsr(recent);
  }

  async function loadAlerts() {
    const url = DEMO ? 'samples/sample-alerts.json' : CONFIG.alertsUrl;
    try {
      const data = await fetchJSON(url);
      lastAlerts = sortAlerts(filterAlerts(data.features || []));
      applyAlerts();
      setStatus('alerts-status', `Alerts: updated ${fmtClock(new Date())}` + (DEMO ? ' (demo)' : ''), null);
    } catch (err) {
      console.error('alerts load failed', err);
      setStatus('alerts-status', `Alerts: error — retrying`, 'error');
      const list = document.getElementById('alerts-list');
      if (firstAlertPaint) {
        list.innerHTML = `<p class="error-msg">Could not load alerts (${escapeHtml(err.message)}). Will retry.</p>`;
      }
    }
  }

  async function loadLsr() {
    const url = DEMO
      ? 'samples/sample-lsr.json'
      : `${CONFIG.lsrBaseUrl}?hours=${CONFIG.lsrWindowHours}`;
    try {
      const data = await fetchJSON(url);
      lastLsr = sortLsr(filterLsr(data.features || []));
      applyLsr();
      setStatus('lsr-status', `Storm reports: updated ${fmtClock(new Date())}` + (DEMO ? ' (demo)' : ''), null);
    } catch (err) {
      console.error('lsr load failed', err);
      setStatus('lsr-status', `Storm reports: error — retrying`, 'error');
      const list = document.getElementById('lsr-list');
      if (firstLsrPaint) {
        list.innerHTML = `<p class="error-msg">Could not load storm reports (${escapeHtml(err.message)}). Will retry.</p>`;
      }
    }
  }

  function refreshAll() { loadAlerts(); loadLsr(); }

  // Enriched feed written by the rewrite pipeline: the original NWS/IEM
  // features plus a plain-language summary on each. One file covers both
  // columns, so a single fetch replaces the two live calls.
  async function loadFeed() {
    const sep = CONFIG.feedUrl.indexOf('?') < 0 ? '?' : '&';
    const data = await fetchJSON(CONFIG.feedUrl + sep + 't=' + Date.now());
    const gen = data.generated_at ? new Date(data.generated_at).getTime() : 0;
    const stale = !gen || (Date.now() - gen) > CONFIG.feedStaleMs;

    lastAlerts = sortAlerts((data.alerts && data.alerts.features) || []);
    lastLsr = sortLsr((data.lsr && data.lsr.features) || []);
    lastMapAlerts = (data.map_alerts && data.map_alerts.features) || [];
    lastWatchAlerts = (data.watch_alerts && data.watch_alerts.features) || [];
    applyAlerts();
    applyLsr();

    const stamp = fmtClock(new Date());
    const note = stale ? ' (feed delayed)' : '';
    setStatus('alerts-status', `Alerts: updated ${stamp}${note}`, stale ? 'stale' : null);
    setStatus('lsr-status', `Storm reports: updated ${stamp}${note}`, stale ? 'stale' : null);
    return !stale;
  }

  // What the Refresh button calls; swapped to loadFeed once the feed is in use.
  let refresh = refreshAll;

  // Try the enriched feed; if it is unreachable, degrade to the live APIs so
  // the page keeps working (just without the plain-language rewrites).
  async function bootFeed() {
    try {
      await loadFeed();
      refresh = loadFeed;
      setInterval(loadFeed, CONFIG.feedRefreshMs);
    } catch (err) {
      console.warn('enriched feed unavailable, using live APIs', err);
      refresh = refreshAll;
      refreshAll();
      setInterval(loadAlerts, CONFIG.alertsRefreshMs);
      setInterval(loadLsr, CONFIG.lsrRefreshMs);
    }
  }

  // ---- Boot -------------------------------------------------------------

  function boot() {
    initMap();
    startRadar();
    if (DEMO) {
      // Demo mode loads the bundled fixtures directly (no rewrite feed).
      refresh = refreshAll;
      refreshAll();
      setInterval(loadAlerts, CONFIG.alertsRefreshMs);
      setInterval(loadLsr, CONFIG.lsrRefreshMs);
    } else {
      bootFeed();
    }
    // Prune expired alerts / out-of-window reports between fetches, so the
    // display ages out correctly even if a poll is late or fails.
    setInterval(() => { applyAlerts(); applyLsr(); }, CONFIG.pruneMs);
    const btn = document.getElementById('refresh-btn');
    if (btn) btn.addEventListener('click', () => refresh());
  }

  // Leaflet is loaded with `defer`; wait for window load so L exists.
  if (document.readyState === 'complete') boot();
  else window.addEventListener('load', boot);
})();
