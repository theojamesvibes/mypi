/* ─── MyPi Dashboard JS ──────────────────────────────────────────────────── */

'use strict';

let queriesChart = null;
let typeChart = null;
let _drillFilter = {};
let _drillPage = 1;
let _drillHours = 24;
let _drillSince = null;   // ISO string when "today" mode is active; null otherwise
let _drillModal = null;
let _searchPage = 1;
let _searchModal = null;
const _topTableDrillData = {};

// Delegated click handler for drill-row entries in top tables
document.addEventListener('click', e => {
  const tr = e.target.closest('tr.drill-row');
  if (!tr) return;
  const configs = _topTableDrillData[tr.dataset.tbl];
  if (configs) openDrillDown(configs[parseInt(tr.dataset.idx)]);
});

// ─── Utilities ───────────────────────────────────────────────────────────────

function fmtNum(n) {
  if (n === null || n === undefined) return '—';
  return Number(n).toLocaleString();
}

function fmtPct(n) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(1) + '%';
}

function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

function fmtTimeShort(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

const BLOCKED_STATUSES = new Set([
  'GRAVITY', 'REGEX', 'BLACKLIST',
  'EXTERNAL_BLOCKED_IP', 'EXTERNAL_BLOCKED_NULL', 'EXTERNAL_BLOCKED_NXDOMAIN',
  'GRAVITY_CNAME', 'REGEX_CNAME', 'BLACKLIST_CNAME',
]);

function statusPill(status) {
  if (!status) return '<span class="status-pill status-other">—</span>';
  if (BLOCKED_STATUSES.has(status)) return `<span class="status-pill status-blocked">${status}</span>`;
  if (status === 'FORWARDED') return `<span class="status-pill status-forwarded">FORWARDED</span>`;
  if (status === 'CACHE' || status === 'CACHE_STALE') return `<span class="status-pill status-cached">${status}</span>`;
  return `<span class="status-pill status-other">${status}</span>`;
}

function instanceDot(status) {
  const cls = status === 'online' ? 'dot-online' : status === 'offline' ? 'dot-offline' : 'dot-unknown';
  return `<span class="instance-dot ${cls}"></span>`;
}

async function apiFetch(url) {
  const res = await fetch(url, { credentials: 'include', cache: 'no-store' });
  if (res.status === 401) { window.location.href = '/login'; return null; }
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.json();
}

// ─── Dashboard ───────────────────────────────────────────────────────────────

// ─── Time range helpers ───────────────────────────────────────────────────────

function getTimeParams() {
  const val = document.getElementById('time-range')?.value || '24';
  let since = null, hours = null, bucketMinutes = 10;
  if (val === '15m') {
    since = new Date(Date.now() - 15 * 60 * 1000).toISOString();
    bucketMinutes = 1;
  } else if (val === '1h') {
    since = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    bucketMinutes = 5;
  } else if (val === 'today') {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    since = d.toISOString();
    bucketMinutes = 30;
  } else {
    hours = parseInt(val);
    if (hours >= 168) bucketMinutes = 60;
    else if (hours >= 48) bucketMinutes = 30;
    else bucketMinutes = 10;
  }
  return { since, hours, bucketMinutes };
}

function buildTimeQS(extra) {
  const { since, hours, bucketMinutes } = getTimeParams();
  const base = since ? `since=${encodeURIComponent(since)}` : `hours=${hours}`;
  return extra ? `${base}&${extra}` : base;
}

// ─── Dashboard ───────────────────────────────────────────────────────────────

// Master Pi-hole URL — set when instances load; used for Manage Lists footer link.
let _masterUrl = '';

async function loadDashboard() {
  const { since, hours, bucketMinutes } = getTimeParams();
  const tp = since ? `since=${encodeURIComponent(since)}` : `hours=${hours}`;

  try {
    const [summary, history, top] = await Promise.all([
      apiFetch(`/api/stats/summary?${tp}`),
      apiFetch(`/api/stats/history?${tp}&bucket_minutes=${bucketMinutes}`),
      apiFetch(`/api/stats/top?${tp}&limit=10`),
    ]);

    if (!summary) return;

    const instances = summary.instances || [];

    // Stat cards
    document.getElementById('total-queries').textContent = fmtNum(summary.totals.dns_queries_today);
    document.getElementById('queries-blocked').textContent = fmtNum(summary.totals.queries_blocked);
    document.getElementById('percent-blocked').textContent = fmtPct(summary.totals.percent_blocked);
    document.getElementById('blocklist-size').textContent = fmtNum(summary.totals.domains_on_blocklist);

    // Stat card footers
    const clientsFooter = document.getElementById('stat-footer-clients');
    if (clientsFooter) clientsFooter.textContent = `${fmtNum(summary.totals.unique_clients)} unique clients`;

    // Master URL for Manage Lists footer
    const master = instances.find(i => i.is_master);
    if (master && master.url) {
      _masterUrl = master.url;
      const blFooter = document.getElementById('stat-footer-blocklist');
      if (blFooter) blFooter.href = master.url.replace(/\/+$/, '') + '/admin/groups-lists';
    }

    // Blocklist validation — check if all online instances agree
    const onlineInsts = instances.filter(i => i.status === 'online' && i.domains_on_blocklist != null);
    const blocklistValues = onlineInsts.map(i => i.domains_on_blocklist);
    const allAgree = blocklistValues.length === 0 || blocklistValues.every(v => v === blocklistValues[0]);
    const cardBlocklist = document.getElementById('card-blocklist');
    const blWarning = document.getElementById('blocklist-warning');
    if (!allAgree) {
      cardBlocklist.classList.remove('stat-card-green');
      cardBlocklist.classList.add('stat-card-red');
      if (blWarning) blWarning.classList.remove('d-none');
    } else {
      cardBlocklist.classList.remove('stat-card-red');
      cardBlocklist.classList.add('stat-card-green');
      if (blWarning) blWarning.classList.add('d-none');
    }

    _drillHours = hours || 24;
    _drillSince = since || null;

    // Online count badge
    updateStatusBadge(instances);
    const lu = document.getElementById('last-updated');
    if (lu) lu.textContent = 'Updated ' + new Date().toLocaleTimeString();

    // Queries over time chart
    renderQueriesChart(history.buckets);

    // Query type chart (from summary instances)
    renderTypeChart(summary.totals);

    // Per-instance table — uses time-windowed counts from summary
    renderInstancesTable(instances);

    // Top tables — blocked and clients are drillable
    renderTopTable('top-permitted', top.top_permitted, r => r.domain, r => fmtNum(r.count));
    renderTopTable('top-blocked', top.top_blocked, r => r.domain, r => fmtNum(r.count),
      r => ({ label: `Blocked: ${r.domain}`, domain: r.domain, blocked: true }));
    renderTopTable('top-clients', top.top_clients, r => r.client, r => fmtNum(r.count),
      r => ({ label: `Client queries: ${r.client}`, client: r.client, blocked: true }));

  } catch (err) {
    console.error('Dashboard load error:', err);
  }
}

function renderQueriesChart(buckets) {
  const labels = buckets.map(b => fmtTimeShort(b.timestamp));
  const queries = buckets.map(b => b.queries);
  const blocked = buckets.map(b => b.blocked);

  const ctx = document.getElementById('queriesChart');
  if (!ctx) return;

  if (queriesChart) queriesChart.destroy();
  queriesChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Total Queries',
          data: queries,
          backgroundColor: 'rgba(60,141,188,0.5)',
          borderColor: 'rgba(60,141,188,0.8)',
          borderWidth: 1,
        },
        {
          label: 'Blocked',
          data: blocked,
          backgroundColor: 'rgba(221,75,57,0.5)',
          borderColor: 'rgba(221,75,57,0.8)',
          borderWidth: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: getChartColors().tipBg,
          titleColor: getChartColors().tipText,
          bodyColor: getChartColors().tipText,
          borderColor: getChartColors().grid,
          borderWidth: 1,
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 12, font: { size: 10 }, color: getChartColors().tick } },
        y: { beginAtZero: true, grid: { color: getChartColors().grid }, ticks: { font: { size: 10 }, color: getChartColors().tick } },
      },
    },
  });

  // Custom legend
  const legend = document.getElementById('chart-legend');
  if (legend) {
    legend.innerHTML = `
      <span><span style="display:inline-block;width:12px;height:12px;background:rgba(60,141,188,0.8);border-radius:2px;margin-right:4px;"></span>Queries</span>
      <span><span style="display:inline-block;width:12px;height:12px;background:rgba(221,75,57,0.8);border-radius:2px;margin-right:4px;"></span>Blocked</span>
    `;
  }
}

