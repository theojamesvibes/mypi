/* ─── MyPi Dashboard JS ──────────────────────────────────────────────────── */

'use strict';

let queriesChart = null;
let typeChart = null;

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

function statusPill(status) {
  if (!status) return '<span class="status-pill status-other">—</span>';
  const s = status.toLowerCase();
  if (s.startsWith('blocked')) return `<span class="status-pill status-blocked">${status}</span>`;
  if (s === 'forwarded') return `<span class="status-pill status-forwarded">Forwarded</span>`;
  if (s === 'cached') return `<span class="status-pill status-cached">Cached</span>`;
  return `<span class="status-pill status-other">${status}</span>`;
}

function instanceDot(status) {
  const cls = status === 'online' ? 'dot-online' : status === 'offline' ? 'dot-offline' : 'dot-unknown';
  return `<span class="instance-dot ${cls}"></span>`;
}

async function apiFetch(url) {
  const res = await fetch(url, { credentials: 'include' });
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

    // Online count badge
    const onlineCount = instances.filter(i => i.status === 'online').length;
    const onlineBadge = document.getElementById('online-count');
    if (onlineBadge) {
      onlineBadge.textContent = `${onlineCount}/${instances.length} online`;
      onlineBadge.className = onlineCount === instances.length ? 'badge bg-success' : 'badge bg-warning text-dark';
    }
    const lu = document.getElementById('last-updated');
    if (lu) lu.textContent = 'Updated ' + new Date().toLocaleTimeString();

    // Queries over time chart
    renderQueriesChart(history.buckets);

    // Query type chart (from summary instances)
    renderTypeChart(summary.totals);

    // Per-instance table
    renderInstancesTable(instances);

    // Top tables
    renderTopTable('top-permitted', top.top_permitted, r => r.domain, r => fmtNum(r.count));
    renderTopTable('top-blocked', top.top_blocked, r => r.domain, r => fmtNum(r.count));
    renderTopTable('top-clients', top.top_clients, r => r.client, r => fmtNum(r.count));

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

function renderTopTable(tbodyId, rows, labelFn, countFn) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="2" class="text-center text-muted py-2 small">No data yet</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td class="text-truncate" style="max-width:200px;" title="${escHtml(labelFn(r))}">${escHtml(labelFn(r))}</td>
      <td class="text-end">${countFn(r)}</td>
    </tr>
  `).join('');
}

// ─── Query Log ───────────────────────────────────────────────────────────────

let currentPage = 1;

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
}

async function loadQueries(page) {
  currentPage = page || 1;
  const instance = document.getElementById('f-instance')?.value || '';
  const domain = document.getElementById('f-domain')?.value || '';
  const client = document.getElementById('f-client')?.value || '';
  const status = document.getElementById('f-status')?.value || '';
  const hours = document.getElementById('f-hours')?.value || 24;

  const params = new URLSearchParams({ page: currentPage, page_size: 100, hours });
  if (instance) params.set('instance_id', instance);
  if (domain) params.set('domain', domain);
  if (client) params.set('client', client);
  if (status) params.set('status', status);

  try {
    const data = await apiFetch(`/api/queries?${params}`);
    if (!data) return;

    const tbody = document.getElementById('queries-tbody');
    if (!tbody) return;

    document.getElementById('query-count').textContent =
      `${fmtNum(data.total)} total results — page ${data.page}`;

    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">No queries found.</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(q => `
        <tr>
          <td class="text-nowrap small">${fmtTime(q.timestamp)}</td>
          <td><span class="badge rounded-pill" style="background:#6c757d;font-weight:500;">${escHtml(q.instance_name)}</span></td>
          <td class="text-truncate" style="max-width:220px;" title="${escHtml(q.domain || '')}">${escHtml(q.domain || '—')}</td>
          <td><code class="small">${q.query_type || '—'}</code></td>
          <td class="small">${escHtml(q.client_name || q.client_ip || '—')}</td>
          <td>${statusPill(q.status)}</td>
          <td class="text-end small">${q.reply_time_ms != null ? Number(q.reply_time_ms).toFixed(1) : '—'}</td>
        </tr>
      `).join('');
    }

    // Pagination
    const totalPages = Math.ceil(data.total / data.page_size);
    renderPagination('pagination-top', currentPage, totalPages);
    renderPagination('pagination-bottom', currentPage, totalPages);

  } catch (err) {
    console.error('Query log error:', err);
  }
}

function renderPagination(id, current, total) {
  const el = document.getElementById(id);
  if (!el) return;
  if (total <= 1) { el.innerHTML = ''; return; }

  const maxPages = 7;
  let pages = [];
  if (total <= maxPages) {
    pages = Array.from({ length: total }, (_, i) => i + 1);
  } else {
    pages = [1];
    let start = Math.max(2, current - 2);
    let end = Math.min(total - 1, current + 2);
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
  tbody.innerHTML = instances.map(i => `
    <tr>
      <td>
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${i.color};margin-right:6px;"></span>
        ${escHtml(i.name)}
      </td>
      <td class="small text-muted">${escHtml(i.url)}</td>
      <td>${instanceDot(i.status)}${i.status}</td>
    </tr>
  `).join('');
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
