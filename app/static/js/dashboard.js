/* ─── MyPi Dashboard JS ──────────────────────────────────────────────────── */

'use strict';

let queriesChart = null;
let typeChart = null;
let _drillFilter = {};
let _drillPage = 1;
let _drillHours = 24;
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

async function loadDashboard() {
  const hours = document.getElementById('time-range')?.value || 24;

  try {
    const [summary, instances, history, top] = await Promise.all([
      apiFetch('/api/stats/summary'),
      apiFetch('/api/instances'),
      apiFetch(`/api/stats/history?hours=${hours}`),
      apiFetch(`/api/stats/top?hours=${hours}&limit=10`),
    ]);

    if (!summary) return;

    // Stat cards
    document.getElementById('total-queries').textContent = fmtNum(summary.totals.dns_queries_today);
    document.getElementById('queries-blocked').textContent = fmtNum(summary.totals.queries_blocked);
    document.getElementById('percent-blocked').textContent = fmtPct(summary.totals.percent_blocked);
    document.getElementById('blocklist-size').textContent = fmtNum(summary.totals.domains_on_blocklist);

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

    _drillHours = hours;

    // Online count badge
    updateStatusBadge(instances);
    const lu = document.getElementById('last-updated');
    if (lu) lu.textContent = 'Updated ' + new Date().toLocaleTimeString();

    // Queries over time chart
    renderQueriesChart(history.buckets);

    // Query type chart (from summary instances)
    renderTypeChart(summary.totals);

    // Per-instance table
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
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 12, font: { size: 10 } } },
        y: { beginAtZero: true, ticks: { font: { size: 10 } } },
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
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: 'bottom', labels: { font: { size: 11 }, padding: 10 } },
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

  tbody.innerHTML = instances.map(inst => `
    <tr>
      <td>
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${inst.color};margin-right:6px;"></span>
        <strong>${escHtml(inst.name)}</strong>
      </td>
      <td>${instanceDot(inst.status)}${inst.status}</td>
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
      ? data.items.map(q => `
          <tr>
            <td class="text-nowrap small">${fmtTime(q.timestamp)}</td>
            <td><span class="badge rounded-pill" style="background:#6c757d;font-weight:500;">${escHtml(q.instance_name)}</span></td>
            <td class="text-truncate" style="max-width:220px;" title="${escHtml(q.domain || '')}">${escHtml(q.domain || '—')}</td>
            <td><code class="small">${escHtml(q.query_type || '—')}</code></td>
            <td class="small">${escHtml(q.client_name || q.client_ip || '—')}</td>
            <td>${statusPill(q.status)}</td>
            <td class="text-end small">${q.reply_time_ms != null ? Number(q.reply_time_ms).toFixed(1) : '—'}</td>
          </tr>
        `).join('')
      : '<tr><td colspan="7" class="text-center text-muted py-4">No queries found.</td></tr>';

    const totalPages = Math.ceil(data.total / data.page_size);
    renderPagination('pagination-top', currentPage, totalPages);
    renderPagination('pagination-bottom', currentPage, totalPages);

    const lu = document.getElementById('last-updated');
    if (lu) lu.textContent = 'Updated ' + new Date().toLocaleTimeString();

  } catch (err) {
    console.error('Query log error:', err);
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
  tbody.innerHTML = instances.map(i => `
    <tr>
      <td>
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${i.color};margin-right:6px;"></span>
        ${escHtml(i.name)}
        ${i.is_master ? '<span class="badge bg-primary ms-1" style="font-size:0.65rem;">master</span>' : ''}
      </td>
      <td class="small text-muted">${escHtml(i.url)}</td>
      <td>${instanceDot(i.status)}${i.status}</td>
    </tr>
  `).join('');
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
  const params = new URLSearchParams({ page, page_size: 50, hours: _drillHours });
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
  const el = document.getElementById('sync-last-synced');
  if (!el) return;

  let status = null;
  try {
    const res = await fetch('/api/sync/status', { credentials: 'include', cache: 'no-store' });
    if (res.ok) status = await res.json();
  } catch (err) {
    el.innerHTML = '<i class="bi bi-arrow-repeat me-1 text-secondary"></i>Pi sync: <span class="text-muted">unavailable</span>';
    return;
  }

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
  const label = body.interval_minutes > 0
    ? `Schedule saved — syncing every ${body.interval_minutes} min.`
    : 'Schedule saved — manual sync only.';
  const btn = document.querySelector('[onclick="saveSchedule()"]');
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

  const rows = (data.results || []).map(r => {
    const icon = r.status === 'success'
      ? '<i class="bi bi-check-circle text-success me-1"></i>'
      : '<i class="bi bi-x-circle text-danger me-1"></i>';
    const err = r.error ? ` — <span class="text-danger">${escHtml(r.error)}</span>` : '';
    return `<div>${icon}<strong>${escHtml(r.name)}</strong>${err}</div>`;
  }).join('');

  const masterLine = data.master ? `<div class="text-muted mb-1">Master: <strong>${escHtml(data.master)}</strong></div>` : '';
  result.innerHTML = `${masterLine}${rows}<div class="text-muted mt-1">Completed: ${finished}</div>`;
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