function renderTypeChart(totals) {
  const ctx = document.getElementById('typeChart');
  if (!ctx) return;

  const forwarded = totals.queries_forwarded || 0;
  const cached = totals.queries_cached || 0;
  const blocked = totals.queries_blocked || 0;
  const other = Math.max(0, (totals.dns_queries_today || 0) - forwarded - cached - blocked);

  if (typeChart) typeChart.destroy();
  typeChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Forwarded', 'Cached', 'Blocked', 'Other'],
      datasets: [{
        data: [forwarded, cached, blocked, other],
        backgroundColor: ['#3c8dbc', '#00c0ef', '#dd4b39', '#aaa'],
        borderColor: getChartColors().border,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { font: { size: 11 }, padding: 10, color: getChartColors().tick },
        },
        tooltip: {
          backgroundColor: getChartColors().tipBg,
          titleColor: getChartColors().tipText,
          bodyColor: getChartColors().tipText,
        },
      },
      cutout: '65%',
    },
  });
}

function renderInstancesTable(instances) {
  const tbody = document.getElementById('instances-tbody');
  if (!tbody) return;

  if (!instances.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">No instances configured.</td></tr>';
    return;
  }

  // Master first, then alphabetical by name.
  const sorted = [...instances].sort((a, b) => {
    if (a.is_master !== b.is_master) return a.is_master ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  tbody.innerHTML = sorted.map(inst => `
    <tr>
      <td>
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${escHtml(inst.color)};margin-right:6px;"></span>
        <strong>${escHtml(inst.name)}</strong>
        ${inst.is_master ? '<span class="badge bg-primary ms-1" style="font-size:0.65rem;">master</span>' : ''}
      </td>
      <td>${instanceDot(inst.status)}${escHtml(inst.status)}</td>
      <td class="text-end">${fmtNum(inst.dns_queries_today)}</td>
      <td class="text-end">${fmtNum(inst.queries_blocked)}</td>
      <td class="text-end">${fmtPct(inst.percent_blocked)}</td>
      <td class="text-end">${fmtNum(inst.domains_on_blocklist)}</td>
      <td class="text-end">${fmtNum(inst.unique_clients)}</td>
      <td class="text-muted small">${inst.last_seen_at ? fmtTime(inst.last_seen_at) : '—'}</td>
    </tr>
  `).join('');
}

function renderTopTable(tbodyId, rows, labelFn, countFn, drillFn) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="2" class="text-center text-muted py-2 small">No data yet</td></tr>';
    return;
  }
  if (drillFn) _topTableDrillData[tbodyId] = rows.map(r => drillFn(r));
  tbody.innerHTML = rows.map((r, i) => `
    <tr ${drillFn ? `class="drill-row" data-tbl="${tbodyId}" data-idx="${i}"` : ''}>
      <td class="text-truncate" style="max-width:200px;" title="${escHtml(labelFn(r))}">${escHtml(labelFn(r))}</td>
      <td class="text-end">${countFn(r)}</td>
    </tr>
  `).join('');
}

// ─── Query Log ───────────────────────────────────────────────────────────────

let currentPage = 1;
let _sortBy = 'timestamp';
let _sortDir = 'desc';
let _liveInterval = null;

async function loadInstanceFilter() {
  const sel = document.getElementById('f-instance');
  if (!sel) return;
  const instances = await apiFetch('/api/instances');
  if (!instances) return;
  instances.forEach(i => {
    const opt = document.createElement('option');
    opt.value = i.id;
    opt.textContent = i.name;
    sel.appendChild(opt);
  });
  updateStatusBadge(instances);
}

function updateStatusBadge(instances) {
  if (!instances) return;
  const online = instances.filter(i => i.status === 'online').length;
  const badge = document.getElementById('online-count');
  if (badge) {
    badge.textContent = `${online}/${instances.length} online`;
    badge.className = online === instances.length ? 'badge bg-success' : 'badge bg-warning text-dark';
  }
}

async function loadQueries(page) {
  currentPage = page || 1;
  const instance = document.getElementById('f-instance')?.value || '';
  const domain = document.getElementById('f-domain')?.value || '';
  const client = document.getElementById('f-client')?.value || '';
  const blocked = document.getElementById('f-blocked')?.value || '';
  const hours = document.getElementById('f-hours')?.value || 24;

  // Toggle table header based on view mode
  const theadDefault = document.getElementById('queries-thead-default');
  const theadClients = document.getElementById('queries-thead-clients');

  if (blocked === 'clients') {
    if (theadDefault) theadDefault.classList.add('d-none');
    if (theadClients) theadClients.classList.remove('d-none');
    await loadClientSummary(hours, instance);
    return;
  }

  if (theadDefault) theadDefault.classList.remove('d-none');
  if (theadClients) theadClients.classList.add('d-none');

  const params = new URLSearchParams({
    page: currentPage, page_size: 100, hours,
    sort_by: _sortBy, sort_dir: _sortDir,
  });
  if (instance) params.set('instance_id', instance);
  if (domain) params.set('domain', domain);
  if (client) params.set('client', client);
  if (blocked !== '') params.set('blocked', blocked);

  try {
    const data = await apiFetch(`/api/queries?${params}`);
    if (!data) return;

    const tbody = document.getElementById('queries-tbody');
    if (!tbody) return;

    document.getElementById('query-count').textContent =
      `${fmtNum(data.total)} results — page ${data.page} of ${Math.max(1, Math.ceil(data.total / data.page_size))}`;

    tbody.innerHTML = data.items.length
      ? data.items.map(q => {
          const d = escHtml(q.domain || '');
          const isBlocked = BLOCKED_STATUSES.has(q.status);
          return `<tr>
            <td class="text-nowrap small">${fmtTime(q.timestamp)}</td>
            <td><span class="badge rounded-pill" style="background:#6c757d;font-weight:500;">${escHtml(q.instance_name)}</span></td>
            <td class="text-truncate" style="max-width:220px;" title="${d}">${d || '—'}</td>
            <td><code class="small">${escHtml(q.query_type || '—')}</code></td>
            <td class="small">${escHtml(q.client_name || q.client_ip || '—')}</td>
            <td>${statusPill(q.status)}</td>
            <td class="text-end small">${q.reply_time_ms != null ? Number(q.reply_time_ms).toFixed(1) : '—'}</td>
            <td class="text-end domain-action-cell" data-domain="${d}" data-blocked="${isBlocked}"></td>
          </tr>`;
        }).join('')
      : '<tr><td colspan="8" class="text-center text-muted py-4">No queries found.</td></tr>';

    // Populate domain action buttons via DOM (never via innerHTML) to prevent XSS.
    tbody.querySelectorAll('.domain-action-cell').forEach(td => {
      td.appendChild(_mkDomainBtn(td.dataset.domain, td.dataset.blocked === 'true'));
    });

    const totalPages = Math.ceil(data.total / data.page_size);
    renderPagination('pagination-top', currentPage, totalPages);
    renderPagination('pagination-bottom', currentPage, totalPages);

    const lu = document.getElementById('last-updated');
    if (lu) lu.textContent = 'Updated ' + new Date().toLocaleTimeString();

  } catch (err) {
    console.error('Query log error:', err);
  }
}

