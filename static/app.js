'use strict';
/* =====================================================================
   PingTrace — network ops console frontend.
   Vanilla ES2020: no frameworks, no build step, no external assets.
   Talks strictly to the fixed /api contract documented in the README.
   ===================================================================== */
(function () {
  'use strict';

  /* ------------------------------ constants ------------------------------ */
  const RANGES = { '10m': 600000, '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 };
  const RANGE_ORDER = ['10m', '1h', '6h', '24h', '7d'];
  const INTERVAL_OPTIONS = [1, 2, 5, 10, 30, 60];
  const MAX_POINTS = 20000;
  const MAX_BACKOFF = 15000;
  const MIN_SPAN = 10000;
  const MAX_SPAN = 7 * 86400000;
  /* fixed, colour-blind-safe-ish palette; index by target order, never random */
  const PALETTE = ['#4EA6EA', '#E8A33D', '#3FB98A', '#E06C75', '#C678DD', '#56B6C2', '#D19A66', '#98C379'];

  const IDS = [
    'banner', 'banner-text', 'banner-meta',
    'hud-engine', 'engine-dot', 'engine-state', 'probes-total', 'probes-failed', 'clock', 'btn-pause',
    'range-group', 'toggle-auto', 'toggle-follow', 'toggle-loss', 'btn-reset-view',
    'bucket-readout', 'window-readout', 'points-readout',
    'targets', 'empty-state', 'chart-panel', 'chart-legend', 'latency-chart',
    'add-form', 'f-host', 'e-host', 'f-port', 'e-port', 'f-proto', 'e-proto',
    'f-interval', 'e-interval', 'f-label', 'e-label', 'btn-add', 'form-status',
    'event-list', 'events-empty',
    'db-size', 'db-path', 'retention', 'engine-version',
    'modal', 'modal-title', 'modal-body', 'modal-cancel', 'modal-ok',
    'btn-search', 'btn-export', 'export-status', 'resolution-readout', 'last-data',
    'data-stale', 'targets-down', 'db-rows',
    'search-panel', 'search-form', 'btn-search-close', 'btn-search-go', 'btn-search-clear',
    'btn-search-export', 's-host', 's-error', 's-from', 's-to', 's-only-fail',
    'search-quick', 'search-results', 'search-rows', 'search-note', 'search-status'
  ];
  const el = {};
  for (const id of IDS) el[id] = document.getElementById(id);
  const missingIds = IDS.filter((id) => !el[id]);
  if (missingIds.length) console.error('[pingtrace] missing elements: ' + missingIds.join(', '));

  /* ------------------------------ formatting ----------------------------- */
  const pad2 = (n) => (n < 10 ? '0' : '') + n;
  function hhmmss(ms) {
    const d = new Date(ms);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }
  function hhmm(ms) {
    const d = new Date(ms);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function fmtMs(v) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    const n = Number(v);
    if (n >= 1000) return (n / 1000).toFixed(2) + ' s';
    return n.toFixed(1) + ' ms';
  }
  function fmtNum(v) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    return Number(v).toLocaleString();
  }
  function fmtPct(v) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    return Number(v).toFixed(1) + '%';
  }
  function fmtBytes(n) {
    if (n === null || n === undefined || !isFinite(n)) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
  }
  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds)) return '—';
    let s = Math.max(0, Math.floor(Number(seconds)));
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + pad2(m) + 'm';
    if (m) return m + 'm ' + pad2(s) + 's';
    return s + 's';
  }
  function fmtBucket(ms) {
    if (!isFinite(ms) || ms <= 0) return '—';
    if (ms < 1000) return ms + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(0) + ' s';
    if (ms < 3600000) return (ms / 60000).toFixed(0) + ' min';
    return (ms / 3600000).toFixed(1) + ' h';
  }
  function fmtSpan(ms) {
    if (!isFinite(ms) || ms <= 0) return '—';
    if (ms < 3600000) return (ms / 60000).toFixed(0) + ' min';
    if (ms < 86400000) return (ms / 3600000).toFixed(1) + ' h';
    return (ms / 86400000).toFixed(1) + ' d';
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function withAlpha(hex, a) {
    const h = String(hex).replace('#', '');
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  }
  function targetName(t) {
    if (t.label) return t.label;
    const host = t.host.includes(':') && !t.host.startsWith('[') ? '[' + t.host + ']' : t.host;
    return host + (t.port ? ':' + t.port : '');
  }
  function colorFor(index) { return PALETTE[index % PALETTE.length]; }

  /* ------------------------------ state ---------------------------------- */
  const state = {
    paused: false,
    autoRefresh: true,
    follow: true,
    lossOverlay: true,
    range: '1h',
    view: null,                 // {from,to} when follow is off
    bucketMs: 0,
    connected: true,
    lastError: '',
    config: { default_interval_s: 5, min_interval_s: 1, max_interval_s: 300, db_size_bytes: 0, retention_hours: 0 },
    health: null,
    targets: new Map(),         // id -> latest /api/live target object
    order: [],                  // stable id order -> stable palette index
    series: new Map(),          // id -> {map:Map(ts->point), arr:[[ts,point]], sorted:bool, lastTs:number}
    hiddenSeries: new Set(),
    dataVersion: 0,
    renderedPoints: 0,
    modalResolve: null,
    modalPrevFocus: null,
    searchOpen: false,
    searchWindow: null,
    lastResolution: null
  };

  /* ------------------------------ fetch layer ---------------------------- */
  async function api(path, opts) {
    const o = opts || {};
    const res = await fetch(path, {
      method: o.method || 'GET',
      credentials: 'same-origin',
      headers: o.body ? { 'Content-Type': 'application/json', 'Accept': 'application/json' } : { 'Accept': 'application/json' },
      body: o.body ? JSON.stringify(o.body) : undefined
    });
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    if (!res.ok) {
      const err = new Error((data && data.detail) ? data.detail : 'HTTP ' + res.status + ' on ' + path);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function setConnected(ok, message) {
    state.connected = ok;
    state.lastError = ok ? '' : (message || state.lastError);
    const banner = el['banner'];
    if (!banner) return;
    if (ok) {
      banner.hidden = true;
      banner.classList.remove('is-on');
    } else {
      banner.hidden = false;
      banner.classList.add('is-on');
      if (el['banner-text']) el['banner-text'].textContent = 'disconnected from engine';
    }
  }

  window.addEventListener('unhandledrejection', (e) => {
    e.preventDefault();
    setConnected(false, String((e.reason && e.reason.message) || e.reason || 'unhandled rejection'));
    console.error('[pingtrace] unhandled rejection suppressed:', e.reason);
  });

  /* ------------------------------ polling scheduler ---------------------- */
  const JOBS = [
    { key: 'live', interval: 1000, path: () => '/api/live', run: onLive },
    { key: 'history', interval: 2000, gated: true, path: () => historyPath(), run: onHistory },
    { key: 'events', interval: 5000, path: () => '/api/events?limit=50', run: onEvents },
    { key: 'health', interval: 10000, path: () => '/api/health', run: onHealth },
    { key: 'config', interval: 10000, path: () => '/api/config', run: onConfig },
    { key: 'stats', interval: 30000, path: () => '/api/stats', run: onStats }
  ];
  for (const j of JOBS) { j.due = 0; j.fails = 0; j.busy = false; }

  function historyPath() {
    const ids = state.order.join(',');
    if (!ids) return null;
    if (state.searchWindow) {
      // a search is open: the graph shows the window being searched, not the live one
      return '/api/history/window?ids=' + encodeURIComponent(ids) +
        '&from_ms=' + state.searchWindow.from_ms + '&to_ms=' + state.searchWindow.to_ms + '&bucket=auto';
    }
    return '/api/history?ids=' + encodeURIComponent(ids) + '&range=' + encodeURIComponent(effectiveRange()) + '&bucket=auto';
  }
  function effectiveRange() {
    // widen the requested range so the visible window is always covered
    const need = state.view ? (state.view.to - state.view.from) : RANGES[state.range];
    for (const r of RANGE_ORDER) if (RANGES[r] >= need) return r;
    return '7d';
  }

  async function runJob(job) {
    if (job.busy) return;
    const path = job.path();
    if (path === null) { job.due = performance.now() + job.interval; return; }
    job.busy = true;
    try {
      const data = await api(path);
      job.fails = 0;
      job.due = performance.now() + job.interval;
      setConnected(true);
      job.run(data);
    } catch (err) {
      job.fails += 1;
      const backoff = Math.min(job.interval * Math.pow(2, job.fails), MAX_BACKOFF);
      job.due = performance.now() + backoff;
      if (job.key === 'live') setConnected(false, err.message);
      else console.warn('[pingtrace] ' + job.key + ' failed: ' + err.message);
      renderBannerMeta(backoff);
    } finally {
      job.busy = false;
    }
  }

  let bannerCountdown = 0;
  function renderBannerMeta(backoffMs) {
    bannerCountdown = Math.ceil((backoffMs || 0) / 1000);
    const meta = el['banner-meta'];
    if (meta) meta.textContent = 'retry in ' + bannerCountdown + 's' + (state.lastError ? ' · ' + state.lastError : '');
  }

  let lastSlide = 0;
  function slideWindow() {
    if (!chart) return;
    const w = windowBounds();
    chart.options.scales.x.min = w.from;
    chart.options.scales.x.max = w.to;
    chart.update('none');
  }

  function tick() {
    const now = performance.now();
    for (const job of JOBS) {
      if (state.paused) continue;
      if (job.gated && !state.autoRefresh) continue;
      if (document.hidden) continue;
      if (now >= job.due) runJob(job);
    }
    if (state.follow) {
      applyFollowWindow();
      if (chart && now - lastSlide > 1000) { lastSlide = now; slideWindow(); }
    }
    updateReadouts();
  }

  function kick(key) {
    const job = JOBS.find((j) => j.key === key);
    if (job) { job.due = 0; job.fails = 0; if (!state.paused) runJob(job); }
  }

  /* ------------------------------ clock / hud ---------------------------- */
  function tickClock() {
    if (el['clock']) el['clock'].textContent = hhmmss(Date.now());
    if (bannerCountdown > 0 && !state.connected) renderBannerMeta(bannerCountdown * 1000);
  }

  /* ------------------------------ chart ---------------------------------- */
  let chart = null;
  let chartColors = { grid: 'rgba(140,160,180,.14)', text: '#8b98a8', border: 'rgba(140,160,180,.25)' };

  function readThemeColors() {
    try {
      const cs = getComputedStyle(document.documentElement);
      const pick = (k, fb) => { const v = cs.getPropertyValue(k); return v && v.trim() ? v.trim() : fb; };
      chartColors = {
        grid: pick('--chart-grid', chartColors.grid),
        text: pick('--chart-text', chartColors.text),
        border: pick('--chart-border', chartColors.border)
      };
    } catch (e) { /* keep defaults */ }
  }

  function seriesEntry(id) {
    let s = state.series.get(id);
    if (!s) { s = { map: new Map(), arr: [], sorted: true, lastTs: -1 }; state.series.set(id, s); }
    return s;
  }
  function ingestPoint(s, ts, point) {
    const prev = s.map.get(ts);
    if (prev && prev.avg === point.avg && prev.max === point.max && prev.loss === point.loss) return false;
    s.map.set(ts, point);
    if (s.sorted) {
      if (s.arr.length && s.arr[s.arr.length - 1][0] === ts) s.arr[s.arr.length - 1] = [ts, point];
      else if (ts > s.lastTs) s.arr.push([ts, point]);
      else s.sorted = false;
    }
    if (ts > s.lastTs) s.lastTs = ts;
    if (s.map.size > MAX_POINTS) trimSeries(s);
    state.dataVersion += 1;
    return true;
  }
  function trimSeries(s) {
    if (s.map.size <= MAX_POINTS) return;
    const keys = Array.from(s.map.keys()).sort((a, b) => a - b);
    const drop = keys.slice(0, keys.length - MAX_POINTS);
    for (const k of drop) s.map.delete(k);
    s.sorted = false;
  }
  function sortedPoints(s) {
    if (!s.sorted) {
      s.arr = Array.from(s.map.entries()).sort((a, b) => a[0] - b[0]);
      s.sorted = true;
    }
    return s.arr;
  }

  /* Insert explicit null markers so omitted buckets read as visible breaks. */
  function toData(arr, bucketMs) {
    const out = [];
    const gapLimit = Math.max(bucketMs || 0, 1000) * 1.75;
    for (let i = 0; i < arr.length; i += 1) {
      const ts = arr[i][0], p = arr[i][1];
      if (i > 0) {
        const prevTs = arr[i - 1][0];
        if (ts - prevTs > gapLimit) {
          out.push({ x: prevTs, y: null });
          out.push({ x: ts, y: null });
        }
      }
      out.push({ x: ts, y: (p.avg === null || p.avg === undefined) ? null : Number(p.avg) });
    }
    return out;
  }

  function windowBounds() {
    const span = RANGES[state.range];
    if (state.follow || !state.view) { const to = Date.now(); return { from: to - span, to: to }; }
    return { from: state.view.from, to: state.view.to };
  }
  function applyFollowWindow() { state.view = null; }

  function buildDatasets() {
    const ds = [];
    const bucket = state.bucketMs;
    let points = 0;
    state.order.forEach((id, idx) => {
      const t = state.targets.get(id);
      const s = state.series.get(id);
      if (!t || !s) return;
      const color = colorFor(idx);
      const data = toData(sortedPoints(s), bucket);
      points += s.map.size;
      ds.push({
        label: targetName(t),
        targetId: id,
        kind: 'rtt',
        data: data,
        borderColor: color,
        backgroundColor: withAlpha(color, 0.25),
        borderWidth: 1.5,
        tension: 0,
        spanGaps: false,
        pointRadius: 1.6,
        pointHoverRadius: 4,
        pointBackgroundColor: color,
        pointBorderColor: 'transparent',
        fill: false,
        yAxisID: 'y',
        order: 1
      });
      if (state.lossOverlay) {
        ds.push({
          label: targetName(t) + ' loss',
          targetId: id,
          kind: 'loss',
          data: data.map((p) => ({ x: p.x, y: p.y === null ? null : (s.map.get(p.x).loss === null ? null : Number(s.map.get(p.x).loss)) })),
          borderColor: withAlpha(color, 0.55),
          backgroundColor: withAlpha(color, 0.13),
          borderWidth: 1,
          tension: 0,
          spanGaps: false,
          pointRadius: 0,
          pointHoverRadius: 0,
          fill: 'origin',
          yAxisID: 'yLoss',
          order: 5
        });
      }
    });
    state.renderedPoints = points;
    return ds;
  }

  function syncChart(force) {
    if (!chart) return;
    const signature = state.order.join(',') + '|' + (state.lossOverlay ? 'L' : '-') + '|' + Array.from(state.hiddenSeries).join('.');
    if (!force && signature === state.lastSignature && state.dataVersion === state.lastVersion) return;
    chart.data.datasets = buildDatasets();
    for (const id of state.hiddenSeries) {
      const idx = chart.data.datasets.findIndex((d) => d.targetId === id);
      if (idx >= 0) chart.setDatasetVisibility(idx, false);
    }
    const w = windowBounds();
    chart.options.scales.x.min = w.from;
    chart.options.scales.x.max = w.to;
    chart.options.scales.yLoss.display = state.lossOverlay;
    chart.update('none');
    state.lastSignature = signature;
    state.lastVersion = state.dataVersion;
    renderLegend();
    updateReadouts();
  }

  function createChart() {
    if (!window.Chart) {
      setConnected(false, 'chart library failed to load');
      console.error('[pingtrace] Chart.js not available');
      return;
    }
    readThemeColors();
    const canvas = el['latency-chart'];
    const ctx = canvas.getContext('2d');
    chart = new window.Chart(ctx, {
      type: 'line',
      data: { datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        normalized: true,
        disableTransitions: true,
        layout: { padding: { top: 4, right: 4, bottom: 0, left: 0 } },
        interaction: { mode: 'nearest', intersect: false, axis: 'x' },
        hover: { mode: 'nearest', intersect: false, axis: 'x' },
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: true,
            displayColors: true,
            borderWidth: 1,
            padding: 8,
            callbacks: {
              title: (items) => {
                if (!items.length) return '';
                const ts = items[0].parsed.x;
                return hhmmss(ts) + '  ·  bucket ' + fmtBucket(state.bucketMs);
              },
              label: (item) => {
                const t = state.targets.get(item.dataset.targetId);
                if (!t) return item.dataset.label;
                return ' ' + targetName(t) + '  [' + (t.proto || '?') + ']';
              },
              afterBody: (items) => {
                if (!items.length) return [];
                const ts = items[0].parsed.x;
                const lines = [];
                state.order.forEach((id) => {
                  const t = state.targets.get(id);
                  const s = state.series.get(id);
                  if (!t || !s) return;
                  const p = lookupAt(s, ts);
                  lines.push('  ' + pad(targetName(t), 22) + ' avg ' + pad(fmtMs(p ? p.avg : null), 9) +
                    ' max ' + pad(fmtMs(p ? p.max : null), 9) + ' loss ' + fmtPct(p ? p.loss : null));
                });
                return lines;
              }
            }
          }
        },
        scales: {
          x: {
            type: 'linear',
            min: Date.now() - RANGES[state.range],
            max: Date.now(),
            title: { display: true, text: 'local time', color: chartColors.text, font: { size: 10 } },
            grid: { color: chartColors.grid, drawTicks: false },
            border: { color: chartColors.border },
            ticks: {
              color: chartColors.text, maxRotation: 0, autoSkip: true, maxTicksLimit: 9,
              font: { family: 'ui-monospace, SFMono-Regular, Menlo, monospace', size: 10 },
              callback: (v) => hhmmss(v)
            }
          },
          y: {
            type: 'linear',
            beginAtZero: true,
            grace: '8%',
            title: { display: true, text: 'latency (ms)', color: chartColors.text, font: { size: 10 } },
            grid: { color: chartColors.grid, drawTicks: false },
            border: { color: chartColors.border },
            ticks: {
              color: chartColors.text, maxTicksLimit: 7,
              font: { family: 'ui-monospace, SFMono-Regular, Menlo, monospace', size: 10 },
              callback: (v) => v + ' ms'
            }
          },
          yLoss: {
            type: 'linear',
            display: false,
            position: 'right',
            min: 0,
            max: 100,
            title: { display: true, text: 'packet loss', color: chartColors.text, font: { size: 10 } },
            grid: { drawOnChartArea: false },
            border: { color: chartColors.border },
            ticks: {
              color: chartColors.text, stepSize: 25,
              font: { family: 'ui-monospace, SFMono-Regular, Menlo, monospace', size: 10 },
              callback: (v) => v + '%'
            }
          }
        }
      }
    });
    attachChartGestures();
  }
  function pad(s, n) { const v = String(s); return v.length >= n ? v : v + ' '.repeat(n - v.length); }

  function lookupAt(s, ts) {
    const arr = sortedPoints(s);
    let lo = 0, hi = arr.length - 1, best = null;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid][0] <= ts) { best = arr[mid]; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (best && ts - best[0] <= Math.max(state.bucketMs || 0, 1000) * 2) return best[1];
    return null;
  }

  /* ---------------------- pan / zoom / range controls ------------------- */
  function attachChartGestures() {
    const canvas = el['latency-chart'];
    let dragging = false, lastX = 0;

    canvas.addEventListener('pointerdown', (e) => {
      dragging = true; lastX = e.clientX;
      canvas.setPointerCapture(e.pointerId);
      canvas.classList.add('is-grabbing');
    });
    canvas.addEventListener('pointerup', (e) => {
      dragging = false;
      canvas.classList.remove('is-grabbing');
      try { canvas.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!dragging || !chart) return;
      const dx = e.clientX - lastX; lastX = e.clientX;
      if (!dx) return;
      setFollow(false);
      const area = chart.chartArea;
      if (!area || area.width <= 0) return;
      const w = windowBounds();
      const perPx = (w.to - w.from) / area.width;
      shiftWindow(-dx * perPx);
    });
    canvas.addEventListener('wheel', (e) => {
      if (!chart) return;
      e.preventDefault();
      setFollow(false);
      const area = chart.chartArea;
      if (!area) return;
      const w = windowBounds();
      const span = w.to - w.from;
      const factor = e.deltaY > 0 ? 1.25 : 0.8;
      const next = Math.min(MAX_SPAN, Math.max(MIN_SPAN, span * factor));
      const frac = Math.min(1, Math.max(0, (e.clientX - area.left) / Math.max(1, area.width)));
      const anchor = w.from + span * frac;
      let from = anchor - next * frac;
      let to = from + next;
      const now = Date.now();
      if (to > now + 60000) { to = now + 60000; from = to - next; }
      state.view = { from: from, to: to };
      syncChart(true);
    }, { passive: false });
  }

  function shiftWindow(deltaMs) {
    const w = windowBounds();
    const span = w.to - w.from;
    let from = w.from + deltaMs, to = from + span;
    const now = Date.now();
    if (to > now + 60000) { to = now + 60000; from = to - span; }
    if (from < to - MAX_SPAN) { from = to - MAX_SPAN; }
    state.view = { from: from, to: to };
    syncChart(true);
  }

  function setRange(range) {
    if (!RANGES[range]) return;
    state.range = range;
    state.view = null;
    const group = el['range-group'];
    if (group) group.querySelectorAll('.seg-btn').forEach((b) => {
      const on = b.dataset.range === range;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    kick('history');
    syncChart(true);
  }

  function setFollow(on) {
    state.follow = on;
    if (el['toggle-follow']) el['toggle-follow'].checked = on;
    if (on) state.view = null;
    else state.view = windowBounds();
    syncChart(true);
  }

  function updateReadouts() {
    const w = windowBounds();
    if (el['bucket-readout']) el['bucket-readout'].textContent = state.bucketMs ? fmtBucket(state.bucketMs) : '—';
    if (el['window-readout']) {
      el['window-readout'].textContent = hhmmss(w.from) + ' → ' + hhmmss(w.to) + ' (' + fmtSpan(w.to - w.from) + ')';
    }
    if (el['points-readout']) el['points-readout'].textContent = fmtNum(state.renderedPoints);
    setResolutionReadout(state.lastResolution);
    updateDataFreshness();
  }

  function renderLegend() {
    const box = el['chart-legend'];
    if (!box) return;
    const parts = [];
    state.order.forEach((id, idx) => {
      const t = state.targets.get(id);
      if (!t) return;
      const color = colorFor(idx);
      const hidden = state.hiddenSeries.has(id);
      parts.push('<button type="button" class="legend-item' + (hidden ? ' is-off' : '') +
        '" data-legend="' + id + '" aria-pressed="' + (hidden ? 'false' : 'true') + '" title="toggle this series">' +
        '<span class="legend-swatch" style="background:' + color + '"></span>' +
        '<span class="legend-name mono">' + esc(targetName(t)) + '</span>' +
        '<span class="legend-status status-' + esc(t.status || 'unknown') + '">' + esc(t.status || 'unknown') + '</span>' +
        '</button>');
    });
    box.innerHTML = parts.join('');
  }

  /* ------------------------------ target cards --------------------------- */
  const cards = new Map(); // id -> {root, fields:{}, cache:{}}

  function intervalOptions(current) {
    const min = state.config.min_interval_s || 1;
    const max = state.config.max_interval_s || 300;
    const opts = INTERVAL_OPTIONS.filter((v) => v >= min && v <= max);
    if (current && !opts.includes(current)) { opts.push(current); opts.sort((a, b) => a - b); }
    return opts.length ? opts : [5];
  }
  function optionList(values, selected) {
    return values.map((v) => '<option value="' + v + '"' + (v === selected ? ' selected' : '') + '>' + v + 's</option>').join('');
  }

  function cardMarkup(t) {
    return '' +
      '<div class="card-head">' +
        '<span class="pill status-unknown" data-f="pill">unknown</span>' +
        '<div class="card-id">' +
          '<span class="card-host mono" data-f="host"></span>' +
          '<span class="card-meta mono" data-f="meta"></span>' +
        '</div>' +
        '<div class="card-actions">' +
          '<label class="switch switch-sm" title="enable or disable probing"><input type="checkbox" data-action="enabled">enabled</label>' +
          '<select class="mini-select" data-action="interval" title="probe interval"></select>' +
          '<button type="button" class="btn btn-sm" data-action="trace" title="trace the hop-by-hop route to this target and back">trace route</button>' +
          '<button type="button" class="btn btn-icon" data-action="delete" title="delete target">✕</button>' +
        '</div>' +
      '</div>' +
      '<div class="metrics">' +
        metric('last rtt', 'last') + metric('avg', 'avg') + metric('max', 'max') +
        metric('min', 'min') + metric('jitter', 'jitter') +
        metric('probes', 'probes') + metric('failed', 'failed') +
        metric('consec. fails', 'consec') + metric('last probe', 'lastprobe') +
      '</div>' +
      '<div class="loss-bar" title="packet loss"><div class="loss-fill" data-f="lossfill"></div>' +
      '<span class="loss-text mono" data-f="loss">0.0%</span></div>' +
      '<div class="trace-out" data-f="traceout" hidden></div>';
  }
  function metric(k, f) {
    return '<div class="metric"><span class="metric-k">' + k + '</span>' +
      '<span class="metric-v mono" data-f="' + f + '">—</span></div>';
  }

  function ensureCard(t) {
    let c = cards.get(t.id);
    if (c) return c;
    const root = document.createElement('article');
    root.className = 'card';
    root.dataset.id = String(t.id);
    root.innerHTML = cardMarkup(t);
    const fields = {};
    root.querySelectorAll('[data-f]').forEach((n) => { fields[n.dataset.f] = n; });
    const sel = root.querySelector('[data-action="interval"]');
    sel.innerHTML = optionList(intervalOptions(t.interval_s), t.interval_s);
    const enabled = root.querySelector('[data-action="enabled"]');
    enabled.checked = t.enabled !== false;
    el['targets'].appendChild(root);
    c = { root: root, fields: fields, cache: {} };
    cards.set(t.id, c);
    return c;
  }

  function setField(c, key, value) {
    if (c.cache[key] === value) return;
    c.cache[key] = value;
    if (c.fields[key]) c.fields[key].textContent = value;
  }

  function renderCard(t, idx) {
    const c = ensureCard(t);
    const status = t.status || 'unknown';
    c.root.dataset.status = status;
    c.root.classList.toggle('is-down', status === 'down');
    c.root.classList.toggle('is-degraded', status === 'degraded');
    c.root.classList.toggle('is-disabled', t.enabled === false);
    c.root.classList.toggle('is-unknown', status === 'unknown');
    setField(c, 'pill', status);
    if (c.fields.pill) c.fields.pill.className = 'pill status-' + status;
    setField(c, 'host', targetName(t));
    setField(c, 'meta', (t.proto || '?').toUpperCase() + ' · every ' + fmtDuration(t.interval_s) +
      (t.label ? ' · ' + t.host + (t.port ? ':' + t.port : '') : ''));
    setField(c, 'last', fmtMs(t.last_rtt_ms));
    setField(c, 'avg', fmtMs(t.avg_rtt_ms));
    setField(c, 'max', fmtMs(t.max_rtt_ms));
    setField(c, 'min', fmtMs(t.min_rtt_ms));
    setField(c, 'jitter', fmtMs(t.jitter_ms));
    setField(c, 'probes', fmtNum(t.probes_total));
    setField(c, 'failed', fmtNum(t.probes_failed));
    setField(c, 'consec', fmtNum(t.consecutive_failures));
    setField(c, 'lastprobe', t.last_probe_at ? localTime(t.last_probe_at) : '—');
    const loss = Number(t.loss_pct) || 0;
    setField(c, 'loss', fmtPct(loss));
    if (c.fields.lossfill) {
      c.fields.lossfill.style.width = Math.max(0, Math.min(100, loss)) + '%';
      c.fields.lossfill.dataset.level = loss > 50 ? 'high' : (loss > 5 ? 'mid' : 'low');
    }
    const sel = c.root.querySelector('[data-action="interval"]');
    if (sel && Number(sel.value) !== Number(t.interval_s)) {
      sel.innerHTML = optionList(intervalOptions(t.interval_s), t.interval_s);
      sel.value = String(t.interval_s);
    }
    const enabled = c.root.querySelector('[data-action="enabled"]');
    if (enabled && enabled.checked !== (t.enabled !== false)) enabled.checked = t.enabled !== false;
  }

  function localTime(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return hhmmss(d.getTime());
  }

  function renderEmptyState() {
    const none = state.order.length === 0;
    if (el['empty-state']) el['empty-state'].hidden = !none;
    if (el['targets']) el['targets'].hidden = none;
    if (el['chart-panel']) el['chart-panel'].hidden = none;
    if (!none && chart) requestAnimationFrame(() => { try { chart.resize(); } catch (e) { /* ignore */ } });
  }

  function syncTargetSet() {
    for (const id of cards.keys()) {
      if (!state.targets.has(id)) { cards.get(id).root.remove(); cards.delete(id); }
    }
    for (const id of state.series.keys()) {
      if (!state.targets.has(id)) state.series.delete(id);
    }
    for (const id of Array.from(state.hiddenSeries)) {
      if (!state.targets.has(id)) state.hiddenSeries.delete(id);
    }
    renderEmptyState();
  }

  /* card interactions (delegated) */
  el['targets'].addEventListener('change', async (e) => {
    const action = e.target.dataset.action;
    const root = e.target.closest('.card');
    if (!root || !action) return;
    const id = Number(root.dataset.id);
    try {
      if (action === 'interval') {
        const value = Number(e.target.value);
        const t = await api('/api/targets/' + id, { method: 'PATCH', body: { interval_s: value } });
        setFormStatus('interval set to ' + value + 's', 'ok');
        applyTarget(t.target);
        kick('live');
      } else if (action === 'enabled') {
        const t = await api('/api/targets/' + id, { method: 'PATCH', body: { enabled: e.target.checked } });
        setFormStatus((e.target.checked ? 'enabled ' : 'disabled ') + targetName(t.target), 'ok');
        applyTarget(t.target);
        kick('live');
      }
    } catch (err) {
      setFormStatus('update failed: ' + err.message, 'err');
      const t = state.targets.get(id);
      if (t) renderCard(t, state.order.indexOf(id));
    }
  });

  el['targets'].addEventListener('click', async (e) => {
    const traceBtn = e.target.closest('[data-action="trace"]');
    if (traceBtn) { runTraceFor(traceBtn); return; }
    const btn = e.target.closest('[data-action="delete"]');
    if (!btn) return;
    const root = e.target.closest('.card');
    if (!root) return;
    const id = Number(root.dataset.id);
    const t = state.targets.get(id);
    const name = t ? targetName(t) : 'target #' + id;
    const ok = await openModal({
      title: 'Delete target',
      body: 'Stop probing ' + name + ' and drop its history from the graph? This cannot be undone.',
      okLabel: 'Delete target'
    });
    if (!ok) return;
    try {
      await api('/api/targets/' + id, { method: 'DELETE' });
      state.targets.delete(id);
      state.order = state.order.filter((x) => x !== id);
      syncTargetSet();
      state.dataVersion += 1;
      syncChart(true);
      setFormStatus('removed ' + name, 'ok');
      kick('live');
      kick('events');
    } catch (err) {
      setFormStatus('delete failed: ' + err.message, 'err');
    }
  });

  /* ------------------------------ route tracing -------------------------- */
  async function runTraceFor(btn) {
    const root = btn.closest('.card');
    if (!root) return;
    const id = Number(root.dataset.id);
    const out = root.querySelector('.trace-out');
    if (!out || btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.disabled = true;
    out.hidden = false;
    out.innerHTML = '<p class="trace-note">tracing route — up to 30 hops, a few seconds…</p>';
    try {
      const data = await api('/api/targets/' + id + '/trace', { method: 'POST' });
      renderTrace(out, data.trace);
    } catch (err) {
      out.innerHTML = '<p class="trace-error">trace failed: ' + esc(err.message) + '</p>';
    } finally {
      btn.dataset.busy = '';
      btn.disabled = false;
    }
  }

  // Fixed latency bands for the hop bars, mirroring the green/amber/red
  // reading of classic path monitors. The bar scale itself adapts to the
  // worst hop on the path, but the colour zones stay put.
  const TRACE_WARN_MS = 100;
  const TRACE_CRIT_MS = 300;

  function hopBand(avgMs) {
    if (avgMs === null) return 'silent';
    if (avgMs >= TRACE_CRIT_MS) return 'bad';
    if (avgMs >= TRACE_WARN_MS) return 'warn';
    return 'ok';
  }

  function renderTrace(out, tr) {
    if (!tr || !Array.isArray(tr.hops)) {
      out.innerHTML = '<p class="trace-error">trace returned no hops</p>';
      return;
    }
    const dest = esc(tr.dest_ip || '');
    const head = tr.reached
      ? 'route to ' + dest + ' — destination reached, ' + tr.hops.length + ' hop(s)'
      : 'route to ' + dest + ' — destination did not answer; showing the ' +
        tr.hops.length + ' hop(s) that did';
    let html = '<p class="trace-head">' + head +
      ' <span class="trace-meta">' + fmtMs(tr.duration_ms) + ' · ' + esc(tr.method || '') + '</span></p>';

    // Per-hop stats: average of the probes that answered, and the worst.
    const stats = tr.hops.map((h) => {
      const sent = (h.rtt_ms || []).length;
      const rs = (h.rtt_ms || []).filter((r) => typeof r === 'number');
      if (!rs.length) return { avg: null, max: null, lost: 0, sent: 0 };
      return {
        avg: rs.reduce((a, b) => a + b, 0) / rs.length,
        max: Math.max(...rs),
        lost: sent - rs.length,
        sent: sent,
      };
    });
    const peak = Math.max(...stats.map((s) => s.max || 0));
    // Scale to the actual path, rounded up to a readable tick; colour zones
    // stay at their fixed ms positions even when they fall off-scale.
    const raw = Math.max(peak * 1.15, 5);
    const scaleLabel = raw <= 100 ? Math.ceil(raw / 5) * 5 : Math.ceil(raw / 50) * 50;
    const scale = scaleLabel;
    const pct = (ms) => Math.max(0, Math.min(100, (ms / scale) * 100));

    html += '<table class="trace-table"><thead><tr>' +
      '<th class="c-hop">hop</th><th>address</th><th class="c-avg">avg</th>' +
      '<th class="c-bar"><span>latency</span><span class="trace-scale">0</span>' +
      '<span class="trace-scale trace-scale-max">' + scaleLabel + ' ms</span></th>' +
      '</tr></thead><tbody>';
    tr.hops.forEach((h, i) => {
      const s = stats[i];
      const band = hopBand(s.avg);
      const title = (h.answered || 0) + '/' + s.sent + ' probes · via ' +
        (h.via || 'no reply');
      const badge = '<span class="trace-hop trace-' + band + '" title="' +
        esc(title) + '">' + esc(h.ttl) + '</span>';
      const addr = h.ip
        ? '<span class="trace-ip">' + esc(h.ip) + '</span>' +
          (h.hostname ? ' <span class="trace-name" title="' + esc(h.hostname) +
            '">' + esc(h.hostname) + '</span>' : '')
        : '<span class="trace-silent">·</span>';
      const avg = s.avg === null
        ? '<span class="trace-silent">·</span>'
        : fmtMs(s.avg) + (s.lost ? '<span class="trace-loss" title="' + s.lost +
            ' of ' + s.sent + ' probes lost"> ×' + s.lost + '</span>' : '');
      let bar = '';
      if (s.avg !== null) {
        bar = '<div class="trace-bar" style="--zone-warn:' + pct(TRACE_WARN_MS).toFixed(1) +
          '%;--zone-crit:' + pct(TRACE_CRIT_MS).toFixed(1) + '%">' +
          '<span class="trace-bar-fill trace-' + band + '" style="width:' +
          pct(s.avg).toFixed(1) + '%"></span>' +
          (s.max > s.avg * 1.05 ? '<span class="trace-bar-max" style="left:' +
            pct(s.max).toFixed(1) + '%"></span>' : '') +
          '</div>';
      }
      html += '<tr' + (h.final ? ' class="trace-final"' : '') + '>' +
        '<td class="c-hop">' + badge + '</td>' +
        '<td>' + addr + '</td>' +
        '<td class="c-avg mono trace-avg trace-' + band + '">' + avg + '</td>' +
        '<td class="c-bar">' + bar + '</td></tr>';
    });
    html += '</tbody></table>';

    // Round-trip summary, like the reference tools put under the table.
    const finalIdx = tr.hops.findIndex((h) => h.final);
    const shown = finalIdx >= 0 ? stats[finalIdx]
      : stats.reduce((acc, s) => (s.avg !== null ? s : acc), null);
    const footLabel = finalIdx >= 0 ? 'round trip' : 'last answering hop';
    const footValue = shown && shown.avg !== null ? fmtMs(shown.avg) : 'no reply';
    html += '<div class="trace-foot"><span>' + footLabel + '</span>' +
      '<span class="mono">' + footValue + '</span></div>';
    html += '<p class="trace-note">' + esc(tr.note || '') + '</p>';
    out.innerHTML = html;
  }

  function applyTarget(t) {
    if (!t || t.id === undefined) return;
    state.targets.set(t.id, Object.assign({}, state.targets.get(t.id), t));
    if (!state.order.includes(t.id)) { state.order.push(t.id); state.order.sort((a, b) => a - b); }
    renderCard(state.targets.get(t.id), state.order.indexOf(t.id));
    renderLegend();
  }

  /* ------------------------------ poll handlers -------------------------- */
  function onLive(data) {
    const list = (data && data.targets) || [];
    let total = 0, failed = 0, down = 0;
    for (const t of list) {
      state.targets.set(t.id, Object.assign({}, state.targets.get(t.id), t));
      total += Number(t.probes_total) || 0;
      failed += Number(t.probes_failed) || 0;
      if (t.status === 'down') down += 1;
    }
    const nextOrder = list.map((t) => t.id).sort((a, b) => a - b);
    const changed = nextOrder.join(',') !== state.order.join(',');
    state.order = nextOrder;
    if (changed) syncTargetSet();
    state.order.forEach((id, idx) => { const t = state.targets.get(id); if (t) renderCard(t, idx); });
    for (const t of list) { if (!state.series.has(t.id)) seriesEntry(t.id); }
    setHudNumber('probes-total', total);
    setHudNumber('probes-failed', failed);
    setHudNumber('targets-down', down);
    renderLegend();
  }
  function setHudNumber(id, v) { const n = el[id]; if (n) n.textContent = fmtNum(v); }

  function onHistory(data) {
    if (!data) return;
    state.bucketMs = Number(data.bucket_ms) || 0;
    state.lastResolution = data.resolution || null;
    for (const t of (data.targets || [])) {
      const s = seriesEntry(t.id);
      for (const p of (t.points || [])) {
        if (!Array.isArray(p) || p.length < 4) continue;
        const ts = Number(p[0]);
        if (!isFinite(ts)) continue;
        ingestPoint(s, ts, { avg: p[1], max: p[2], loss: p[3] });
      }
    }
    syncChart(false);
  }

  const EVENT_LABEL = { up: 'up', down: 'down', added: 'added', removed: 'removed', error: 'error' };
  function onEvents(data) {
    const list = ((data && data.events) || []).slice().sort((a, b) => {
      const ta = Date.parse(a.created_at || '') || 0, tb = Date.parse(b.created_at || '') || 0;
      return (tb - ta) || (Number(b.id || 0) - Number(a.id || 0));
    });
    const box = el['event-list'];
    if (!box) return;
    if (!list.length) {
      box.innerHTML = '';
      if (el['events-empty']) el['events-empty'].hidden = false;
      return;
    }
    if (el['events-empty']) el['events-empty'].hidden = true;
    box.innerHTML = list.map((ev) => {
      const kind = EVENT_LABEL[ev.event] ? ev.event : 'error';
      const ts = ev.created_at ? localTime(ev.created_at) : '—';
      return '<div class="event ev-' + kind + '">' +
        '<span class="ev-time mono">' + esc(ts) + '</span>' +
        '<span class="ev-kind">' + esc(kind) + '</span>' +
        '<span class="ev-host mono">' + esc(ev.host || (ev.target_id === null ? '—' : 'target ' + ev.target_id)) + '</span>' +
        '<span class="ev-detail">' + esc(ev.detail || '') + '</span>' +
        '</div>';
    }).join('');
  }

  function onStats(data) {
    if (!data) return;
    setHudText('db-rows', fmtNum(data.probes_raw) + ' raw / ' + fmtNum(data.rollup_rows) + ' rollup');
  }
  function setHudText(id, v) { const n = el[id]; if (n) n.textContent = v; }

  function onHealth(data) {
    state.health = data;
    const dot = el['engine-dot'];
    const ok = data && data.status === 'ok';
    if (dot) dot.className = 'dot ' + (ok ? 'dot-ok' : 'dot-bad');
    const stateEl = el['engine-state'];
    if (stateEl) {
      stateEl.textContent = ok
        ? 'ok · up ' + fmtDuration(data.engine_uptime_s) + ' · ' + (data.targets_active || 0) + ' targets'
        : 'unhealthy';
    }
    if (el['db-path']) el['db-path'].textContent = (data && data.db_path) || '—';
    if (el['engine-version']) el['engine-version'].textContent = (data && data.version) || '—';
    if (data && data.interval_s) { /* engine default; nothing to render beyond config */ }
  }

  function onConfig(data) {
    if (!data) return;
    state.config = Object.assign({}, state.config, data);
    if (el['db-size']) el['db-size'].textContent = fmtBytes(data.db_size_bytes);
    if (el['retention']) {
      const h = Number(data.retention_hours);
      el['retention'].textContent = isFinite(h)
        ? (h >= 48 ? (h / 24).toFixed(h % 24 ? 1 : 0) + ' days' : h + ' h')
        : '—';
    }
    populateFormIntervals();
    for (const [id, c] of cards) {
      const t = state.targets.get(id);
      if (!t) continue;
      const sel = c.root.querySelector('[data-action="interval"]');
      if (sel) sel.innerHTML = optionList(intervalOptions(t.interval_s), t.interval_s);
    }
  }

  function populateFormIntervals() {
    const sel = el['f-interval'];
    if (!sel) return;
    const def = state.config.default_interval_s || 5;
    const values = intervalOptions(def);
    const current = Number(sel.value) || def;
    sel.innerHTML = optionList(values, values.includes(current) ? current : def);
  }

  /* ------------------------------ add-target form ------------------------ */
  const HOST_RE = /^[a-z0-9][a-z0-9.\-_]*$/i;
  const IPV4_RE = /^(\d{1,3}\.){3}\d{1,3}$/;
  const IPV6_RE = /^[0-9a-f:]+$/i;
  function validHost(h) {
    if (!h) return false;
    if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1);
    if (IPV4_RE.test(h)) return h.split('.').every((p) => Number(p) <= 255);
    if (h.includes(':')) return IPV6_RE.test(h) && h.includes(':');
    return HOST_RE.test(h) && h.length <= 253;
  }

  function fieldError(field, message) {
    const node = el['e-' + field];
    if (node) { node.textContent = message || ''; node.hidden = !message; }
    const input = el['f-' + field];
    if (input) input.classList.toggle('is-invalid', !!message);
  }
  function clearFieldErrors() {
    ['host', 'port', 'proto', 'interval', 'label'].forEach((f) => fieldError(f, ''));
  }
  function setFormStatus(text, kind) {
    const n = el['form-status'];
    if (!n) return;
    n.textContent = text || '';
    n.className = 'form-status' + (kind ? ' form-status-' + kind : '');
    if (text) { clearTimeout(setFormStatus._t); setFormStatus._t = setTimeout(() => { n.textContent = ''; n.className = 'form-status'; }, 6000); }
  }

  function surfaceApiError(err) {
    const msg = err.message || 'request failed';
    const low = msg.toLowerCase();
    const map = [
      ['host', /host|hostname|resolve|address|invalid host/],
      ['port', /port/],
      ['interval', /interval/],
      ['label', /label/],
      ['proto', /proto|protocol/]
    ];
    let matched = null;
    for (const [field, re] of map) { if (re.test(low)) { matched = field; break; } }
    if (matched) fieldError(matched, msg);
    else setFormStatus(msg, 'err');
    if (matched) setFormStatus('check the highlighted field', 'err');
  }

  // ICMP echo has no port: grey the field out so the form reads honestly.
  el['f-proto'].addEventListener('change', () => {
    const icmp = el['f-proto'].value === 'icmp';
    el['f-port'].disabled = icmp;
    if (icmp) fieldError('port', '');
  });

  el['add-form'].addEventListener('submit', async (e) => {
    e.preventDefault();
    clearFieldErrors();
    const host = el['f-host'].value.trim();
    const portRaw = el['f-port'].value.trim();
    const proto = el['f-proto'].value;
    const interval = Number(el['f-interval'].value);
    const label = el['f-label'].value.trim();

    let bad = false;
    if (!host) { fieldError('host', 'host is required'); bad = true; }
    else if (!validHost(host)) { fieldError('host', 'not a valid hostname or IPv4/IPv6 address'); bad = true; }
    let port = null;
    if (proto === 'icmp') {
      port = null;  // ICMP echo has no port; the server stores 0
    } else if (portRaw !== '') {
      port = Number(portRaw);
      if (!Number.isInteger(port) || port < 1 || port > 65535) { fieldError('port', 'port must be an integer between 1 and 65535'); bad = true; }
    } else if (proto === 'tcp' || proto === 'udp') {
      port = 443;
    }
    const min = state.config.min_interval_s || 1;
    const max = state.config.max_interval_s || 300;
    if (!Number.isFinite(interval) || interval < min || interval > max) {
      fieldError('interval', 'interval must be between ' + min + 's and ' + max + 's'); bad = true;
    }
    if (label.length > 64) { fieldError('label', 'label is limited to 64 characters'); bad = true; }
    if (bad) { setFormStatus('fix the highlighted fields', 'err'); return; }

    const btn = el['btn-add'];
    btn.disabled = true;
    setFormStatus('adding…', 'ok');
    try {
      const data = await api('/api/targets', {
        method: 'POST',
        body: { host: host, port: port, proto: proto, interval_s: interval, label: label || null }
      });
      const t = data.target || data;
      applyTarget(t);
      seriesEntry(t.id);
      state.dataVersion += 1;
      syncChart(true);
      setFormStatus('added ' + targetName(t), 'ok');
      el['f-host'].value = '';
      el['f-label'].value = '';
      kick('live'); kick('history'); kick('events');
    } catch (err) {
      surfaceApiError(err);
    } finally {
      btn.disabled = false;
    }
  });

  /* ------------------------------ modal confirm -------------------------- */
  function openModal(opts) {
    const modal = el['modal'];
    if (!modal) return Promise.resolve(true);
    state.modalPrevFocus = document.activeElement;
    if (el['modal-title']) el['modal-title'].textContent = opts.title || 'Confirm';
    if (el['modal-body']) el['modal-body'].textContent = opts.body || '';
    if (el['modal-ok']) el['modal-ok'].textContent = opts.okLabel || 'Confirm';
    modal.hidden = false;
    modal.classList.add('is-open');
    if (el['modal-ok']) el['modal-ok'].focus();
    return new Promise((resolve) => { state.modalResolve = resolve; });
  }
  function closeModal(accepted) {
    const modal = el['modal'];
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    modal.classList.remove('is-open');
    const resolve = state.modalResolve;
    state.modalResolve = null;
    if (state.modalPrevFocus && typeof state.modalPrevFocus.focus === 'function') state.modalPrevFocus.focus();
    if (resolve) resolve(!!accepted);
  }
  if (el['modal-ok']) el['modal-ok'].addEventListener('click', () => closeModal(true));
  if (el['modal-cancel']) el['modal-cancel'].addEventListener('click', () => closeModal(false));
  if (el['modal']) el['modal'].addEventListener('click', (e) => { if (e.target === el['modal']) closeModal(false); });

  /* ------------------------------ search + export ------------------------ */
  const SEARCH_MAX_MS = 7 * 86400000;
  const SOURCE_LABEL = {
    raw: 'raw probe log',
    rollup: '60 s rollup table',
    'rollup + raw': '60 s rollup table + raw probe log'
  };

  function setFormStatus(id, text, kind) {
    const n = el[id];
    if (!n) return;
    n.textContent = text || '';
    n.className = 'form-status' + (kind ? ' form-status-' + kind : '');
  }

  /* The graph's own resolution, stated plainly: which table the points came
     from, how coarse the buckets are, and whether that is coarser than the
     data actually allows. The user cannot infer any of this from the curve. */
  function setResolutionReadout(res) {
    const node = el['resolution-readout'];
    if (!node) return;
    if (!res || !res.bucket_ms) { node.textContent = '—'; node.title = ''; return; }
    const bucket = fmtBucket(res.bucket_ms);
    const src = SOURCE_LABEL[res.source] || 'unknown source';
    const finest = fmtBucket(res.finest_available_ms);
    let text;
    if (res.source === 'raw' && !res.downsampled) text = 'raw probes · ' + bucket + ' · 1:1';
    else if (res.source === 'raw') text = 'raw probes → ' + bucket + ' buckets';
    else text = 'rollup → ' + bucket + ' buckets';
    node.textContent = text;
    const notes = [
      'points in this window come from the ' + src,
      res.rollup_span_ms ? 'served by the rollup table: ' + fmtSpan(res.rollup_span_ms) : null,
      res.raw_span_ms ? 'served by raw probe rows: ' + fmtSpan(res.raw_span_ms) : null,
      res.downsampled ? 'downsampled: raw rows exist at ' + finest + ' but are bucketed to ' + bucket :
                       'no downsampling: ' + bucket + ' is the finest the probes allow',
      'raw probe rows are kept for ' + fmtSpan(res.retention_hours * 3600000) +
        ' (from ' + localTime(new Date(res.raw_available_from_ms).toISOString()) + '); older history exists only as 60 s rollups'
    ].filter(Boolean);
    node.title = notes.join('\n');
  }

  /* "last data" is the newest point actually on the graph, not the wall clock. */
  function newestPointTs() {
    let best = -1;
    for (const s of state.series.values()) if (s.lastTs > best) best = s.lastTs;
    return best;
  }
  function updateDataFreshness() {
    const newest = newestPointTs();
    const node = el['last-data'];
    if (node) node.textContent = newest > 0 ? hhmmss(newest) : 'no data yet';
    const stale = el['data-stale'];
    if (!stale) return;
    if (newest <= 0) { stale.hidden = false; stale.textContent = 'no data on the graph yet'; return; }
    const age = Date.now() - newest;
    if (age > 60000) { stale.hidden = false; stale.textContent = 'newest point is ' + fmtSpan(age) + ' old'; }
    else stale.hidden = true;
  }

  function toLocalInput(ms) {
    const d = new Date(ms);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function fillSearchWindow(spanMs) {
    const to = Date.now();
    el['s-from'].value = toLocalInput(to - spanMs);
    el['s-to'].value = toLocalInput(to);
  }
  function readSearchWindow() {
    if (!el['s-from'].value || !el['s-to'].value) return { error: 'set both a start and an end' };
    const from = Date.parse(el['s-from'].value), to = Date.parse(el['s-to'].value);
    if (!isFinite(from) || !isFinite(to)) return { error: 'those dates are not valid' };
    if (to <= from) return { error: 'the end must be after the start' };
    if (to - from > SEARCH_MAX_MS) return { error: 'the window is limited to 7 days' };
    return { from_ms: from, to_ms: to };
  }

  function openSearch(open) {
    state.searchOpen = open;
    if (el['search-panel']) el['search-panel'].hidden = !open;
    if (el['btn-search']) el['btn-search'].setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      if (!el['s-from'].value) fillSearchWindow(3600000);
      if (el['s-host']) el['s-host'].focus();
    }
  }

  /* Leaving the search view puts the live rolling window back on the graph. */
  function clearSearch() {
    state.searchWindow = null;
    state.searchOpen = false;
    if (el['search-panel']) el['search-panel'].hidden = true;
    if (el['search-results']) el['search-results'].hidden = true;
    if (el['search-rows']) el['search-rows'].innerHTML = '';
    if (el['search-note']) el['search-note'].textContent = '';
    if (el['btn-search-export']) el['btn-search-export'].disabled = true;
    setFormStatus('search-status', '', '');
    state.dataVersion += 1;
    syncChart(true);
    kick('history');
  }

  function renderSearchResults(data) {
    const rows = (data && data.results) || [];
    const body = el['search-rows'];
    if (body) {
      body.innerHTML = rows.map((r) => {
        const ts = Number(r.ts_ms);
        const ok = !!r.ok;
        const result = ok ? 'ok' : 'loss' + (r.error ? ' · ' + r.error : '');
        return '<tr>' +
          '<td class="mono">' + esc(localTime(new Date(ts).toISOString())) + ' ' + esc(hhmmss(ts)) + '</td>' +
          '<td class="mono">' + esc(r.host + (r.port ? ':' + r.port : '')) + '</td>' +
          '<td>' + esc(String(r.proto || '').toUpperCase()) + '</td>' +
          '<td class="' + (ok ? 'cell-ok' : 'cell-bad') + '">' + esc(result) + '</td>' +
          '<td class="mono">' + fmtMs(r.rtt_ms) + '</td>' +
          '</tr>';
      }).join('');
    }
    if (el['search-results']) el['search-results'].hidden = rows.length === 0;
    if (el['btn-search-export']) el['btn-search-export'].disabled = rows.length === 0;
    return rows.length;
  }

  /* Say what the table could not possibly contain, instead of showing nothing
     and letting the user conclude the probes never ran. */
  function renderSearchNote(data) {
    const node = el['search-note'];
    if (!node || !data) return;
    const rawFrom = Number(data.raw_available_from_ms) || 0;
    const asked = Number(data.searched_from_ms) || 0;
    if (asked < rawFrom) {
      node.textContent = 'Raw probe rows only exist from ' + localTime(new Date(rawFrom).toISOString()) +
        ' (' + fmtSpan(Date.now() - rawFrom) + ' ago). Everything before that was rolled up into 60 s ' +
        'buckets — it is on the graph, but there are no individual rows to list here.';
    } else {
      node.textContent = 'Individual probe rows from ' + localTime(new Date(asked).toISOString()) +
        ' to ' + localTime(new Date(data.searched_to_ms).toISOString()) + '.';
    }
  }

  async function runSearch() {
    const w = readSearchWindow();
    if (w.error) { setFormStatus('search-status', w.error, 'err'); return; }
    const host = (el['s-host'].value || '').trim();
    const errText = (el['s-error'].value || '').trim();
    const onlyFail = !!el['s-only-fail'].checked;
    state.searchWindow = { from_ms: w.from_ms, to_ms: w.to_ms, host: host };
    state.dataVersion += 1;
    setFormStatus('search-status', 'searching…', 'ok');
    try {
      const q = new URLSearchParams({ from_ms: String(w.from_ms), to_ms: String(w.to_ms), limit: '500' });
      if (host) q.set('target', host);
      if (errText) q.set('q', errText);
      if (onlyFail) q.set('only_failures', 'true');
      const data = await api('/api/search?' + q.toString());
      const n = renderSearchResults(data);
      renderSearchNote(data);
      const capped = data.count >= (data.limit || 500) ? ' (showing the first ' + data.limit + ')' : '';
      setFormStatus('search-status', fmtNum(n) + ' probe rows in that window' + capped, n ? 'ok' : 'err');
    } catch (err) {
      setFormStatus('search-status', 'search failed: ' + err.message, 'err');
    }
    // the graph follows the same window the table is describing
    kick('history');
  }

  function exportPath() {
    const q = new URLSearchParams();
    if (state.searchWindow) {
      q.set('from_ms', String(state.searchWindow.from_ms));
      q.set('to_ms', String(state.searchWindow.to_ms));
      if (state.searchWindow.host) q.set('target', state.searchWindow.host);
    } else {
      q.set('range', state.range);
    }
    return '/api/export.csv?' + q.toString();
  }

  async function downloadCsv(statusId) {
    setFormStatus(statusId, 'exporting…', 'ok');
    try {
      const res = await fetch(exportPath(), { credentials: 'same-origin', headers: { Accept: 'text/csv' } });
      if (!res.ok) {
        let detail = 'HTTP ' + res.status;
        try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
        throw new Error(detail);
      }
      const blob = await res.blob();
      const text = await blob.text();
      const rows = Math.max(0, text.split('\n').filter((l) => l.length).length - 1);
      const m = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
      const filename = m ? m[1] : 'pingtrace-export.csv';
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href; a.download = filename; a.rel = 'noopener';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 30000);
      setFormStatus(statusId, 'wrote ' + filename + ' · ' + fmtBytes(blob.size) + ' · ' + fmtNum(rows) + ' rows', 'ok');
    } catch (err) {
      setFormStatus(statusId, 'export failed: ' + err.message, 'err');
    }
  }

  /* ------------------------------ controls ------------------------------- */
  function setPaused(paused) {
    state.paused = paused;
    const btn = el['btn-pause'];
    if (btn) {
      btn.textContent = paused ? 'Resume' : 'Pause';
      btn.setAttribute('aria-pressed', paused ? 'true' : 'false');
      btn.classList.toggle('is-paused', paused);
    }
    document.body.classList.toggle('is-paused', paused);
    if (!paused) { for (const j of JOBS) j.due = 0; }
  }
  if (el['btn-pause']) el['btn-pause'].addEventListener('click', () => setPaused(!state.paused));

  if (el['toggle-auto']) el['toggle-auto'].addEventListener('change', (e) => {
    state.autoRefresh = e.target.checked;
    if (state.autoRefresh) kick('history');
  });
  if (el['toggle-follow']) el['toggle-follow'].addEventListener('change', (e) => setFollow(e.target.checked));
  if (el['toggle-loss']) el['toggle-loss'].addEventListener('change', (e) => {
    state.lossOverlay = e.target.checked;
    syncChart(true);
  });
  if (el['btn-reset-view']) el['btn-reset-view'].addEventListener('click', () => {
    state.hiddenSeries.clear();
    setFollow(true);
    syncChart(true);
  });
  if (el['range-group']) el['range-group'].addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn || !btn.dataset.range) return;
    if (state.searchWindow) clearSearch();   // picking a live range leaves the search view
    setRange(btn.dataset.range);
  });
  if (el['btn-search']) el['btn-search'].addEventListener('click', () => openSearch(el['search-panel'].hidden));
  if (el['btn-search-close']) el['btn-search-close'].addEventListener('click', () => clearSearch());
  if (el['search-form']) el['search-form'].addEventListener('submit', (e) => { e.preventDefault(); runSearch(); });
  if (el['btn-search-go']) el['btn-search-go'].addEventListener('click', () => runSearch());
  if (el['btn-search-clear']) el['btn-search-clear'].addEventListener('click', () => {
    el['s-host'].value = ''; el['s-error'].value = ''; el['s-only-fail'].checked = false;
    renderSearchResults({ results: [] });
    if (el['search-note']) el['search-note'].textContent = '';
    setFormStatus('search-status', '', '');
  });
  if (el['btn-search-export']) el['btn-search-export'].addEventListener('click', () => downloadCsv('search-status'));
  if (el['btn-export']) el['btn-export'].addEventListener('click', () => downloadCsv('export-status'));
  if (el['search-quick']) el['search-quick'].addEventListener('click', (e) => {
    const btn = e.target.closest('[data-span]');
    if (!btn) return;
    for (const b of el['search-quick'].querySelectorAll('.seg-btn')) b.classList.toggle('is-on', b === btn);
    fillSearchWindow(Number(btn.dataset.span));
    runSearch();
  });
  if (el['chart-legend']) el['chart-legend'].addEventListener('click', (e) => {
    const btn = e.target.closest('[data-legend]');
    if (!btn) return;
    const id = Number(btn.dataset.legend);
    if (state.hiddenSeries.has(id)) state.hiddenSeries.delete(id); else state.hiddenSeries.add(id);
    syncChart(true);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (el['modal'] && !el['modal'].hidden) { e.preventDefault(); closeModal(false); return; }
      if (state.searchOpen) { e.preventDefault(); clearSearch(); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (el['modal'] && !el['modal'].hidden) return;
    const k = e.key.toLowerCase();
    if (k === 'p') { e.preventDefault(); setPaused(!state.paused); }
    else if (k === 'a') { e.preventDefault(); el['f-host'].focus(); el['f-host'].select(); }
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) for (const j of JOBS) j.due = 0; });

  /* ------------------------------ boot ----------------------------------- */
  function boot() {
    if (missingIds.length) return;
    for (const b of el['range-group'].querySelectorAll('.seg-btn')) {
      b.setAttribute('aria-pressed', b.classList.contains('is-on') ? 'true' : 'false');
      if (b.classList.contains('is-on')) state.range = b.dataset.range;
    }
    state.autoRefresh = el['toggle-auto'].checked;
    state.follow = el['toggle-follow'].checked;
    state.lossOverlay = el['toggle-loss'].checked;
    populateFormIntervals();
    createChart();
    setRange(state.range);
    tickClock();
    setInterval(tickClock, 1000);
    setInterval(tick, 250);
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onTheme = () => {
      readThemeColors();
      if (!chart) return;
      chart.options.scales.x.grid.color = chartColors.grid;
      chart.options.scales.y.grid.color = chartColors.grid;
      chart.options.scales.x.border.color = chartColors.border;
      chart.options.scales.y.border.color = chartColors.border;
      chart.options.scales.yLoss.border.color = chartColors.border;
      chart.options.scales.x.ticks.color = chartColors.text;
      chart.options.scales.y.ticks.color = chartColors.text;
      chart.options.scales.yLoss.ticks.color = chartColors.text;
      chart.options.scales.x.title.color = chartColors.text;
      chart.options.scales.y.title.color = chartColors.text;
      chart.options.scales.yLoss.title.color = chartColors.text;
      syncChart(true);
    };
    if (mq && typeof mq.addEventListener === 'function') mq.addEventListener('change', onTheme);
    for (const j of JOBS) runJob(j);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
