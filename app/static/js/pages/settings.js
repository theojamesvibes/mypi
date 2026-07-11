/* Settings page boot + control wiring. Most loaders/savers live in
 * /static/js/dashboard.js; the page-only ones (display, poll interval,
 * change password) are defined below. Inline handlers are not allowed
 * under the strict script-src CSP, so every control is wired here. */

'use strict';

(function () {
  const wire = (id, event, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(event, handler);
  };

  wire('theme-btn-light',  'click', () => setTheme('light'));
  wire('theme-btn-dark',   'click', () => setTheme('dark'));
  wire('theme-btn-system', 'click', () => setTheme('system'));

  wire('stale-remove-all-btn',     'click', deleteAllStale);
  wire('sync-schedule-save-btn',   'click', saveSchedule);
  wire('sync-btn',                 'click', triggerSync);
  wire('poll-interval-save-btn',   'click', savePollInterval);
  wire('display-save-btn',         'click', saveDisplaySettings);
  wire('session-timeout-save-btn', 'click', saveSessionTimeout);

  wire('vc-enabled',        'change', updateCheckNowState);
  wire('vc-pihole-enabled', 'change', updateCheckNowState);
  wire('vc-check-btn',      'click',  checkVersionNow);
  wire('vc-save-btn',       'click',  saveVersionCheckSettings);

  wire('po-validate-btn', 'click', validatePushover);
  wire('po-test-btn',     'click', testPushover);
  wire('po-save-btn',     'click', savePushoverSettings);

  wire('cp-btn',          'click',  changePassword);
  wire('create-key-form', 'submit', createApiKey);
})();

loadApiKeys();
loadSettingsInstances();
loadStaleSites();
loadStaleInstances();
loadSyncStatus();
loadSyncSchedule();
loadPushoverSettings();
loadSessionTimeout();
loadVersionCheckSettings();
loadPollInterval();
loadDisplaySettings();
initThemeButtons();

async function loadDisplaySettings() {
  try {
    const resp = await fetch('/api/display-settings', { credentials: 'include' });
    if (!resp.ok) return;
    const data = await resp.json();
    const cb = document.getElementById('hide-pihole-self');
    if (cb) cb.checked = !!data.hide_pihole_self_in_top_clients;
  } catch {}
}

async function saveDisplaySettings() {
  const status = document.getElementById('display-save-status');
  const cb = document.getElementById('hide-pihole-self');
  if (status) { status.textContent = ''; status.className = 'ms-2 small text-muted'; }
  try {
    const resp = await fetch('/api/display-settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        hide_pihole_self_in_top_clients: !!(cb && cb.checked),
      }),
    });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    if (!resp.ok) throw new Error('save failed');
    if (status) { status.textContent = 'Saved'; status.className = 'ms-2 small text-success'; }
  } catch {
    if (status) { status.textContent = 'Save failed'; status.className = 'ms-2 small text-danger'; }
  }
}

async function loadPollInterval() {
  try {
    const resp = await fetch('/api/poll-settings/');
    if (!resp.ok) return;
    const data = await resp.json();
    const sel = document.getElementById('poll-interval');
    // Select the closest option; fall back to first if exact match not found.
    const opt = [...sel.options].find(o => parseInt(o.value) === data.interval_seconds);
    if (opt) sel.value = opt.value;
  } catch {}
}

async function savePollInterval() {
  const result = document.getElementById('poll-interval-result');
  const interval = parseInt(document.getElementById('poll-interval').value);
  result.textContent = '';
  result.className = 'small mt-2';
  try {
    const resp = await fetch('/api/poll-settings/', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({interval_seconds: interval}),
    });
    const data = await resp.json();
    if (resp.ok) {
      result.textContent = `Saved — polling every ${data.interval_seconds}s (takes effect immediately).`;
      result.classList.add('text-success');
    } else {
      result.textContent = data.detail || 'Save failed.';
      result.classList.add('text-danger');
    }
  } catch {
    result.textContent = 'Request failed.';
    result.classList.add('text-danger');
  }
}

async function changePassword() {
  const current = document.getElementById('cp-current').value;
  const newPw   = document.getElementById('cp-new').value;
  const confirm = document.getElementById('cp-confirm').value;
  const result  = document.getElementById('cp-result');

  result.textContent = '';
  result.className = 'small mt-2';

  try {
    const resp = await fetch('/api/auth/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({current_password: current, new_password: newPw, confirm_password: confirm}),
    });
    const data = await resp.json();
    if (resp.ok) {
      result.textContent = data.detail;
      result.classList.add('text-success');
      document.getElementById('cp-current').value = '';
      document.getElementById('cp-new').value = '';
      document.getElementById('cp-confirm').value = '';
    } else {
      result.textContent = data.detail || 'Password change failed.';
      result.classList.add('text-danger');
    }
  } catch {
    result.textContent = 'Request failed.';
    result.classList.add('text-danger');
  }
}