async function loadClientSummary(hours, instanceId) {
  const params = new URLSearchParams({ hours });
  if (instanceId) params.set('instance_id', instanceId);

  try {
    const data = await apiFetch(`/api/queries/clients?${params}`);
    if (!data) return;

    const tbody = document.getElementById('queries-tbody');
    if (!tbody) return;

    document.getElementById('query-count').textContent = `${fmtNum(data.length)} unique clients`;

    tbody.innerHTML = data.length
      ? data.map(c => {
          const pct = c.total_queries > 0
            ? ((c.blocked_queries / c.total_queries) * 100).toFixed(1) + '%'
            : '0.0%';
          return `<tr>
            <td class="small">${escHtml(c.client_name || c.client_ip || '—')}</td>
            <td class="text-end small">${fmtNum(c.total_queries)}</td>
            <td class="text-end small">${fmtNum(c.blocked_queries)}</td>
            <td class="text-end small">${pct}</td>
            <td class="small text-muted">${c.last_seen ? fmtTime(c.last_seen) : '—'}</td>
          </tr>`;
        }).join('')
      : '<tr><td colspan="5" class="text-center text-muted py-4">No clients found.</td></tr>';

    // Clear pagination — client summary is not paginated
    renderPagination('pagination-top', 1, 1);
    renderPagination('pagination-bottom', 1, 1);

  } catch (err) {
    console.error('Client summary error:', err);
  }
}

function setSort(col) {
  if (_sortBy === col) {
    _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
  } else {
    _sortBy = col;
    _sortDir = col === 'timestamp' ? 'desc' : 'asc';
  }
  // Update header icons
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById(`sort-${col}`);
  if (icon) icon.textContent = _sortDir === 'desc' ? '↓' : '↑';
  loadQueries(1);
}

function toggleLiveView(on) {
  const icon = document.getElementById('live-icon');
  const refreshBtn = document.getElementById('refresh-btn');
  if (on) {
    if (icon) icon.style.display = '';
    if (refreshBtn) refreshBtn.disabled = true;
    _sortBy = 'timestamp';
    _sortDir = 'desc';
    document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
    const ts = document.getElementById('sort-timestamp');
    if (ts) ts.textContent = '↓';
    loadQueries(1);
    _liveInterval = setInterval(() => loadQueries(1), 2000);
  } else {
    clearInterval(_liveInterval);
    _liveInterval = null;
    if (icon) icon.style.display = 'none';
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

// Wire up sortable column headers after DOM ready
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.sort-col').forEach(th => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => setSort(th.dataset.col));
  });
});

function renderPagination(id, current, total) {
  const el = document.getElementById(id);
  if (!el) return;
  if (total <= 1) { el.innerHTML = ''; return; }

  let pages = [];
  if (total <= 7) {
    pages = Array.from({ length: total }, (_, i) => i + 1);
  } else {
    pages = [1];
    const start = Math.max(2, current - 2);
    const end = Math.min(total - 1, current + 2);
    if (start > 2) pages.push('…');
    for (let p = start; p <= end; p++) pages.push(p);
    if (end < total - 1) pages.push('…');
    pages.push(total);
  }

  el.innerHTML = pages.map(p => {
    if (p === '…') return `<span class="btn btn-sm btn-outline-secondary disabled">…</span>`;
    const active = p === current ? 'btn-secondary' : 'btn-outline-secondary';
    return `<button class="btn btn-sm ${active}" onclick="loadQueries(${p})">${p}</button>`;
  }).join('');
}

// ─── Domain block / unblock ──────────────────────────────────────────────────

// Build the initial Block / Unblock button for a query row.
// domain must be an already-HTML-escaped string (safe for inline onclick).
// Returns a DOM element — never an HTML string — so domain values cannot inject
// event-handler code regardless of what characters Pi-hole returns.
function _mkDomainBtn(domain, isCurrentlyBlocked) {
  const btn = document.createElement('button');
  btn.className = `btn btn-xs ${isCurrentlyBlocked ? 'btn-outline-success' : 'btn-outline-danger'} py-0 px-1`;
  btn.style.cssText = 'font-size:.7rem;white-space:nowrap;';
  btn.textContent = isCurrentlyBlocked ? 'Unblock' : 'Block';
  btn.addEventListener('click', () => askDomainAction(btn, domain, isCurrentlyBlocked));
  return btn;
}

function _cellSpinner(cell) {
  cell.replaceChildren();
  const s = document.createElement('span');
  s.className = 'text-muted';
  s.style.fontSize = '.7rem';
  s.textContent = '…';
  cell.appendChild(s);
}

// First click: check actual deny-list state on the master Pi-hole, then show
// the correct "Verb? ✓ ✗" prompt.  The `blocking` hint from the row is only a
// fallback used if the API call fails.
async function askDomainAction(btn, domain, blocking) {
  const cell = btn.closest('td');
  _cellSpinner(cell);

  // Query the master Pi-hole for the real current state.
  let actuallyBlocked = blocking;
  try {
    const resp = await fetch(`/api/domains/block/${encodeURIComponent(domain)}`);
    if (resp.ok) actuallyBlocked = (await resp.json()).blocked;
  } catch (_) { /* keep fallback */ }

  const doBlocking = !actuallyBlocked;

  const label = document.createElement('span');
  label.className = `${doBlocking ? 'text-danger' : 'text-success'} me-1`;
  label.style.cssText = 'font-size:.7rem;white-space:nowrap;';
  label.textContent = doBlocking ? 'Block?' : 'Unblock?';

  const confirmBtn = document.createElement('button');
  confirmBtn.className = `btn btn-xs ${doBlocking ? 'btn-outline-danger' : 'btn-outline-success'} py-0 px-1 me-1`;
  confirmBtn.style.fontSize = '.7rem';
  confirmBtn.title = 'Confirm';
  confirmBtn.textContent = '✓';
  confirmBtn.addEventListener('click', () => doDomainAction(confirmBtn, domain, doBlocking));

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn btn-xs btn-outline-secondary py-0 px-1';
  cancelBtn.style.fontSize = '.7rem';
  cancelBtn.title = 'Cancel';
  cancelBtn.textContent = '✗';
  cancelBtn.addEventListener('click', () => cell.replaceChildren(_mkDomainBtn(domain, actuallyBlocked)));

  cell.replaceChildren(label, confirmBtn, cancelBtn);
}

// Second click (confirm): execute the API call.
async function doDomainAction(btn, domain, blocking) {
  const cell = btn.closest('td');
  _cellSpinner(cell);
  try {
    const resp = blocking
      ? await fetch('/api/domains/block', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ domain }),
        })
      : await fetch(`/api/domains/block/${encodeURIComponent(domain)}`, { method: 'DELETE' });

    if (resp.ok || resp.status === 204) {
      cell.replaceChildren(_mkDomainBtn(domain, !blocking));
    } else {
      const err = await resp.json().catch(() => ({}));
      alert(`Failed to ${blocking ? 'block' : 'unblock'} ${domain}: ${err.detail || resp.status}`);
      cell.replaceChildren(_mkDomainBtn(domain, blocking));
    }
  } catch (e) {
    alert(`Error: ${e}`);
    cell.replaceChildren(_mkDomainBtn(domain, blocking));
  }
}

// ─── Settings ────────────────────────────────────────────────────────────────

async function loadApiKeys() {
  const tbody = document.getElementById('api-keys-tbody');
  if (!tbody) return;
  const keys = await apiFetch('/api/auth/api-keys');
  if (!keys) return;
  if (!keys.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center small py-2">No API keys yet</td></tr>';
    return;
  }
  tbody.innerHTML = keys.map(k => `
    <tr>
      <td>${escHtml(k.name)}</td>
      <td class="small text-muted">${fmtTime(k.created_at)}</td>
      <td class="small text-muted">${k.last_used_at ? fmtTime(k.last_used_at) : 'Never'}</td>
      <td><button class="btn btn-sm btn-outline-danger py-0" onclick="revokeKey('${k.id}')">Revoke</button></td>
    </tr>
  `).join('');
}

async function createApiKey(event) {
  event.preventDefault();
  const name = document.getElementById('key-name').value.trim();
  if (!name) return;
  const res = await fetch('/api/auth/api-key', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ name }),
  });
  if (res.status === 401) { window.location.href = '/login'; return; }
  const data = await res.json();
  document.getElementById('new-key-value').textContent = data.raw_key;
  document.getElementById('new-key-alert').classList.remove('d-none');
  document.getElementById('key-name').value = '';
  loadApiKeys();
}

async function revokeKey(id) {
  if (!confirm('Revoke this API key?')) return;
  await fetch(`/api/auth/api-key/${id}`, { method: 'DELETE', credentials: 'include' });
  document.getElementById('new-key-alert').classList.add('d-none');
  loadApiKeys();
}

async function loadSettingsInstances() {
  const tbody = document.getElementById('settings-instances-tbody');
  if (!tbody) return;
  const instances = await apiFetch('/api/instances');
  if (!instances) return;
  updateStatusBadge(instances);

  const _versionRepos = {
    core: 'pi-hole/pi-hole',
    ftl:  'pi-hole/FTL',
    web:  'pi-hole/web',
  };

  function versionCell(version, updateAvailable, type) {
    if (!version) return '<td class="small text-muted">—</td>';
    const repo = _versionRepos[type] || 'pi-hole/pi-hole';
    const url = `https://github.com/${repo}/releases/tag/v${encodeURIComponent(version)}`;
    if (updateAvailable === true)
      return `<td><a href="${url}" target="_blank" class="badge bg-danger text-decoration-none" title="Update available">${escHtml(version)}</a></td>`;
    if (updateAvailable === false)
      return `<td><a href="${url}" target="_blank" class="badge bg-success text-decoration-none">${escHtml(version)}</a></td>`;
    // update_available is null — no remote data yet
    return `<td><a href="${url}" target="_blank" class="badge bg-secondary text-decoration-none">${escHtml(version)}</a></td>`;
  }

  function fmtLastSeen(iso) {
    if (!iso) return '<td class="small text-muted">—</td>';
    const d = new Date(iso);
    const diffMins = Math.floor((Date.now() - d) / 60000);
    let label;
    if (diffMins < 1) label = 'just now';
    else if (diffMins < 60) label = `${diffMins}m ago`;
    else if (diffMins < 1440) label = `${Math.floor(diffMins / 60)}h ago`;
    else label = d.toLocaleDateString();
    return `<td class="small text-muted" title="${d.toLocaleString()}">${label}</td>`;
  }

  tbody.innerHTML = instances.map(i => `
    <tr>
      <td class="ps-3">
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${i.color};margin-right:6px;"></span>
        ${escHtml(i.name)}
        ${i.is_master ? '<span class="badge bg-primary ms-1" style="font-size:0.65rem;">master</span>' : ''}
      </td>
      <td class="small"><a href="${escHtml(i.url.replace(/\/+$/, '') + '/admin')}" target="_blank" class="text-muted">${escHtml(i.url)}</a></td>
      <td>${instanceDot(i.status)}${i.status}</td>
      ${fmtLastSeen(i.last_seen_at)}
      ${versionCell(i.version_core, i.update_available_core, 'core')}
      ${versionCell(i.version_ftl, i.update_available_ftl, 'ftl')}
      ${versionCell(i.version_web, i.update_available_web, 'web')}
    </tr>
  `).join('');

  // Version check health notices
  const noticeDiv = document.getElementById('instances-version-notice');
  if (noticeDiv) {
    const [mypi, pihole] = await Promise.all([
      apiFetch('/api/version/status'),
      apiFetch('/api/version/pihole-status'),
    ]);
    const notices = [];
    if (mypi?.enabled && mypi?.checked_at && !mypi?.latest_version)
      notices.push('<i class="bi bi-exclamation-triangle text-warning me-1"></i>MyPi version check failed');
    if (pihole?.enabled && pihole?.checked_at && !pihole?.latest_core)
      notices.push('<i class="bi bi-exclamation-triangle text-warning me-1"></i>Pi-hole version check failed');
    noticeDiv.innerHTML = notices.length
      ? `<span class="text-muted">${notices.join('<span class="mx-2">·</span>')}</span>`
      : '';
  }
}

// ─── Stale (orphaned) instances ──────────────────────────────────────────────

async function loadStaleInstances() {
  const card = document.getElementById('stale-instances-card');
  const tbody = document.getElementById('stale-instances-tbody');
  const badge = document.getElementById('stale-count-badge');
  if (!card || !tbody) return;

  const instances = await apiFetch('/api/instances/stale');
  if (!instances || instances.length === 0) {
    card.classList.add('d-none');
    return;
  }

  card.classList.remove('d-none');
  if (badge) badge.textContent = instances.length;

  tbody.innerHTML = instances.map(i => `
    <tr>
      <td>${escHtml(i.name)}</td>
      <td class="small text-muted">${escHtml(i.url)}</td>
      <td class="small text-muted">${i.last_seen_at ? new Date(i.last_seen_at).toLocaleString() : '—'}</td>
      <td>
        <button class="btn btn-xs btn-outline-danger py-0 px-1" style="font-size:0.75rem;"
                onclick="deleteStaleInstance('${i.id}', '${escHtml(i.name)}')">
          <i class="bi bi-trash"></i> Remove
        </button>
      </td>
    </tr>
  `).join('');
}

async function deleteStaleInstance(id, name) {
  if (!confirm(`Remove orphaned instance "${name}" and all its historical data? This cannot be undone.`)) return;
  try {
    const res = await fetch(`/api/instances/${id}`, { method: 'DELETE', credentials: 'include' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(`Failed to remove instance: ${data.detail || res.status}`);
      return;
    }
    await loadStaleInstances();
  } catch (err) {
    alert(`Error: ${err.message}`);
  }
}

async function deleteAllStale() {
  const instances = await apiFetch('/api/instances/stale');
  if (!instances || instances.length === 0) return;
  if (!confirm(`Remove all ${instances.length} orphaned instance(s) and their historical data? This cannot be undone.`)) return;
  for (const i of instances) {
    await fetch(`/api/instances/${i.id}`, { method: 'DELETE', credentials: 'include' });
  }
  await loadStaleInstances();
}

// ─── Drill-down modal ─────────────────────────────────────────────────────────

function openDrillDown(filter) {
  _drillFilter = filter || {};
  _drillPage = 1;
  document.getElementById('drillModalLabel').textContent = _drillFilter.label || 'Query Detail';

  const viewAll = document.getElementById('drill-view-all');
  if (viewAll) {
    const qs = new URLSearchParams();
    if (_drillFilter.domain) qs.set('domain', _drillFilter.domain);
    if (_drillFilter.client) qs.set('client', _drillFilter.client);
    if (_drillFilter.blocked) qs.set('blocked', 'true');
    viewAll.href = `/queries${qs.size ? '?' + qs : ''}`;
  }

  if (!_drillModal) {
    _drillModal = new bootstrap.Modal(document.getElementById('drillModal'));
  }
  _drillModal.show();
  loadDrillPage(1);
}

async function loadDrillPage(page) {
  _drillPage = page;
  const params = new URLSearchParams({ page, page_size: 50 });
  if (_drillSince) params.set('since', _drillSince);
  else params.set('hours', _drillHours);
  if (_drillFilter.blocked) params.set('blocked', 'true');
  if (_drillFilter.domain) params.set('domain', _drillFilter.domain);
  if (_drillFilter.client) params.set('client', _drillFilter.client);

  document.getElementById('drill-count').textContent = 'Loading…';
  document.getElementById('drill-tbody').innerHTML =
    '<tr><td colspan="7" class="text-center text-muted py-3">Loading…</td></tr>';

  try {
    const data = await apiFetch(`/api/queries?${params}`);
    if (!data) return;

    const totalPages = Math.ceil(data.total / data.page_size);
    document.getElementById('drill-count').textContent =
      `${fmtNum(data.total)} results — page ${page} of ${totalPages}`;

    document.getElementById('drill-tbody').innerHTML = data.items.length
      ? data.items.map(q => `
          <tr>
            <td class="text-nowrap small">${fmtTime(q.timestamp)}</td>
            <td><span class="badge rounded-pill" style="background:#6c757d;font-weight:500;">${escHtml(q.instance_name)}</span></td>
            <td class="text-truncate" style="max-width:200px;" title="${escHtml(q.domain || '')}">${escHtml(q.domain || '—')}</td>
            <td><code class="small">${escHtml(q.query_type || '—')}</code></td>
            <td class="small">${escHtml(q.client_name || q.client_ip || '—')}</td>
            <td>${statusPill(q.status)}</td>
            <td class="text-end small">${q.reply_time_ms != null ? Number(q.reply_time_ms).toFixed(1) : '—'}</td>
          </tr>
        `).join('')
      : '<tr><td colspan="7" class="text-center text-muted py-3">No results.</td></tr>';

    renderDrillPagination(page, totalPages);
  } catch (err) {
    console.error('Drill-down error:', err);
    document.getElementById('drill-count').textContent = 'Error loading data';
  }
}

function renderDrillPagination(current, total) {
  const el = document.getElementById('drill-pagination');
  if (!el) return;
  if (total <= 1) { el.innerHTML = ''; return; }

  let pages = [];
  if (total <= 7) {
    pages = Array.from({ length: total }, (_, i) => i + 1);
  } else {
    pages = [1];
    const start = Math.max(2, current - 2);
    const end = Math.min(total - 1, current + 2);
    if (start > 2) pages.push('…');
    for (let p = start; p <= end; p++) pages.push(p);
    if (end < total - 1) pages.push('…');
    pages.push(total);
  }

  el.innerHTML = pages.map(p => {
    if (p === '…') return `<span class="btn btn-sm btn-outline-secondary disabled">…</span>`;
    const active = p === current ? 'btn-secondary' : 'btn-outline-secondary';
    return `<button class="btn btn-sm ${active}" onclick="loadDrillPage(${p})">${p}</button>`;
  }).join('');
}

// ─── Global search ────────────────────────────────────────────────────────────

function openSearch() {
  if (!_searchModal) {
    _searchModal = new bootstrap.Modal(document.getElementById('searchModal'));
  }
  _searchModal.show();
  // Focus the input after show
  document.getElementById('searchModal').addEventListener('shown.bs.modal', () => {
    document.getElementById('s-domain').focus();
  }, { once: true });
}

async function runSearch(page) {
  _searchPage = page || 1;
  const domain = document.getElementById('s-domain')?.value.trim() || '';
  const client = document.getElementById('s-client')?.value.trim() || '';
  const filter = document.getElementById('s-filter')?.value || '';
  const hours = document.getElementById('s-hours')?.value || 24;

  const params = new URLSearchParams({ page: _searchPage, page_size: 50, hours });
  if (domain) params.set('domain', domain);
  if (client) params.set('client', client);
  if (filter === 'blocked') params.set('blocked', 'true');
  if (filter === 'permitted') params.set('blocked', 'false');

  document.getElementById('s-count').textContent = 'Searching…';
  document.getElementById('s-tbody').innerHTML =
    '<tr><td colspan="7" class="text-center text-muted py-3">Searching…</td></tr>';

  try {
    const data = await apiFetch(`/api/queries?${params}`);
    if (!data) return;

    const totalPages = Math.ceil(data.total / data.page_size);
    document.getElementById('s-count').textContent =
      `${fmtNum(data.total)} results — page ${_searchPage} of ${Math.max(1, totalPages)}`;

    document.getElementById('s-tbody').innerHTML = data.items.length
      ? data.items.map(q => `
          <tr>
            <td class="text-nowrap small">${fmtTime(q.timestamp)}</td>
            <td><span class="badge rounded-pill" style="background:#6c757d;font-weight:500;">${escHtml(q.instance_name)}</span></td>
            <td class="text-truncate" style="max-width:180px;" title="${escHtml(q.domain || '')}">${escHtml(q.domain || '—')}</td>
            <td><code class="small">${escHtml(q.query_type || '—')}</code></td>
            <td class="small">${escHtml(q.client_name || q.client_ip || '—')}</td>
            <td>${statusPill(q.status)}</td>
            <td class="text-end small">${q.reply_time_ms != null ? Number(q.reply_time_ms).toFixed(1) : '—'}</td>
          </tr>
        `).join('')
      : '<tr><td colspan="7" class="text-center text-muted py-3">No results found.</td></tr>';

    renderSearchPagination(totalPages);
  } catch (err) {
    console.error('Search error:', err);
    document.getElementById('s-count').textContent = 'Error — see console';
  }
}

function renderSearchPagination(total) {
  const el = document.getElementById('s-pagination');
  if (!el) return;
  if (total <= 1) { el.innerHTML = ''; return; }

  const current = _searchPage;
  let pages = [];
  if (total <= 7) {
    pages = Array.from({ length: total }, (_, i) => i + 1);
  } else {
    pages = [1];
    const start = Math.max(2, current - 2);
    const end = Math.min(total - 1, current + 2);
    if (start > 2) pages.push('…');
    for (let p = start; p <= end; p++) pages.push(p);
    if (end < total - 1) pages.push('…');
    pages.push(total);
  }

  el.innerHTML = pages.map(p => {
    if (p === '…') return `<span class="btn btn-sm btn-outline-secondary disabled">…</span>`;
    const active = p === current ? 'btn-secondary' : 'btn-outline-secondary';
    return `<button class="btn btn-sm ${active}" onclick="runSearch(${p})">${p}</button>`;
  }).join('');
}

// ─── Dashboard sync indicator ────────────────────────────────────────────────

async function loadSyncIndicator() {
  let status = null;
  try {
    const res = await fetch('/api/sync/status', { credentials: 'include', cache: 'no-store' });
    if (res.ok) status = await res.json();
  } catch (_) { /* network error — leave badge hidden */ }

  renderSyncBadge(status);

  // Also update the dashboard inline element if present
  const el = document.getElementById('sync-last-synced');
  if (!el) return;

  if (!status || !status.completed_at) {
    el.innerHTML = '<i class="bi bi-arrow-repeat me-1 text-secondary"></i>Pi sync: <span class="text-muted">never run</span>';
    return;
  }

  const completedAt = new Date(status.completed_at);
  const ageHours = (Date.now() - completedAt.getTime()) / 3600000;
  const isStale = ageHours > 24;
  const timeStr = completedAt.toLocaleString();
  const timeCls = isStale ? 'text-danger fw-semibold' : '';
  const icon = status.status === 'error'
    ? '<i class="bi bi-x-circle text-danger me-1"></i>'
    : '<i class="bi bi-arrow-repeat me-1 text-success"></i>';
  const staleIcon = isStale
    ? ' <i class="bi bi-exclamation-triangle-fill text-danger ms-1" title="Last sync was over 24 hours ago"></i>'
    : '';
  el.innerHTML = `${icon}Pi synced: <span class="${timeCls}">${timeStr}</span>${staleIcon}`;
}

function renderSyncBadge(status) {
  const badge = document.getElementById('sync-status-badge');
  if (!badge) return;

  if (!status || !status.completed_at) {
    badge.style.setProperty('display', 'none', 'important');
    return;
  }

  const completedAt = new Date(status.completed_at);
  const ageHours = (Date.now() - completedAt.getTime()) / 3600000;
  const results = status.results || [];

  let label, cls;

  if (status.status === 'running') {
    label = '↻ Syncing…';
    cls = 'bg-warning text-dark';
  } else if (!results.length) {
    // No per-replica results (error before sync started)
    label = '⚠ Sync failed';
    cls = 'bg-danger';
  } else {
    const ok = results.filter(r => r.status === 'success').length;
    const total = results.length;
    const timeStr = completedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (ok === total) {
      label = `✓ Synced ${timeStr}`;
      cls = ageHours > 24 ? 'bg-danger' : 'bg-success';
    } else if (ok === 0) {
      label = `✗ Sync failed ${timeStr}`;
      cls = 'bg-danger';
    } else {
      // Include master in the count — if per-replica results exist, the master
      // export succeeded (a master failure produces a global error, not results).
      const masterCount = data.master ? 1 : 0;
      label = `⚠ Synced ${ok + masterCount}/${total + masterCount} ${timeStr}`;
      cls = 'bg-warning text-dark';
    }
  }

  badge.className = `badge ${cls}`;
  badge.textContent = label;
  badge.style.removeProperty('display');
}

// ─── Sync ────────────────────────────────────────────────────────────────────

let _syncPollInterval = null;

async function loadSyncStatus() {
  const data = await apiFetch('/api/sync/status');
  if (!data) return;
  renderSyncStatus(data);
}

async function loadSyncSchedule() {
  const data = await apiFetch('/api/sync/schedule');
  if (!data) return;
  const interval = document.getElementById('sync-interval');
  if (interval) interval.value = String(data.interval_minutes);
  const autoG = document.getElementById('sync-auto-gravity');
  if (autoG) autoG.checked = data.auto_gravity;
  const cfg = document.getElementById('sync-config');
  if (cfg) cfg.checked = data.import_config;
  const grav = document.getElementById('sync-gravity');
  if (grav) grav.checked = data.import_gravity;
  const dhcp = document.getElementById('sync-dhcp');
  if (dhcp) dhcp.checked = data.import_dhcp_leases;
}

async function saveSchedule() {
  const body = {
    interval_minutes: parseInt(document.getElementById('sync-interval')?.value || '0'),
    auto_gravity: document.getElementById('sync-auto-gravity')?.checked ?? false,
    import_config: document.getElementById('sync-config')?.checked ?? true,
    import_gravity: document.getElementById('sync-gravity')?.checked ?? true,
    import_dhcp_leases: document.getElementById('sync-dhcp')?.checked ?? false,
    run_gravity: true,
  };
  const res = await fetch('/api/sync/schedule', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (res.status === 401) { window.location.href = '/login'; return; }
  const btn = document.querySelector('[onclick="saveSchedule()"]');
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<i class="bi bi-x me-1"></i>Save failed';
      btn.classList.replace('btn-outline-secondary', 'btn-danger');
      setTimeout(() => { btn.innerHTML = orig; btn.classList.replace('btn-danger', 'btn-outline-secondary'); }, 4000);
    }
    alert(`Schedule save failed: ${err.detail || res.statusText}`);
    return;
  }
  if (btn) {
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-check me-1"></i>Saved';
    btn.classList.replace('btn-outline-secondary', 'btn-success');
    setTimeout(() => { btn.innerHTML = orig; btn.classList.replace('btn-success', 'btn-outline-secondary'); }, 2000);
  }
}

function renderSyncStatus(data) {
  const badge = document.getElementById('sync-badge');
  const result = document.getElementById('sync-result');
  const btn = document.getElementById('sync-btn');
  if (!badge || !result) return;

  const colours = { idle: 'secondary', running: 'warning', success: 'success', error: 'danger' };
  badge.className = `badge bg-${colours[data.status] || 'secondary'}`;
  badge.textContent = data.status;

  if (btn) btn.disabled = data.status === 'running';

  if (data.status === 'idle') {
    result.innerHTML = '<span class="text-muted">No sync has been run yet.</span>';
    return;
  }

  const started = data.started_at ? fmtTime(data.started_at) : '—';
  const finished = data.completed_at ? fmtTime(data.completed_at) : '—';

  if (data.status === 'running') {
    result.innerHTML = `<span class="text-warning"><i class="bi bi-arrow-repeat spin me-1"></i>Running… (started ${started})</span>`;
    return;
  }

  if (data.error) {
    result.innerHTML = `<div class="text-danger"><i class="bi bi-x-circle me-1"></i>${escHtml(data.error)}</div>
      <div class="text-muted mt-1">Started: ${started}</div>`;
    return;
  }

  const masterRow = data.master
    ? `<div><i class="bi bi-check-circle text-success me-1"></i><strong>${escHtml(data.master)}</strong><span class="badge bg-primary ms-1" style="font-size:0.65rem;">master</span></div>`
    : '';

  const rows = (data.results || []).map(r => {
    const icon = r.status === 'success'
      ? '<i class="bi bi-check-circle text-success me-1"></i>'
      : '<i class="bi bi-x-circle text-danger me-1"></i>';
    const err = r.error ? ` — <span class="text-danger">${escHtml(r.error)}</span>` : '';
    return `<div>${icon}<strong>${escHtml(r.name)}</strong>${err}</div>`;
  }).join('');

  result.innerHTML = `${masterRow}${rows}<div class="text-muted mt-1">Completed: ${finished}</div>`;
}

async function triggerSync() {
  const btn = document.getElementById('sync-btn');
  if (btn) btn.disabled = true;

  const body = {
    import_config: document.getElementById('sync-config')?.checked ?? true,
    import_gravity: document.getElementById('sync-gravity')?.checked ?? true,
    import_dhcp_leases: document.getElementById('sync-dhcp')?.checked ?? false,
    run_gravity: true,
  };

  try {
    const res = await fetch('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    const data = await res.json();
    renderSyncStatus(data);
  } catch (err) {
    console.error('Sync trigger failed:', err);
    if (btn) btn.disabled = false;
    return;
  }

  // Poll for completion
  _syncPollInterval = setInterval(async () => {
    const data = await apiFetch('/api/sync/status');
    if (!data) return;
    renderSyncStatus(data);
    if (data.status !== 'running') {
      clearInterval(_syncPollInterval);
      _syncPollInterval = null;
    }
  }, 2000);
}

// ─── Pushover settings ────────────────────────────────────────────────────────

async function loadPushoverSettings() {
  const data = await apiFetch('/api/notifications/settings');
  if (!data) return;

  const badge = document.getElementById('pushover-status-badge');
  if (badge) {
    if (data.enabled) {
      badge.className = 'badge bg-success';
      badge.textContent = 'enabled';
    } else if (data.app_token) {
      badge.className = 'badge bg-warning text-dark';
      badge.textContent = 'disabled';
    } else {
      badge.className = 'badge bg-secondary';
      badge.textContent = 'not configured';
    }
  }

  // Show masked credential values as read-only text so user can see they're saved
  const tokDisplay = document.getElementById('po-app-token-display');
  if (tokDisplay) tokDisplay.textContent = data.app_token || '—';
  const ukDisplay = document.getElementById('po-user-key-display');
  if (ukDisplay) ukDisplay.textContent = data.user_key || '—';

  const set = (id, val) => { const el = document.getElementById(id); if (el) el[el.type === 'checkbox' ? 'checked' : 'value'] = val; };
  set('po-enabled', data.enabled);
  set('po-alert-sync-failure', data.alert_sync_failure);
  set('po-alert-offline', data.alert_instance_offline);
  set('po-alert-no-logs', data.alert_no_logs);
  set('po-alert-block-rate', data.alert_high_block_rate);
  set('po-no-logs-minutes', data.no_logs_minutes);
  set('po-block-rate-pct', data.block_rate_threshold_pct);
  set('po-offline-retries', data.offline_alert_retries ?? 1);
  set('po-offline-max-count', data.offline_alert_max_count ?? 1);
}

async function savePushoverSettings() {
  const get = (id) => document.getElementById(id);
  const body = {
    app_token: get('po-app-token')?.value || '',
    user_key: get('po-user-key')?.value || '',
    enabled: get('po-enabled')?.checked ?? false,
    alert_sync_failure: get('po-alert-sync-failure')?.checked ?? true,
    alert_instance_offline: get('po-alert-offline')?.checked ?? true,
    alert_no_logs: get('po-alert-no-logs')?.checked ?? true,
    alert_high_block_rate: get('po-alert-block-rate')?.checked ?? false,
    no_logs_minutes: parseInt(get('po-no-logs-minutes')?.value || '30'),
    block_rate_threshold_pct: parseFloat(get('po-block-rate-pct')?.value || '50'),
    offline_alert_retries: parseInt(get('po-offline-retries')?.value || '1'),
    offline_alert_max_count: parseInt(get('po-offline-max-count')?.value || '1'),
  };

  const res = await fetch('/api/notifications/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (res.status === 401) { window.location.href = '/login'; return; }

  const btn = document.querySelector('[onclick="savePushoverSettings()"]');
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<i class="bi bi-x me-1"></i>Save failed';
      btn.classList.replace('btn-primary', 'btn-danger');
      setTimeout(() => { btn.innerHTML = orig; btn.classList.replace('btn-danger', 'btn-primary'); }, 4000);
    }
    alert(`Settings save failed: ${err.detail || res.statusText}`);
    return;
  }
  if (btn) {
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-check me-1"></i>Saved';
    btn.classList.replace('btn-primary', 'btn-success');
    setTimeout(() => { btn.innerHTML = orig; btn.classList.replace('btn-success', 'btn-primary'); }, 2000);
  }
  // Clear password fields and reload
  if (get('po-app-token')) get('po-app-token').value = '';
  if (get('po-user-key')) get('po-user-key').value = '';
  loadPushoverSettings();
}

async function validatePushover() {
  const result = document.getElementById('po-validate-result');
  if (!result) return;
  result.innerHTML = '<span class="text-muted">Validating…</span>';

  const body = {
    app_token: document.getElementById('po-app-token')?.value || '',
    user_key: document.getElementById('po-user-key')?.value || '',
  };
  if (!body.app_token || !body.user_key) {
    result.innerHTML = '<span class="text-danger">Enter App Token and User Key first.</span>';
    return;
  }

  const res = await fetch('/api/notifications/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  const data = await res.json();
  result.innerHTML = data.ok
    ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Credentials valid</span>'
    : `<span class="text-danger"><i class="bi bi-x-circle me-1"></i>${escHtml(data.error || 'Invalid credentials')}</span>`;
}

async function testPushover() {
  const result = document.getElementById('po-validate-result');
  if (result) result.innerHTML = '<span class="text-muted">Sending test…</span>';
  const res = await fetch('/api/notifications/test', { method: 'POST', credentials: 'include' });
  if (result) {
    result.innerHTML = res.ok
      ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Test notification sent</span>'
      : '<span class="text-danger"><i class="bi bi-x-circle me-1"></i>Failed — check that notifications are enabled and credentials are saved</span>';
  }
}

// ─── Session timeout settings ─────────────────────────────────────────────────

async function loadSessionTimeout() {
  const data = await apiFetch('/api/auth/session-timeout');
  if (!data) return;
  const sel = document.getElementById('session-timeout');
  if (!sel) return;
  // Select matching option, or nearest if custom value
  const minutes = String(data.timeout_minutes);
  const opt = [...sel.options].find(o => o.value === minutes);
  if (opt) {
    sel.value = minutes;
  } else {
    // Value not in list — add it as a temporary option so the UI reflects reality
    const custom = document.createElement('option');
    custom.value = minutes;
    custom.textContent = `${data.timeout_minutes} minutes`;
    sel.appendChild(custom);
    sel.value = minutes;
  }
}

async function saveSessionTimeout() {
  const sel = document.getElementById('session-timeout');
  const result = document.getElementById('session-timeout-result');
  const btn = document.querySelector('[onclick="saveSessionTimeout()"]');
  if (!sel) return;

  const body = { timeout_minutes: parseInt(sel.value) };
  const res = await fetch('/api/auth/session-timeout', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (res.status === 401) { window.location.href = '/login'; return; }

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    if (result) result.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle me-1"></i>${escHtml(err.detail || res.statusText)}</span>`;
    return;
  }

  const label = sel.options[sel.selectedIndex]?.text || `${sel.value} min`;
  if (result) result.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Saved — takes effect on next login (${label})</span>`;
  if (btn) {
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-check me-1"></i>Saved';
    btn.classList.replace('btn-outline-secondary', 'btn-success');
    setTimeout(() => { btn.innerHTML = orig; btn.classList.replace('btn-success', 'btn-outline-secondary'); }, 2000);
  }
}

// ─── Version check settings ───────────────────────────────────────────────────

function updateCheckNowState() {
  const mypiEnabled  = document.getElementById('vc-enabled')?.checked ?? true;
  const piholeEnabled = document.getElementById('vc-pihole-enabled')?.checked ?? true;
  const btn = document.getElementById('vc-check-btn');
  if (btn) btn.disabled = !mypiEnabled && !piholeEnabled;
}

async function loadVersionCheckSettings() {
  const [mypi, pihole] = await Promise.all([
    apiFetch('/api/version/status'),
    apiFetch('/api/version/pihole-status'),
  ]);

  // ── MyPi ──
  if (mypi) {
    const badge  = document.getElementById('vc-badge');
    const status = document.getElementById('vc-status');
    const cb     = document.getElementById('vc-enabled');
    if (cb) cb.checked = mypi.enabled;

    if (!mypi.enabled) {
      if (badge)  { badge.className = 'badge bg-secondary'; badge.textContent = 'MyPi disabled'; }
      if (status) status.innerHTML = '<span class="text-muted">Version checking is off.</span>';
    } else if (!mypi.latest_version) {
      if (badge)  { badge.className = 'badge bg-secondary'; badge.textContent = 'MyPi unknown'; }
      if (status) status.innerHTML = '<span class="text-muted">No check performed yet.</span>';
    } else {
      const checked = mypi.checked_at ? new Date(mypi.checked_at).toLocaleString() : '—';
      if (mypi.up_to_date) {
        if (badge)  { badge.className = 'badge bg-success'; badge.textContent = 'MyPi current'; }
        if (status) status.innerHTML =
          `<span class="text-success"><i class="bi bi-check-circle me-1"></i>v${escHtml(mypi.current_version)} is the latest</span>
           <span class="text-muted ms-2" style="font-size:0.72rem;">checked ${escHtml(checked)}</span>`;
      } else {
        if (badge)  { badge.className = 'badge bg-danger'; badge.textContent = 'MyPi update'; }
        if (status) status.innerHTML =
          `<span class="text-danger"><i class="bi bi-exclamation-circle me-1"></i>Update available: v${escHtml(mypi.latest_version)}</span>
           <a href="${escHtml(mypi.release_url)}" target="_blank" class="btn btn-xs btn-outline-danger ms-2 py-0 px-1" style="font-size:0.75rem;">View release</a>
           <span class="text-muted ms-2" style="font-size:0.72rem;">checked ${escHtml(checked)}</span>`;
      }
    }
  }

  // ── Pi-hole ──
  if (pihole) {
    const badge  = document.getElementById('vc-pihole-badge');
    const status = document.getElementById('vc-pihole-status');
    const cb     = document.getElementById('vc-pihole-enabled');
    if (cb) cb.checked = pihole.enabled;

    if (!pihole.enabled) {
      if (badge)  { badge.className = 'badge bg-secondary'; badge.textContent = 'Pi-hole disabled'; }
      if (status) status.innerHTML = '<span class="text-muted">Pi-hole version checking is off.</span>';
    } else if (!pihole.latest_core) {
      if (badge)  { badge.className = 'badge bg-secondary'; badge.textContent = 'Pi-hole unknown'; }
      if (status) status.innerHTML = '<span class="text-muted">No check performed yet.</span>';
    } else {
      const checked = pihole.checked_at ? new Date(pihole.checked_at).toLocaleString() : '—';
      if (badge)  { badge.className = 'badge bg-success'; badge.textContent = 'Pi-hole checked'; }
      if (status) status.innerHTML =
        `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Latest — core: ${escHtml(pihole.latest_core)}, FTL: ${escHtml(pihole.latest_ftl)}, web: ${escHtml(pihole.latest_web)}</span>
         <span class="text-muted ms-2" style="font-size:0.72rem;">checked ${escHtml(checked)}</span>`;
    }
  }

  updateCheckNowState();
}

async function saveVersionCheckSettings() {
  const mypiEnabled   = document.getElementById('vc-enabled')?.checked ?? true;
  const piholeEnabled = document.getElementById('vc-pihole-enabled')?.checked ?? true;
  const btn = document.getElementById('vc-save-btn');

  const [res1, res2] = await Promise.all([
    fetch('/api/version/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      credentials: 'include', body: JSON.stringify({ enabled: mypiEnabled }),
    }),
    fetch('/api/version/pihole-settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      credentials: 'include', body: JSON.stringify({ enabled: piholeEnabled }),
    }),
  ]);
  if (res1.status === 401 || res2.status === 401) { window.location.href = '/login'; return; }

  const ok = res1.ok && res2.ok;
  if (btn) {
    const orig = btn.innerHTML;
    btn.innerHTML = ok ? '<i class="bi bi-check me-1"></i>Saved' : '<i class="bi bi-x me-1"></i>Failed';
    btn.classList.replace('btn-outline-primary', ok ? 'btn-success' : 'btn-danger');
    setTimeout(() => {
      btn.innerHTML = orig;
      btn.classList.replace(ok ? 'btn-success' : 'btn-danger', 'btn-outline-primary');
    }, 2000);
  }
  if (ok) loadVersionCheckSettings();
}

async function checkVersionNow() {
  const btn = document.getElementById('vc-check-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-arrow-clockwise spin me-1"></i>Checking…'; }
  try {
    const mypiEnabled   = document.getElementById('vc-enabled')?.checked ?? true;
    const piholeEnabled = document.getElementById('vc-pihole-enabled')?.checked ?? true;
    const checks = [];
    if (mypiEnabled)   checks.push(fetch('/api/version/check',        { method: 'POST', credentials: 'include' }));
    if (piholeEnabled) checks.push(fetch('/api/version/pihole-check', { method: 'POST', credentials: 'include' }));
    if (checks.length) await Promise.all(checks);
    await loadVersionCheckSettings();
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>Check now'; }
    updateCheckNowState();
  }
}

// ─── Theme ───────────────────────────────────────────────────────────────────

function getChartColors() {
  const dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
  return {
    tick:    dark ? '#adb5bd' : '#666',
    grid:    dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)',
    tipBg:   dark ? '#2b2b2b' : '#fff',
    tipText: dark ? '#e0e0e0' : '#333',
    border:  dark ? '#212529' : '#fff',
  };
}

function applyTheme(pref) {
  const resolved = pref === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : pref;
  document.documentElement.setAttribute('data-bs-theme', resolved);
  // Trigger chart re-render if on dashboard
  if (document.getElementById('queriesChart') || document.getElementById('typeChart')) {
    if (typeof loadDashboard === 'function') loadDashboard();
  }
}

function setTheme(pref) {
  localStorage.setItem('mypi-theme', pref);
  applyTheme(pref);
  ['light', 'dark', 'system'].forEach(t => {
    const btn = document.getElementById('theme-btn-' + t);
    if (btn) btn.classList.toggle('active', t === pref);
  });
}

function initThemeButtons() {
  const pref = localStorage.getItem('mypi-theme') || 'system';
  ['light', 'dark', 'system'].forEach(t => {
    const btn = document.getElementById('theme-btn-' + t);
    if (btn) btn.classList.toggle('active', t === pref);
  });
}

// Update theme live if OS switches and preference is 'system'
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
  if ((localStorage.getItem('mypi-theme') || 'system') === 'system') applyTheme('system');
});

// ─── Security helper ──────────────────────────────────────────────────────────

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
